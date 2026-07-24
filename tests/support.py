"""Shared test doubles.

FakeMemory is a sparse stand-in for a Target's read side: tests place named byte
segments at chosen addresses, so a decoder can follow pointers between structs
without any live process. It exposes the same read/read_uint/read_ptr surface the
decoders rely on.
"""

import struct


class FakeMemory:
    """A sparse, read-only memory image addressed by absolute address.

    place(addr, data) registers a segment; read() serves any range fully inside a
    single segment and returns None otherwise (matching MemorySource semantics).
    """

    def __init__(self):
        self._segments = []                     # list of (start, bytes)

    def place(self, addr, data):
        """Register `data` at absolute address `addr`; returns `addr` for chaining."""
        self._segments.append((addr, bytes(data)))
        return addr

    def read(self, addr, n):
        for start, data in self._segments:
            if start <= addr and addr + n <= start + len(data):
                off = addr - start
                return data[off:off + n]
        return None

    def read_uint(self, addr, size):
        b = self.read(addr, size)
        return None if b is None else int.from_bytes(b, "little")

    def read_ptr(self, addr):
        return self.read_uint(addr, 8)


def pack(*fields):
    """Concatenate (fmt, value) pairs into little-endian bytes.

    e.g. pack(("<Q", ptr), ("<I", length)) builds an 8-byte pointer + 4-byte length.
    """
    return b"".join(struct.pack(fmt, val) for fmt, val in fields)
