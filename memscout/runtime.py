"""Self-contained reporter-side runtime for memscout collection scripts.

This is everything a script needs to run on a REPORTER's machine and nothing more:
attach read-only, enumerate loaded modules, relocate a developer-supplied
`(module, offset)`, scan the heap, and decode fields. It uses the Python standard
library only -- no symbols, DWARF, `readelf`, or symbol server. (Build-ids are read
from the ELF `.note.gnu.build-id` note directly, so even `readelf` isn't needed.)

It is the single source of truth for these primitives: the full framework's `Target`
subclasses `Reporter` here and adds the developer-side pieces (symbol resolution,
vtable class identification). `memscout bundle` inlines this module ahead of a
developer's script to ship one self-contained file.

Public surface: `Reporter`, `Module`, `ModuleMap`, `register` (custom decoders),
`registered_tokens` (list the supported decoder tokens), and `decode`/`parse_spec`.
"""

import ctypes
import os
import struct

# Slot values in this range are treated as candidate userspace pointers when
# annotating a dump (below the x86-64 canonical user ceiling, above the first page).
_PTR_LO = 0x1000
_PTR_HI = 0x800000000000

# Regions larger than this are almost never the C++ malloc heap; skipping them keeps
# a scan to tens of MB. Matches scripts/procmem-vptr-scan.py.
_MAX_SCAN_REGION = 256 * 1024 * 1024


# ============================================================================
# Memory: read-only /proc/<pid>/mem with a non-stopping PTRACE_SEIZE fallback
# ============================================================================

# PTRACE_SEIZE attaches for /proc/mem access without ever stopping the tracee;
# PTRACE_ATTACH would stop it, which we must never do.
_PTRACE_SEIZE = 0x4206


def _ptrace_seize(pid):
    """SEIZE the process so /proc/<pid>/mem opens under Yama, without stopping it."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ctypes.set_errno(0)
    res = libc.ptrace(ctypes.c_long(_PTRACE_SEIZE), ctypes.c_long(pid),
                      ctypes.c_void_p(0), ctypes.c_void_p(0))
    if res == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class MemorySource:
    """A read-only handle on one process's memory via /proc/<pid>/mem.

    read() returns None (never raises) for memory that isn't mapped or readable.
    Never writes, never stops the tracee, never PTRACE_DETACHes.
    """

    def __init__(self, pid):
        self.pid = pid
        self._fd = self._open(pid)

    @staticmethod
    def _open(pid):
        path = "/proc/%d/mem" % pid
        try:
            return os.open(path, os.O_RDONLY)
        except PermissionError:
            pass
        try:
            _ptrace_seize(pid)
        except OSError as e:
            raise SystemExit(
                "cannot read %s and PTRACE_SEIZE failed (%s).\n"
                "Try running as root, or lower Yama restrictions: "
                "echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope" % (path, e))
        return os.open(path, os.O_RDONLY)

    def read(self, addr, n):
        """Return n bytes at addr, or None if the range is unmapped/unreadable."""
        try:
            return os.pread(self._fd, n, addr)
        except OSError:
            return None

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# ============================================================================
# Modules: /proc/<pid>/maps, load bias, and a native (readelf-free) build-id
# ============================================================================

def _find_build_id_note(notes):
    """Scan an ELF note blob for NT_GNU_BUILD_ID (type 3, name "GNU"); return its bytes or None."""
    off = 0
    while off + 12 <= len(notes):
        namesz = int.from_bytes(notes[off:off + 4], "little")
        descsz = int.from_bytes(notes[off + 4:off + 8], "little")
        ntype = int.from_bytes(notes[off + 8:off + 12], "little")
        off += 12
        name = notes[off:off + namesz]
        off += (namesz + 3) & ~3                # 4-byte aligned
        desc = notes[off:off + descsz]
        off += (descsz + 3) & ~3
        if ntype == 3 and name.rstrip(b"\x00") == b"GNU":
            return desc
    return None


def _read_build_id(path):
    """Read a 64-bit ELF's build-id from its PT_NOTE segments, without readelf.

    Returns the build-id bytes, or None (not an ELF64, no note, or unreadable).
    x86-64 only, matching the project's supported architecture.
    """
    try:
        with open(path, "rb") as f:
            hdr = f.read(64)
            if len(hdr) < 64 or hdr[:4] != b"\x7fELF" or hdr[4] != 2:   # ELF64
                return None
            e_phoff = int.from_bytes(hdr[0x20:0x28], "little")
            e_phentsize = int.from_bytes(hdr[0x36:0x38], "little")
            e_phnum = int.from_bytes(hdr[0x38:0x3A], "little")
            for i in range(e_phnum):
                f.seek(e_phoff + i * e_phentsize)
                ph = f.read(e_phentsize)
                if len(ph) < 40 or int.from_bytes(ph[0:4], "little") != 4:  # PT_NOTE
                    continue
                p_offset = int.from_bytes(ph[8:16], "little")
                p_filesz = int.from_bytes(ph[32:40], "little")
                f.seek(p_offset)
                found = _find_build_id_note(f.read(p_filesz))
                if found is not None:
                    return found
    except OSError:
        return None
    return None


def _is_elf(path):
    """True if the on-disk file at `path` starts with the ELF magic."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


# Sentinel distinguishing "build-id not looked up yet" from "looked up, none found".
_UNSET = object()


class Module:
    """One ELF file mapped into the target process.

    load_bias is the runtime address of the mapping at file offset 0 (adding it to a
    link-relative offset yields a runtime address). ranges are the mapped extents.
    build_id is read from the ELF note on first access (no readelf).
    """

    def __init__(self, path, load_bias, ranges):
        self.path = path
        self.load_bias = load_bias
        self.ranges = ranges
        self._build_id = _UNSET

    @property
    def name(self):
        """The file's basename, e.g. 'libxul.so'."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def build_id(self):
        """The module's build-id bytes (or None), read from the ELF note on first access."""
        if self._build_id is _UNSET:
            self._build_id = _read_build_id(self.path)
        return self._build_id

    def contains(self, addr):
        """True if addr falls in any of this module's mapped ranges."""
        return any(lo <= addr < hi for lo, hi in self.ranges)

    def __repr__(self):
        return "<Module %s bias=%#x ranges=%d>" % (
            self.name, self.load_bias, len(self.ranges))


class ModuleMap:
    """All modules loaded in a process, queryable by name or by address."""

    def __init__(self, modules):
        self.modules = modules

    def __iter__(self):
        return iter(self.modules)

    def by_name(self, name):
        """The module whose path ends in `name` (basename or suffix match), or None."""
        for m in self.modules:
            if m.name == name or m.path.endswith(name):
                return m
        return None

    def for_addr(self, addr):
        """The module whose mapped ranges cover `addr`, or None."""
        for m in self.modules:
            if m.contains(addr):
                return m
        return None


def parse(pid):
    """Build the ModuleMap for a process from /proc/<pid>/maps (ELF files only)."""
    order = []                 # paths in first-seen order, for stable output
    ranges = {}                # path -> list[(lo, hi)]
    bias = {}                  # path -> load bias (from the offset-0 mapping)
    with open("/proc/%d/maps" % pid) as maps_file:
        for line in maps_file:
            parts = line.split()
            if len(parts) < 6:
                continue
            path = parts[5]
            if not path.startswith("/"):
                continue
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            file_off = int(parts[2], 16)
            if path not in ranges:
                order.append(path)
                ranges[path] = []
            ranges[path].append((lo, hi))
            if file_off == 0 and path not in bias:
                bias[path] = lo

    modules = []
    for path in order:
        if path not in bias or not _is_elf(path):
            continue
        modules.append(Module(path, bias[path], ranges[path]))
    return ModuleMap(modules)


# ============================================================================
# Decoders: turn a named field's bytes into a typed Python value
# ============================================================================

_REGISTRY = {}                                  # token -> fn(mem, base, off, arg)


def register(token, fn):
    """Register (or replace) the decoder for a type token."""
    _REGISTRY[token] = fn


def get(token):
    """The decoder registered for `token`, or None."""
    return _REGISTRY.get(token)


def registered_tokens():
    """Sorted list of every decoder type token currently registered.

    This is the authoritative set of base tokens `decode`/`scan`/`dump` accept -- read it
    straight from the registry so it can never drift from what's actually supported.
    (Parametric tokens appear as their base here: `atomic` is used as `atomic:<T>`, and
    `mhashtable` as `mhashtable[:entry_size]`.)
    """
    return sorted(_REGISTRY)


def parse_spec(spec):
    """Split an `OFF:TYPE:NAME` field spec into (offset, type_token, name)."""
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError("bad field spec %r (want OFF:TYPE:NAME)" % spec)
    off = int(parts[0], 0)
    name = parts[-1]
    type_token = ":".join(parts[1:-1])
    return off, type_token, name


def decode_field(mem, base, spec):
    """Decode one `OFF:TYPE:NAME` field of the object at `base`; return (name, value)."""
    off, type_token, name = parse_spec(spec)
    token, _, arg = type_token.partition(":")
    fn = _REGISTRY.get(token)
    if fn is None:
        return name, "<bad-type:%s>" % type_token
    return name, fn(mem, base, off, arg or None)


# --- primitives -------------------------------------------------------------

def _make_int_decoder(size, signed):
    def dec(mem, base, off, arg):
        val = mem.read_uint(base + off, size)
        if val is None:
            return None
        if signed and val >> (size * 8 - 1):
            val -= 1 << (size * 8)              # sign-extend
        return val
    return dec


for _tok, _sz, _sg in (("u8", 1, False), ("u16", 2, False), ("u32", 4, False),
                       ("u64", 8, False), ("i8", 1, True), ("i16", 2, True),
                       ("i32", 4, True), ("i64", 8, True),
                       ("bool", 1, False), ("ptr", 8, False)):
    register(_tok, _make_int_decoder(_sz, _sg))


def _decode_atomic(mem, base, off, arg):
    """mozilla::Atomic<T> is layout-compatible with T, so decode as the T named in arg."""
    fn = _REGISTRY.get(arg or "")
    if fn is None:
        return "<bad-atomic:%s>" % arg
    return fn(mem, base, off, None)


register("atomic", _decode_atomic)


# --- Firefox strings --------------------------------------------------------

_MAX_STRLEN = 4096


def _make_string_decoder(char_bytes, encoding):
    def dec(mem, base, off, arg):
        hdr = mem.read(base + off, 12)          # T* mData; uint32 mLength
        if hdr is None:
            return "<unreadable>"
        ptr = int.from_bytes(hdr[0:8], "little")
        length = int.from_bytes(hdr[8:12], "little")
        if not ptr or length > _MAX_STRLEN:
            return "<len=%d ptr=%#x?>" % (length, ptr)
        raw = mem.read(ptr, length * char_bytes)
        return raw.decode(encoding, "replace") if raw else "<unreadable>"
    return dec


_utf16 = _make_string_decoder(2, "utf-16-le")
_utf8 = _make_string_decoder(1, "utf-8")
for _tok in ("nsstring", "nsastring", "nsautostring"):
    register(_tok, _utf16)
for _tok in ("nscstring", "nsacstring", "nsautocstring"):
    register(_tok, _utf8)


# --- Firefox pointers and containers ---------------------------------------

def _decode_ptr_member(mem, base, off, arg):
    """RefPtr/nsCOMPtr hold a single raw pointer member; return its value."""
    return mem.read_ptr(base + off)


register("refptr", _decode_ptr_member)
register("nscomptr", _decode_ptr_member)


def _decode_nstarray(mem, base, off, arg):
    """nsTArray<T>: a Header* whose first uint32 is the length; elements follow at +8."""
    hdr = mem.read_ptr(base + off)
    if not hdr:
        return {"length": 0, "data": 0}
    length = mem.read_uint(hdr, 4)
    return {"length": length, "data": hdr + 8}


register("nstarray", _decode_nstarray)


# --- Firefox hashtables (layouts: see DESIGN.md) ---------------------------

# Width of a cached hash slot: PLDHashNumber and mozilla::HashNumber are both
# uint32_t (mfbt/HashFunctions.h). The hashes block is `capacity` of these.
_HASH_WIDTH = 4


def _live_slots(mem, store, capacity, entry_size, count, live_mask):
    """Addresses of live entries.

    PLDHashTable and mozilla::HashTable (mfbt) both lay out their storage as all
    cached hashes contiguously first (hashes[capacity], _HASH_WIDTH bytes each),
    *then* all entries contiguously (entries[capacity], entry_size bytes each) --
    not one combined [hash, entry] struct repeated per slot. (Both headers spell
    this out: interleaving would waste ABI-mandated padding between a 4-byte hash
    and whatever alignment the entry itself needs, e.g. a single pointer on
    64-bit.) So slot i's hash and slot i's entry live at different offsets from
    `store`, and the entries block starts at store + capacity*_HASH_WIDTH with no
    padding gap (&hashes[capacity] is exact; power-of-two capacity keeps entry0
    naturally aligned).
    """
    live = []
    if not (store and entry_size and capacity):
        return live
    entries = store + capacity * _HASH_WIDTH
    for i in range(capacity):
        keyhash = mem.read_uint(store + i * _HASH_WIDTH, _HASH_WIDTH)
        if keyhash is not None and (keyhash & live_mask) > 1:
            live.append(entries + i * entry_size)
            if count is not None and len(live) >= count:
                break
    return live


def _decode_pldhash(mem, base, off, arg):
    """XPCOM PLDHashTable at base+off -> {count, capacity, entry_size, live[]}."""
    t = base + off
    store = mem.read_ptr(t + 8)
    hash_shift = mem.read_uint(t + 18, 1)
    entry_size = mem.read_uint(t + 19, 1)
    count = mem.read_uint(t + 20, 4)
    if count is None:
        return None
    capacity = (1 << (32 - hash_shift)) if hash_shift else 0
    live = _live_slots(mem, store, capacity, entry_size, count, 0xFFFFFFFF)
    return {"count": count, "capacity": capacity, "entry_size": entry_size, "live": live}


def _decode_mhashtable(mem, base, off, arg):
    """mfbt mozilla::HashMap/HashSet mImpl at base+off -> {count, capacity, live[]}."""
    t = base + off
    gen_shift = mem.read_uint(t, 8)
    table = mem.read_ptr(t + 8)
    count = mem.read_uint(t + 16, 4)
    if count is None:
        return None
    hash_shift = (gen_shift & 0xFF) if gen_shift is not None else 0
    capacity = (1 << (32 - hash_shift)) if hash_shift else 0
    entry_size = int(arg, 0) if arg else None
    live = _live_slots(mem, table, capacity, entry_size, count, 0x7FFFFFFF)
    return {"count": count, "capacity": capacity, "live": live}


register("pldhash", _decode_pldhash)
register("mhashtable", _decode_mhashtable)


# ============================================================================
# Reporter: the read-only facade a collection script drives
# ============================================================================

class Reporter:
    """Read-only view of a running process: relocate, scan, read, decode.

    The reporter-side surface -- everything usable without symbols or DWARF. Use as
    a context manager so the memory fd is always closed. The full `Target` subclasses
    this and adds symbol resolution and vtable class identification.
    """

    def __init__(self, pid):
        self.pid = pid
        self._mem = MemorySource(pid)
        self._modules = None                    # built lazily on first .modules access

    # --- lifecycle ---

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._mem.close()

    # --- raw and typed reads ---

    def read(self, addr, n):
        """Return n bytes at addr, or None if the range is unmapped/unreadable."""
        return self._mem.read(addr, n)

    def read_uint(self, addr, size):
        """Read a little-endian unsigned integer of `size` bytes, or None."""
        data = self._mem.read(addr, size)
        return None if data is None else int.from_bytes(data, "little")

    def read_ptr(self, addr):
        """Read an 8-byte pointer-sized value, or None."""
        return self.read_uint(addr, 8)

    # --- module map + relocation ---

    @property
    def modules(self):
        """The process's loaded ELF modules (built once, on first access)."""
        if self._modules is None:
            self._modules = parse(self.pid)
        return self._modules

    def module(self, name):
        """The loaded module named `name` (basename or path suffix), or None."""
        return self.modules.by_name(name)

    def relocate(self, module, offset):
        """Live runtime address of a link-relative `offset` in a loaded module, or None.

        The developer resolves a symbol offline to (module, offset); this turns it into
        a real address with only the module map -- no symbols, DWARF, or readelf.
        `module` may be a module name or a Module.
        """
        m = self.module(module) if isinstance(module, str) else module
        return None if m is None else m.load_bias + offset

    # --- heap scanning ---

    def scan_regions(self, include_js=False):
        """Yield (lo, hi) for each writable region worth scanning for C++ objects."""
        with open("/proc/%d/maps" % self.pid) as maps_file:
            for line in maps_file:
                parts = line.split()
                if len(parts) < 2 or "w" not in parts[1]:
                    continue
                name = parts[5] if len(parts) > 5 else ""
                if not include_js and ("js-gc-heap" in name or name.startswith("/")):
                    continue
                lo, hi = (int(x, 16) for x in parts[0].split("-"))
                if hi - lo > _MAX_SCAN_REGION:
                    continue
                yield lo, hi

    def find_objects(self, needle8, include_js=False, limit=1000):
        """Return addresses where the 8-byte value `needle8` appears in scanned regions."""
        pat = struct.pack("<Q", needle8)
        hits = []
        for lo, hi in self.scan_regions(include_js):
            data = self._mem.read(lo, hi - lo)
            if not data:
                continue
            i = data.find(pat)
            while i != -1:
                hits.append(lo + i)
                if len(hits) >= limit:
                    return hits
                i = data.find(pat, i + 8)
        return hits

    # --- field decoding ---

    def decode(self, base, fields):
        """Decode named fields of the object at `base` into {name: value}.

        `fields` is a space-separated string of `OFF:TYPE:NAME` specs, or a list.
        """
        specs = fields.split() if isinstance(fields, str) else fields
        out = {}
        for spec in specs:
            name, value = decode_field(self, base, spec)
            out[name] = value
        return out

    def dump_slots(self, base, count=12):
        """Yield plain lines for the first `count` 8-byte slots at `base` (UTF-16 guesses)."""
        obj = self._mem.read(base, count * 8)
        if not obj:
            yield "  <unreadable object>"
            return
        for o in range(0, count * 8, 8):
            val = int.from_bytes(obj[o:o + 8], "little")
            hint = ""
            if _PTR_LO < val < _PTR_HI:
                s = self._mem.read(val, 32)
                if s:
                    text = s.decode("utf-16-le", "ignore")
                    printable = "".join(c for c in text if 32 <= ord(c) < 127)
                    if len(printable) >= 3:
                        hint = "  -> u16 %r" % printable[:24]
            yield "  +%3d: %#018x%s" % (o, val, hint)
