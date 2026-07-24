"""Debuginfod symbol source: fetch a module's debug ELF by build-id.

debuginfod serves full ELF/DWARF debug info keyed by build-id. We prefer the
`debuginfod-find` client when it's installed (it reuses DEBUGINFOD_URLS and the
elfutils cache); otherwise we fetch over HTTP into our own cache. Either way, the
fetched file is a normal ELF whose symbols share the module's link addresses, so
resolution goes through the same elf.symbols() path as the local sources.
"""

import os
import shutil
import subprocess
import urllib.request

from . import cache, elf


class Debuginfod:
    """DebugInfoSource that resolves names from debuginfod-fetched debug info.

    A custom `fetcher(build_id_hex) -> path | None` can be injected (tests use
    this to avoid the network); the default tries `debuginfod-find`, then HTTP
    against DEBUGINFOD_URLS.
    """

    id = "debuginfod"

    def __init__(self, urls=None, fetcher=None):
        self.urls = urls if urls is not None else os.environ.get("DEBUGINFOD_URLS", "")
        self._fetch = fetcher or self._default_fetch
        self._tables = {}                       # build-id hex -> symbol table (or {})

    def lookup(self, module, name):
        bid = module.build_id
        if not bid:
            return None
        return self._symbols(bid.hex()).get(name)

    def _symbols(self, hexid):
        if hexid not in self._tables:
            path = None
            try:
                path = self._fetch(hexid)
            except Exception:
                path = None                     # network/tool failure -> treat as "no info"
            self._tables[hexid] = elf.symbols(path) if path else {}
        return self._tables[hexid]

    def _default_fetch(self, hexid):
        """Return a path to the debuginfo ELF for `hexid`, or None if unavailable."""
        finder = shutil.which("debuginfod-find")
        if finder and (self.urls or os.environ.get("DEBUGINFOD_URLS")):
            out = subprocess.run([finder, "debuginfo", hexid],
                                 capture_output=True, text=True)
            path = out.stdout.strip()
            if out.returncode == 0 and path and os.path.exists(path):
                return path
        return self._http_fetch(hexid)

    def _http_fetch(self, hexid):
        """Fetch <server>/buildid/<hexid>/debuginfo over HTTP into the cache, or None."""
        if not self.urls:
            return None
        dest = cache.cache_path("debuginfod", hexid, "debuginfo")
        if os.path.exists(dest):
            return dest
        for server in self.urls.split():
            url = "%s/buildid/%s/debuginfo" % (server.rstrip("/"), hexid)
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    if resp.status != 200:
                        continue
                    data = resp.read()
            except Exception:
                continue
            with open(dest, "wb") as f:
                f.write(data)
            return dest
        return None
