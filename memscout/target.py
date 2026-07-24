"""Target: the full inspection facade — reporter primitives + developer-side extras.

`Reporter` (memscout.runtime) provides the reporter-side core: attach, read, module map,
relocate, heap scan, decode, dump_slots. `Target` subclasses it and adds the pieces that
need symbols — `resolve()`/`vtable()` and the class-aware object dump — which rely on
readelf / debug info and so belong to the developer/authoring side.

    with memscout.Target(pid) as t:
        needle = t.vtable("_ZTVN7mozilla3dom8WakeLockE", module="libxul.so")
        for base in t.find_objects(needle):
            print(t.identify_class(base), t.decode(base, "40:bool:mLocked"))
"""

from . import elf
from .runtime import Reporter, _PTR_LO, _PTR_HI
from .symbols import SymbolResolver


class Target(Reporter):
    """A read-only view of a running process with symbol resolution on top of Reporter."""

    def __init__(self, pid):
        super().__init__(pid)
        self._resolver = SymbolResolver()
        self._vtable_maps = {}          # module path -> {runtime vptr needle: class name}

    # --- symbol resolution (developer-side) ---

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

    # --- object content printing (class id + annotated slots) ---

    def identify_class(self, base):
        """Class name of the object at `base` from its vptr, or None.

        Reads the object's vtable pointer and reverse-resolves it against the vtable
        symbols of the module it points into (e.g. -> "mozilla::dom::WakeLock").
        """
        vptr = self.read_ptr(base)
        if not vptr:
            return None
        module = self.modules.for_addr(vptr)
        if module is None:
            return None
        return self._vtable_map(module).get(vptr)

    def dump_object(self, base, count=12):
        """Yield annotated lines for the first `count` 8-byte slots at `base`.

        Each slot shows its raw value plus a best-effort hint: a string behind the
        pointer (ASCII or UTF-16), a vtable pointer named by its class, or a plain
        pointer into a known module as `module+offset`. Purely descriptive.
        """
        obj = self._mem.read(base, count * 8)
        if not obj:
            yield "  <unreadable object>"
            return
        for o in range(0, count * 8, 8):
            val = int.from_bytes(obj[o:o + 8], "little")
            ann = self._annotate(val)
            yield "  +%3d: %#018x%s" % (o, val, ("  " + ann) if ann else "")

    def _annotate(self, value):
        """A display hint for a slot value: string, vtable class, or module+offset."""
        if not (_PTR_LO < value < _PTR_HI):
            return None
        text = self._string_at(value)
        if text:
            return "-> " + text
        module = self.modules.for_addr(value)
        if module is None:
            return None
        cls = self._vtable_map(module).get(value)
        if cls:
            return "vtable " + cls
        return "%s+%#x" % (module.name, value - module.load_bias)

    def _string_at(self, addr):
        """Return `ascii "..."` / `u16 "..."` if a readable string sits at addr, else None."""
        data = self._mem.read(addr, 48)
        if not data:
            return None
        end = data.find(b"\x00")
        head = data[:end] if end != -1 else data
        if len(head) >= 4 and all(32 <= b < 127 for b in head[:32]):
            return 'ascii "%s"' % head[:40].decode("ascii", "replace")
        printable = "".join(c for c in data.decode("utf-16-le", "ignore") if 32 <= ord(c) < 127)
        if len(printable) >= 3:
            return 'u16 "%s"' % printable[:40]
        return None

    def _vtable_map(self, module):
        """Cached {runtime vptr needle -> class name} for one module's vtable symbols.

        Inverts the module's `_ZTV*` symbols: each maps to the value an object of that
        class stores at its vptr slot (symbol address + 16 header). Parses the module's
        symbol table once (cached thereafter) -- hence developer-side.
        """
        if module.path in self._vtable_maps:
            return self._vtable_maps[module.path]
        base = elf.load_vaddr(module.path) or 0
        vmap = {}
        for name, (vaddr, _size, _type) in elf.symbols(module.path).items():
            if not name.startswith("_ZTV"):
                continue
            needle = module.load_bias + (vaddr - base) + 16
            cls = elf.demangle(name)
            if cls.startswith("vtable for "):
                cls = cls[len("vtable for "):]
            vmap[needle] = cls
        self._vtable_maps[module.path] = vmap
        return vmap
