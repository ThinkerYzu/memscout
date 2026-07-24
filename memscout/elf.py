"""Thin wrappers over `readelf` for the ELF facts memscout needs.

Level 1 only needs a module's build-id (to key the remote symbol sources) and,
later, its symbol addresses and base vaddr. We shell out to binutils rather than
take a parsing dependency (DESIGN Decision 1); this module is the single place
that does so, so swapping in pyelftools at Level 2 stays a local change.
"""

import subprocess


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
