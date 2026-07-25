"""Decoder registry tests, driven entirely off FakeMemory (no live process)."""

import io
import struct
import unittest
from contextlib import redirect_stdout

from memscout import cli, decoders
from support import FakeMemory, pack


class RegisteredTokensTest(unittest.TestCase):
    def test_lists_the_known_builtins(self):
        tokens = set(decoders.registered_tokens())
        # every built-in a script/spec may name must be enumerable, not source-only
        for tok in ("u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "bool", "ptr",
                    "atomic", "nsstring", "nscstring", "nscomptr", "refptr", "nstarray",
                    "pldhash", "mhashtable"):
            self.assertIn(tok, tokens)

    def test_sorted_and_matches_registry(self):
        toks = decoders.registered_tokens()
        self.assertEqual(toks, sorted(toks))
        # each listed token actually resolves to a decoder
        for tok in toks:
            self.assertIsNotNone(decoders.get(tok))

    def test_decoders_command_covers_whole_registry(self):
        # `memscout decoders` must describe every registered token (no undocumented ones)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["decoders"])
        out = buf.getvalue()
        for tok in decoders.registered_tokens():
            self.assertIn(tok, out)
        self.assertNotIn("(custom / undocumented)", out)


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


class Tier1DecoderTest(unittest.TestCase):
    def test_uniqueptr_and_owningnonnull_read_the_pointer(self):
        mem = FakeMemory()
        mem.place(0x4000, struct.pack("<Q", 0xCAFE0000))
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:uniqueptr:p")[1], 0xCAFE0000)
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:owningnonnull:p")[1], 0xCAFE0000)

    def test_nsatom_static(self):
        # nsStaticAtom: word0 = mLength | (mIsStatic<<30); chars at (atom - mStringOffset).
        mem = FakeMemory()
        atom, string_offset = 0x5000, 0x40
        mem.place(atom - string_offset, "div".encode("utf-16-le"))
        word0 = 3 | (1 << 30)
        mem.place(atom, pack(("<I", word0), ("<I", 0), ("<I", string_offset)))
        mem.place(0x4000, struct.pack("<Q", atom))          # RefPtr<nsAtom> member
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:nsatom:tag")[1], "div")

    def test_nsatom_dynamic(self):
        # nsDynamicAtom: is_static=0; StringBuffer* at +16; chars at buffer + 8.
        mem = FakeMemory()
        atom, buf = 0x6000, 0x7000
        mem.place(buf, b"\0" * 8 + "span".encode("utf-16-le"))  # 8-byte StringBuffer header
        mem.place(atom, pack(("<I", 4), ("<I", 0), ("<Q", 0), ("<Q", buf)))
        mem.place(0x4000, struct.pack("<Q", atom))
        self.assertEqual(decoders.decode_field(mem, 0x4000, "0:nsatom:tag")[1], "span")

    def test_nsatom_null_is_none(self):
        mem = FakeMemory()
        mem.place(0x4000, struct.pack("<Q", 0))
        self.assertIsNone(decoders.decode_field(mem, 0x4000, "0:nsatom:tag")[1])

    def test_maybe_engaged_points_at_storage(self):
        # Maybe<uint32_t>: value @ +0, char mIsSome @ +4 -> spec maybe:4.
        mem = FakeMemory()
        obj = 0x4000
        mem.place(obj, pack(("<I", 12345), ("<B", 1)))
        got = decoders.decode_field(mem, obj, "0:maybe:4:m")[1]
        self.assertEqual(got, {"engaged": True, "value": obj})

    def test_maybe_empty(self):
        mem = FakeMemory()
        obj = 0x4000
        mem.place(obj, pack(("<I", 0), ("<B", 0)))
        self.assertEqual(decoders.decode_field(mem, obj, "0:maybe:4:m")[1],
                         {"engaged": False, "value": 0})

    def test_maybe_without_flag_offset_is_flagged(self):
        mem = FakeMemory()
        mem.place(0x4000, pack(("<I", 1), ("<B", 1)))
        self.assertIn("needs flag offset", decoders.decode_field(mem, 0x4000, "0:maybe:m")[1])


class HashtableTest(unittest.TestCase):
    def test_pldhash_counts_and_enumerates_live(self):
        # PLDHashTable's EntryStore lays out hashes[capacity] (4 bytes each) first,
        # then entries[capacity] (entry_size bytes each) -- not interleaved per slot.
        # See PLDHashTable.h's EntryStore comment for why (avoids ABI padding).
        mem = FakeMemory()
        store = 0x9000
        entry_size, capacity = 16, 8
        hashes = bytearray(capacity * 4)
        for idx, keyhash in ((0, 5), (2, 9)):               # two live entries
            struct.pack_into("<I", hashes, idx * 4, keyhash)
        entries = bytearray(capacity * entry_size)
        mem.place(store, bytes(hashes) + bytes(entries))
        table = 0x4000
        # mOps, mEntryStore, mGeneration, mHashShift=29, mEntrySize=16, mEntryCount=2, mRemoved=0
        mem.place(table, pack(("<Q", 0), ("<Q", store), ("<H", 0), ("<B", 29),
                              ("<B", entry_size), ("<I", 2), ("<I", 0)))
        got = decoders.decode_field(mem, table, "0:pldhash:h")[1]
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["capacity"], capacity)         # 1 << (32 - 29)
        self.assertEqual(got["entry_size"], entry_size)
        entries_base = store + capacity * 4
        self.assertEqual(got["live"], [entries_base, entries_base + 2 * entry_size])

    def test_mhashtable_needs_entry_size_for_live(self):
        # mozilla::HashTable (mfbt) uses the same hashes-then-entries layout as
        # PLDHashTable (see mfbt/HashTable.h's HashTableEntry comment).
        mem = FakeMemory()
        table = 0x9000
        entry_size, capacity = 8, 8
        hashes = bytearray(capacity * 4)
        struct.pack_into("<I", hashes, 1 * 4, 7)            # one live entry at slot 1
        entries = bytearray(capacity * entry_size)
        mem.place(table, bytes(hashes) + bytes(entries))
        impl = 0x4000
        # mGenAndHashShift (low byte 29), mTable, mEntryCount=1, mRemoved=0
        mem.place(impl, pack(("<Q", 29), ("<Q", table), ("<I", 1), ("<I", 0)))

        entries_base = table + capacity * 4
        with_size = decoders.decode_field(mem, impl, "0:mhashtable:8:m")[1]
        self.assertEqual(with_size["count"], 1)
        self.assertEqual(with_size["capacity"], capacity)
        self.assertEqual(with_size["live"], [entries_base + entry_size])

        # Without an entry size, count/capacity still work; live can't be walked.
        no_size = decoders.decode_field(mem, impl, "0:mhashtable:m")[1]
        self.assertEqual(no_size["count"], 1)
        self.assertEqual(no_size["live"], [])


if __name__ == "__main__":
    unittest.main()
