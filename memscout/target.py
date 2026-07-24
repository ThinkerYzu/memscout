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

from . import maps
from .memory import MemorySource


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

    # --- heap region scanning ---

    def scan_regions(self, include_js=False):
        """Yield (lo, hi) for each writable region worth scanning for C++ objects.

        C++ objects live in the malloc heap ([anon:jemalloc] and plain anonymous
        mappings), so by default we skip the JS GC heap and file-backed mappings
        and any region over 256 MB. Pass include_js=True to widen the net.
        """
        for line in open("/proc/%d/maps" % self.pid):
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

    def find_pattern(self, needle8, include_js=False, limit=1000):
        """Return addresses where the 8-byte value `needle8` appears in scanned regions.

        Reads each writable region once and scans it in-process (fast), stopping
        at `limit` hits. The primary use is locating objects by their vtable
        pointer, but any 8-byte needle works.
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
