"""Symbol resolution: a name -> runtime address, across loaded modules.

A SymbolResolver walks an ordered list of DebugInfoSource objects and returns the
first that can place the name. Each source shares one small interface, so the
precedence is declarative and a new source (debuginfod, Mozilla) is a one-line
addition (Phase 2.3). Level 2.2 ships the two local sources.
"""

import os

from . import elf


class Symbol:
    """A resolved symbol: its runtime address plus where and how it was found.

    addr is already load-bias-adjusted (a real address in the target). kind is a
    best-effort classification (func/object/vtable/...); source names the
    DebugInfoSource that produced it.
    """

    def __init__(self, name, addr, module, kind, size, source):
        self.name = name
        self.addr = addr
        self.module = module
        self.kind = kind
        self.size = size
        self.source = source

    def __repr__(self):
        return "<Symbol %s @ %#x %s in %s via %s>" % (
            self.name, self.addr, self.kind, self.module.name, self.source)


def _classify(name, elf_type):
    """Best-effort symbol kind from its name and ELF type."""
    if name.startswith("_ZTV"):
        return "vtable"
    return {"FUNC": "func", "OBJECT": "object"}.get(elf_type, elf_type.lower())


class DebugInfoSource:
    """One place symbols can come from. lookup returns (vaddr, size, elf_type) or None."""

    id = "?"

    def lookup(self, module, name):
        raise NotImplementedError


class LocalSymtab(DebugInfoSource):
    """Symbols straight from the module's own on-disk .symtab/.dynsym.

    Parses each module's symbol table once and caches it (Decision 3): a big
    `readelf -sW libxul.so` runs at most once per module per process.
    """

    id = "local-symtab"

    def __init__(self):
        self._cache = {}                        # module path -> {name: (vaddr,size,type)}

    def _table(self, path):
        if path not in self._cache:
            self._cache[path] = elf.symbols(path)
        return self._cache[path]

    def lookup(self, module, name):
        return self._table(module.path).get(name)


class LocalDebugFile(DebugInfoSource):
    """Symbols from a separate local debug file (build-id path or .gnu_debuglink).

    Stripped release binaries keep their symbols in a companion .debug file. We
    look first at the standard build-id path (/usr/lib/debug/.build-id/xx/rest.debug),
    then at the .gnu_debuglink name beside the binary and under /usr/lib/debug.
    """

    id = "local-debug"

    def __init__(self):
        self._table_cache = {}                  # debug file path -> symbol table
        self._path_cache = {}                   # module path -> debug file path | None

    def _debug_path(self, module):
        if module.path in self._path_cache:
            return self._path_cache[module.path]
        found = self._find_debug_file(module)
        self._path_cache[module.path] = found
        return found

    @staticmethod
    def _find_debug_file(module):
        bid = module.build_id
        if bid:
            hexid = bid.hex()
            p = "/usr/lib/debug/.build-id/%s/%s.debug" % (hexid[:2], hexid[2:])
            if os.path.exists(p):
                return p
        link = elf.debuglink(module.path)
        if link:
            bindir = os.path.dirname(module.path)
            for d in (bindir, os.path.join(bindir, ".debug"), "/usr/lib/debug" + bindir):
                p = os.path.join(d, link)
                if os.path.exists(p):
                    return p
        return None

    def lookup(self, module, name):
        path = self._debug_path(module)
        if not path:
            return None
        if path not in self._table_cache:
            self._table_cache[path] = elf.symbols(path)
        return self._table_cache[path].get(name)


def _default_sources():
    """The standard source precedence: local first, then remote (SPEC Decision 3).

    Imported lazily so a bare SymbolResolver (or the offline unit tests) doesn't
    pull in the remote-source modules until they're actually used.
    """
    from .debuginfod import Debuginfod
    from .mozilla import MozillaSymbols
    return [LocalSymtab(), LocalDebugFile(), Debuginfod(), MozillaSymbols()]


class SymbolResolver:
    """Resolves names to Symbols by trying each source in precedence order.

    Default precedence is local .symtab/.dynsym, then a separate local debug file,
    then debuginfod, then the Mozilla symbol server. Pass `sources` to override
    (used by tests).
    """

    def __init__(self, sources=None):
        self.sources = sources if sources is not None else _default_sources()
        self._base_vaddr = {}                   # module path -> ELF base vaddr (cached)

    def _base(self, module):
        if module.path not in self._base_vaddr:
            self._base_vaddr[module.path] = elf.load_vaddr(module.path) or 0
        return self._base_vaddr[module.path]

    def resolve(self, modules, name, module=None):
        """Return a Symbol for `name`, or None. Searches all modules if `module` is None.

        The runtime address is load_bias + (linked_vaddr - base_vaddr); for PIE
        modules base_vaddr is 0 and this reduces to load_bias + vaddr.
        """
        candidates = [module] if module is not None else list(modules)
        for m in candidates:
            base = self._base(m)
            for src in self.sources:
                info = src.lookup(m, name)
                if info is not None:
                    vaddr, size, elf_type = info
                    addr = m.load_bias + (vaddr - base)
                    return Symbol(name, addr, m, _classify(name, elf_type), size, src.id)
        return None
