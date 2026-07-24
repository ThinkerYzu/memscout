"""Target: the one object a script holds to inspect a live process.

It attaches read-only (via MemorySource), exposes memory reads and typed
readers, lazily builds the module map, and scans the heap for byte patterns
(e.g. a vtable pointer). Symbol resolution and field decoding are layered on in
later phases; Phase 2.1 provides the read/enumerate/scan core.

    with memscout.Target(pid) as t:
        for m in t.modules:
            print(m.name, hex(m.load_bias), m.build_id and m.build_id.hex())
"""

import struct

from . import decoders, maps
from .memory import MemorySource
from .symbols import SymbolResolver


# Regions larger than this are almost never the C++ malloc heap; skipping them
# keeps a scan to tens of MB. Matches scripts/procmem-vptr-scan.py.
_MAX_SCAN_REGION = 256 * 1024 * 1024


class Target:
    """A read-only view of a running process, identified by pid.

    Use as a context manager so the memory fd is always closed. read() returns
    None for unmapped memory rather than raising, so callers can probe freely.
    """

    def __init__(self, pid):
        self.pid = pid
        self._mem = MemorySource(pid)
        self._modules = None            # built lazily on first .modules access
        self._resolver = SymbolResolver()

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
        if data is None:
            return None
        return int.from_bytes(data, "little")

    def read_ptr(self, addr):
        """Read an 8-byte pointer-sized value, or None."""
        return self.read_uint(addr, 8)

    # --- module map ---

    @property
    def modules(self):
        """The process's loaded ELF modules (built once, on first access)."""
        if self._modules is None:
            self._modules = maps.parse(self.pid)
        return self._modules

    def module(self, name):
        """The loaded module named `name` (basename or path suffix), or None."""
        return self.modules.by_name(name)

    # --- symbol resolution ---

    def resolve(self, name, module=None):
        """Resolve a symbol to a Symbol (runtime address + provenance), or None.

        `module` may be a module name, a Module, or None to search every module.
        """
        if isinstance(module, str):
            module = self.module(module)
        return self._resolver.resolve(self.modules, name, module)

    def vtable(self, name, module=None, secondary_offset=16):
        """Runtime value objects store at their vptr slot for the class `name`.

        That is the vtable symbol's address past its two header words (offset-to-top
        and typeinfo) -- i.e. +16 for a primary base. Pass a class's sub-vtable
        offset via secondary_offset for a multiply-inherited secondary base.
        Returns the needle address, or None if the vtable symbol can't be resolved.
        """
        sym = self.resolve(name, module)
        return None if sym is None else sym.addr + secondary_offset

    # --- field decoding ---

    def decode(self, base, fields):
        """Decode named fields of the object at `base` into {name: value}.

        `fields` is a space-separated string of `OFF:TYPE:NAME` specs, or a list
        of them. Field types come from the decoder registry (memscout.decoders).
        """
        specs = fields.split() if isinstance(fields, str) else fields
        out = {}
        for spec in specs:
            name, value = decoders.decode_field(self, base, spec)
            out[name] = value
        return out

    def dump_slots(self, base, count=12):
        """Yield formatted lines for the first `count` 8-byte slots at `base`.

        Reveals a raw object layout (vptrs, small ints, string pointers) when the
        field offsets aren't known yet, guessing UTF-16 strings behind pointers.
        """
        obj = self._mem.read(base, count * 8)
        if not obj:
            yield "  <unreadable object>"
            return
        for o in range(0, count * 8, 8):
            val = int.from_bytes(obj[o:o + 8], "little")
            hint = ""
            if 0x1000 < val < 0x800000000000:
                s = self._mem.read(val, 32)
                if s:
                    text = s.decode("utf-16-le", "ignore")
                    printable = "".join(c for c in text if 32 <= ord(c) < 127)
                    if len(printable) >= 3:
                        hint = "  -> u16 %r" % printable[:24]
            yield "  +%3d: %#018x%s" % (o, val, hint)

    # --- heap region scanning ---

    def scan_regions(self, include_js=False):
        """Yield (lo, hi) for each writable region worth scanning for C++ objects.

        C++ objects live in the malloc heap ([anon:jemalloc] and plain anonymous
        mappings), so by default we skip the JS GC heap and file-backed mappings
        and any region over 256 MB. Pass include_js=True to widen the net.
        """
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
        """Return addresses where the 8-byte value `needle8` appears in scanned regions.

        Reads each writable region once and scans it in-process (fast), stopping
        at `limit` hits. The primary use is locating objects by their vtable
        pointer (see vtable()), but any 8-byte needle works.
        """
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
