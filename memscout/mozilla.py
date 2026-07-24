"""Mozilla symbol server source: Breakpad `.sym` files from symbols.mozilla.org.

The Mozilla server keys files by a Breakpad debug-id (derived from the build-id)
and serves text `.sym` files. These carry FUNC/PUBLIC symbols with *demangled*
names -- and no vtables, globals, or line-free data symbols. So this source is
best at symbolizing an address to a function name; for a forward name lookup we
demangle the query and match the demangled entries (best-effort). Per SPEC
Resolved Decision 1, vtables/globals come from debuginfod or local debug info.
"""

import bisect
import os
import urllib.request

from . import cache, elf


def debug_id(build_id):
    """Breakpad debug-id (33-char) from an ELF build-id.

    The first 16 build-id bytes are read as a little-endian GUID (first three
    fields byte-swapped, the rest as-is), uppercased with no dashes, then an age
    nibble of '0' is appended -- the id Tecken uses in the `.sym` URL path.
    """
    b = build_id[:16]
    if len(b) < 16:
        b = b + b"\x00" * (16 - len(b))
    d1 = int.from_bytes(b[0:4], "little")
    d2 = int.from_bytes(b[4:6], "little")
    d3 = int.from_bytes(b[6:8], "little")
    return "%08X%04X%04X%s0" % (d1, d2, d3, b[8:16].hex().upper())


def parse_sym(text):
    """Parse a Breakpad `.sym` body into (name->(addr,size), sorted [(addr,name)]).

    Handles FUNC and PUBLIC records (with the optional `m` multiple marker).
    Names are demangled and may contain spaces, so everything past the fixed
    columns is the name. The sorted list backs address symbolization.
    """
    by_name = {}
    funcs = []                                  # (addr, name), sorted at the end
    for line in text.splitlines():
        cols = line.split()
        if not cols:
            continue
        if cols[0] == "FUNC":
            rest = cols[1:]
            if rest and rest[0] == "m":         # "FUNC m addr size param name"
                rest = rest[1:]
            if len(rest) < 4:
                continue
            addr = int(rest[0], 16)
            size = int(rest[1], 16)
            name = " ".join(rest[3:])
            by_name.setdefault(name, (addr, size))
            funcs.append((addr, name))
        elif cols[0] == "PUBLIC":
            rest = cols[1:]
            if rest and rest[0] == "m":          # "PUBLIC m addr param name"
                rest = rest[1:]
            if len(rest) < 3:
                continue
            addr = int(rest[0], 16)
            name = " ".join(rest[2:])
            by_name.setdefault(name, (addr, 0))
    funcs.sort()
    return by_name, funcs


class MozillaSymbols:
    """DebugInfoSource backed by Mozilla `.sym` files (functions/public symbols).

    A custom `fetcher(debugfile, debugid) -> path | None` can be injected for
    tests. Forward lookups match the demangled query against the demangled `.sym`
    names, so they resolve functions but not vtables/globals (absent by format).
    """

    id = "mozilla"

    def __init__(self, base_url="https://symbols.mozilla.org", fetcher=None):
        self.base_url = base_url
        self._fetch = fetcher or self._default_fetch
        self._tables = {}                       # module path -> (by_name, funcs)

    def lookup(self, module, name):
        by_name, _ = self._table(module)
        if not by_name:
            return None
        info = by_name.get(elf.demangle(name)) or by_name.get(name)
        if info is None:
            return None
        addr, size = info
        return (addr, size, "FUNC")

    def symbolize(self, module, offset):
        """Name of the function whose `.sym` range starts at or before `offset`, or None.

        `offset` is a module-relative address (runtime address minus load bias).
        """
        _, funcs = self._table(module)
        if not funcs:
            return None
        i = bisect.bisect_right(funcs, (offset, "\xff")) - 1
        return funcs[i][1] if i >= 0 else None

    def _table(self, module):
        if module.path not in self._tables:
            self._tables[module.path] = self._load(module)
        return self._tables[module.path]

    def _load(self, module):
        bid = module.build_id
        if not bid:
            return ({}, [])
        try:
            path = self._fetch(module.name, debug_id(bid))
        except Exception:
            path = None
        if not path:
            return ({}, [])
        with open(path, "r", errors="replace") as f:
            return parse_sym(f.read())

    def _default_fetch(self, debugfile, debugid):
        """Fetch <base>/<debugfile>/<debugid>/<debugfile>.sym into the cache, or None."""
        dest = cache.cache_path("mozilla", debugid, debugfile + ".sym")
        if os.path.exists(dest):
            return dest
        url = "%s/%s/%s/%s.sym" % (self.base_url, debugfile, debugid, debugfile)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                if resp.status != 200:
                    return None
                data = resp.read()
        except Exception:
            return None
        with open(dest, "wb") as f:
            f.write(data)
        return dest
