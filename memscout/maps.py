"""Compatibility shim: the module map now lives in memscout.runtime.

Module / ModuleMap / parse / _is_elf moved to runtime.py (the single source of truth
for reporter-side primitives; build-ids are now read from the ELF note directly, no
readelf). This re-export keeps `from memscout.maps import ...` working.
"""

from .runtime import Module, ModuleMap, parse, _is_elf  # noqa: F401
