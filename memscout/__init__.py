"""memscout: a lightweight, read-only runtime data-collection framework.

Inspect the live internal state of a running Linux process by reading its memory
through /proc/<pid>/mem -- no debugger, and the target is never stopped.

`Target` is the developer-side entry point: symbol resolution, heap scanning, and
field decoding. `Reporter` is the subset that needs no symbols or DWARF, for
scripts that run on someone else's machine -- import that one as
`from memscout.runtime import Reporter`, which is the form `memscout bundle`
inlines. `register_decoder` adds field types (`register` in memscout.runtime).

    from memscout import Target

    with Target(pid) as t:
        needle = t.vtable("_ZTV7Session", module="demo_target")
        for base in t.find_objects(needle):
            print(t.identify_class(base), t.decode(base, "12:i32:mId"))
"""

from .target import Target
from .runtime import Reporter, Module, ModuleMap, register as register_decoder

__version__ = "0.1.0"
__all__ = ["Target", "Reporter", "Module", "ModuleMap", "register_decoder", "__version__"]
