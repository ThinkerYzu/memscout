"""Decoder registry tests, driven entirely off FakeMemory (no live process)."""

import struct
import unittest

from memscout import decoders
from support import FakeMemory, pack


class SpecParseTest(unittest.TestCase):
    def test_plain_spec(self):
        self.assertEqual(decoders.parse_spec("40:bool:mLocked"), (40, "bool", "mLocked"))

    def test_hex_offset(self):
        self.assertEqual(decoders.parse_spec("0x10:u32:x"), (16, "u32", "x"))

    def test_type_with_arg_keeps_colon(self):
        self.assertEqual(decoders.parse_spec("8:atomic:u32:mState"),
                         (8, "atomic:u32", "mState"))
        self.assertEqual(decoders.parse_spec("0:mhashtable:24:mMap"),
                         (0, "mhashtable:24", "mMap"))

    def test_too_few_parts_raises(self):
        with self.assertRaises(ValueError):
            decoders.parse_spec("40:bool")


class PrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.base = 0x4000
        self.mem.place(self.base, pack(("<I", 0xFFFFFFFF), ("<I", 1),
                                       ("<Q", 0xCAFEF00D)))

    def decode(self, spec):
        return decoders.decode_field(self.mem, self.base, spec)[1]

    def test_u32(self):
        self.assertEqual(self.decode("0:u32:x"), 0xFFFFFFFF)

    def test_i32_sign_extends(self):
        self.assertEqual(self.decode("0:i32:x"), -1)

    def test_bool(self):
        self.assertEqual(self.decode("4:bool:x"), 1)

    def test_ptr_and_u64(self):
        self.assertEqual(self.decode("8:ptr:x"), 0xCAFEF00D)
        self.assertEqual(self.decode("8:u64:x"), 0xCAFEF00D)

    def test_atomic_dispatches_to_underlying(self):
        self.assertEqual(self.decode("0:atomic:u32:x"), 0xFFFFFFFF)

    def test_unreadable_returns_none(self):
        self.assertIsNone(self.decode("0x1000:u32:x"))     # outside any segment

    def test_bad_type(self):
        self.assertEqual(self.decode("0:nope:x"), "<bad-type:nope>")


class StringTest(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()

    def test_nsstring_utf16(self):
        buf = 0x9000
        self.mem.place(buf, "hello".encode("utf-16-le"))
        obj = 0x4000
        self.mem.place(obj, pack(("<Q", buf), ("<I", 5)))
        self.assertEqual(decoders.decode_field(self.mem, obj, "0:nsstring:s")[1], "hello")

    def test_nscstring_utf8(self):
        buf = 0x9000
        self.mem.place(buf, b"video-playing")
        obj = 0x4000
        self.mem.place(obj, pack(("<Q", buf), ("<I", 13)))
        self.assertEqual(decoders.decode_field(self.mem, obj, "0:nscstring:s")[1],
                         "video-playing")

    def test_absurd_length_is_flagged_not_read(self):
        obj = 0x4000
        self.mem.place(obj, pack(("<Q", 0x9000), ("<I", 10 ** 9)))
        self.assertIn("?", decoders.decode_field(self.mem, obj, "0:nsstring:s")[1])


class ContainerTest(unittest.TestCase):
    def test_refptr(self):
        mem = FakeMemory()
        mem.place(0x4000, struct.pack("<Q", 0xDEAD0000))
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:refptr:p")[1], 0xDEAD0000)

    def test_nstarray(self):
        mem = FakeMemory()
        hdr = 0x9000
        mem.place(hdr, struct.pack("<I", 3))
        mem.place(0x4000, struct.pack("<Q", hdr))
        got = decoders.decode_field(mem, 0x4000, "0:nstarray:a")[1]
        self.assertEqual(got, {"length": 3, "data": hdr + 8})

    def test_nstarray_null_is_empty(self):
        mem = FakeMemory()
        mem.place(0x4000, struct.pack("<Q", 0))
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:nstarray:a")[1],
                         {"length": 0, "data": 0})


class HashtableTest(unittest.TestCase):
    def test_pldhash_counts_and_enumerates_live(self):
        mem = FakeMemory()
        store = 0x9000
        entry_size, capacity = 16, 8
        slots = bytearray(capacity * entry_size)
        for idx, keyhash in ((0, 5), (2, 9)):               # two live entries
            struct.pack_into("<I", slots, idx * entry_size, keyhash)
        mem.place(store, slots)
        table = 0x4000
        # mOps, mEntryStore, mGeneration, mHashShift=29, mEntrySize=16, mEntryCount=2, mRemoved=0
        mem.place(table, pack(("<Q", 0), ("<Q", store), ("<H", 0), ("<B", 29),
                              ("<B", entry_size), ("<I", 2), ("<I", 0)))
        got = decoders.decode_field(mem, table, "0:pldhash:h")[1]
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["capacity"], capacity)         # 1 << (32 - 29)
        self.assertEqual(got["entry_size"], entry_size)
        self.assertEqual(got["live"], [store, store + 2 * entry_size])

    def test_mhashtable_needs_entry_size_for_live(self):
        mem = FakeMemory()
        table = 0x9000
        entry_size, capacity = 8, 8
        slots = bytearray(capacity * entry_size)
        struct.pack_into("<I", slots, 1 * entry_size, 7)    # one live entry at slot 1
        mem.place(table, slots)
        impl = 0x4000
        # mGenAndHashShift (low byte 29), mTable, mEntryCount=1, mRemoved=0
        mem.place(impl, pack(("<Q", 29), ("<Q", table), ("<I", 1), ("<I", 0)))

        with_size = decoders.decode_field(mem, impl, "0:mhashtable:8:m")[1]
        self.assertEqual(with_size["count"], 1)
        self.assertEqual(with_size["capacity"], capacity)
        self.assertEqual(with_size["live"], [table + entry_size])

        # Without an entry size, count/capacity still work; live can't be walked.
        no_size = decoders.decode_field(mem, impl, "0:mhashtable:m")[1]
        self.assertEqual(no_size["count"], 1)
        self.assertEqual(no_size["live"], [])


if __name__ == "__main__":
    unittest.main()
