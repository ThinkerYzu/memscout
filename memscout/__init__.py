"""memscout: a lightweight, read-only runtime data-collection framework.

Inspect the live internal state of a running Linux process by reading its memory
through /proc/<pid>/mem -- no debugger, and the target is never stopped. Import
Target and compose the primitives:

    import memscout
    with memscout.Target(pid) as t:
        for m in t.modules:
            print(m.name, hex(m.load_bias))

Symbol resolution and field decoding arrive in later phases; Phase 2.1 provides
the read/enumerate/scan core.
"""

from .target import Target
from .maps import Module, ModuleMap

__version__ = "0.1.0"
__all__ = ["Target", "Module", "ModuleMap", "__version__"]
