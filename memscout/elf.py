"""Thin wrappers over `readelf` for the ELF facts memscout needs.

Level 1 only needs a module's build-id (to key the remote symbol sources) and,
later, its symbol addresses and base vaddr. We shell out to binutils rather than
take a parsing dependency (DESIGN Decision 1); this module is the single place
that does so, so swapping in pyelftools at Level 2 stays a local change.
"""

import shutil
import subprocess


def demangle(name):
    """Demangle a C++ linker symbol with c++filt, or return it unchanged.

    e.g. `_ZTV6Widget` -> `vtable for Widget`. Falls back to the raw name if
    c++filt is missing or fails, so callers never need to guard.
    """
    filt = shutil.which("c++filt")
    if not filt:
        return name
    try:
        out = subprocess.run([filt, name], capture_output=True, text=True)
    except OSError:
        return name
    return out.stdout.strip() if out.returncode == 0 else name


def _readelf(path, *flags):
    """Run `readelf <flags> <path>` and return stdout, or None if it can't be read."""
    try:
        out = subprocess.run(["readelf", *flags, path],
                             capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout


def build_id(path):
    """Return the ELF's build-id as bytes, or None if it has none / isn't readable.

    Parses the `readelf -n` note dump, whose Build ID line looks like
    "    Build ID: 1b8f9e...". The hex is the raw note payload, so we return it
    decoded to bytes -- the form both remote sources key on.
    """
    out = _readelf(path, "-n")
    if out is None:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Build ID:"):
            hexstr = line.split(":", 1)[1].strip()
            try:
                return bytes.fromhex(hexstr)
            except ValueError:
                return None
    return None


def load_vaddr(path):
    """VirtAddr of the LOAD segment covering file offset 0 (the ELF's base vaddr).

    Turns a symbol's linked address into a runtime one: works for PIE libs
    (base vaddr usually 0) and non-PIE executables (e.g. 0x400000) alike.
    Returns None if `readelf` can't read the file or finds no such segment.
    """
    out = _readelf(path, "-lW")
    if out is None:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("LOAD"):
            continue
        # LOAD  Offset  VirtAddr  PhysAddr  FileSiz  MemSiz  Flg  Align
        cols = line.split()
        if int(cols[1], 16) == 0:
            return int(cols[2], 16)
    return None


def symbols(path):
    """Parse the ELF's symbol tables into {name: (vaddr, size, kind)}.

    Reads both .symtab and .dynsym via `readelf -sW`. Undefined symbols (Ndx UND)
    are skipped, and the first defining entry for a name wins (so .symtab's fuller
    definitions aren't overwritten by a later duplicate). Versioned dynamic names
    like `memcpy@@GLIBC_2.14` are also indexed under their bare name `memcpy`, so a
    caller can resolve either form. Returns {} if the file can't be read.
    """
    out = _readelf(path, "-sW")
    if out is None:
        return {}
    table = {}
    for line in out.splitlines():
        cols = line.split()
        # Num:  Value  Size  Type  Bind  Vis  Ndx  Name
        if len(cols) < 8 or not cols[0].endswith(":"):
            continue
        if cols[6] == "UND":                    # undefined import; nothing to place
            continue
        name = cols[7]
        if name in table:
            continue
        try:
            vaddr = int(cols[1], 16)
            size = int(cols[2], 0)
        except ValueError:
            continue
        entry = (vaddr, size, cols[3])          # cols[3] is the ELF type (FUNC/OBJECT/...)
        table[name] = entry
        bare = name.split("@", 1)[0]            # strip @@VERSION / @VERSION
        if bare != name and bare not in table:
            table[bare] = entry
    return table


def debuglink(path):
    """Return the filename in the ELF's .gnu_debuglink section, or None.

    The section holds a NUL-terminated debug-file name followed by a CRC; we dump
    it with `readelf -x` and read bytes up to the first NUL. Used to locate a
    separate debug file when there's no build-id path.
    """
    out = _readelf(path, "-x", ".gnu_debuglink")
    if out is None:
        return None
    raw = bytearray()
    for line in out.splitlines():
        cols = line.split()
        # Hex-dump rows look like: 0xADDR  <up to 4 hex words>  <ascii gutter>
        if not cols or not cols[0].startswith("0x"):
            continue
        for word in cols[1:5]:
            if len(word) == 8 and all(c in "0123456789abcdefABCDEF" for c in word):
                raw += bytes.fromhex(word)
    name = bytes(raw).split(b"\x00", 1)[0]
    return name.decode("ascii", "replace") if name else None
