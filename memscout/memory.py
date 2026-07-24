"""Compatibility shim: the memory primitives now live in memscout.runtime.

MemorySource moved to runtime.py (the single source of truth for reporter-side
primitives). This re-export keeps `from memscout.memory import MemorySource` working.
"""

from .runtime import MemorySource, _ptrace_seize  # noqa: F401
