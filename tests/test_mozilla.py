"""Mozilla source tests: debug-id derivation, .sym parsing, lookup, symbolize.

All offline: the fetcher is injected to return a local .sym fixture, so no network.
"""

import os
import shutil
import tempfile
import unittest

from memscout.maps import Module
from memscout import mozilla


_SYM = """MODULE Linux x86_64 0123456789ABCDEF0 libtest.so
FILE 0 /src/a.cpp
FUNC 1a2b 40 0 foo(int)
FUNC m 2c3d 10 0 Bar::baz()
PUBLIC 3e4f 0 some_public_symbol
"""


class DebugIdTest(unittest.TestCase):
    def test_derivation_byte_swaps_and_appends_age(self):
        bid = bytes(range(16))              # 00 01 02 ... 0f
        # d1=LE(00010203)=03020100, d2=LE(0405)=0504, d3=LE(0607)=0706, rest as-is, +age 0
        self.assertEqual(mozilla.debug_id(bid), "030201000504070608090A0B0C0D0E0F0")

    def test_short_build_id_is_padded(self):
        self.assertEqual(len(mozilla.debug_id(b"\x01\x02\x03")), 33)


class ParseSymTest(unittest.TestCase):
    def test_parses_func_and_public(self):
        by_name, funcs = mozilla.parse_sym(_SYM)
        self.assertEqual(by_name["foo(int)"], (0x1a2b, 0x40))
        self.assertEqual(by_name["Bar::baz()"], (0x2c3d, 0x10))   # 'm' marker handled
        self.assertEqual(by_name["some_public_symbol"], (0x3e4f, 0))
        self.assertEqual(funcs, [(0x1a2b, "foo(int)"), (0x2c3d, "Bar::baz()")])


class MozillaSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memscout-moz-")
        self.sym = os.path.join(self.tmp, "libtest.so.sym")
        with open(self.sym, "w") as f:
            f.write(_SYM)
        # A module with any build-id; the injected fetcher ignores the id.
        self.mod = Module("/x/libtest.so", 0x400000, [(0x400000, 0x500000)])
        self.mod._build_id = b"\x01\x02\x03\x04"
        self.src = mozilla.MozillaSymbols(fetcher=lambda df, di: self.sym)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_forward_lookup_by_demangled_name(self):
        # c++filt leaves an already-demangled string unchanged, so this needs no c++filt.
        self.assertEqual(self.src.lookup(self.mod, "foo(int)"), (0x1a2b, 0x40, "FUNC"))
        self.assertEqual(self.src.lookup(self.mod, "some_public_symbol"), (0x3e4f, 0, "FUNC"))

    def test_missing_symbol_returns_none(self):
        self.assertIsNone(self.src.lookup(self.mod, "nonexistent"))

    def test_symbolize_finds_enclosing_function(self):
        self.assertEqual(self.src.symbolize(self.mod, 0x2c40), "Bar::baz()")
        self.assertEqual(self.src.symbolize(self.mod, 0x1a2b), "foo(int)")
        self.assertIsNone(self.src.symbolize(self.mod, 0x1000))   # before any func

    @unittest.skipUnless(shutil.which("c++filt"), "needs c++filt for mangled lookup")
    def test_mangled_query_is_demangled(self):
        # _Z3fooi demangles to "foo(int)".
        self.assertEqual(self.src.lookup(self.mod, "_Z3fooi"), (0x1a2b, 0x40, "FUNC"))

    def test_no_build_id_is_empty(self):
        mod = Module("/x/y.so", 0, [(0, 1)])
        mod._build_id = None
        self.assertIsNone(mozilla.MozillaSymbols(fetcher=lambda a, b: self.sym).lookup(mod, "foo(int)"))


if __name__ == "__main__":
    unittest.main()
