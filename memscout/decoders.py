"""Compatibility shim: the decoder registry now lives in memscout.runtime.

register / get / parse_spec / decode_field and all built-in decoders moved to
runtime.py (the single source of truth for reporter-side primitives). This re-export
keeps `from memscout import decoders` working; there is one shared registry.
"""

from .runtime import register, get, parse_spec, decode_field, registered_tokens  # noqa: F401
