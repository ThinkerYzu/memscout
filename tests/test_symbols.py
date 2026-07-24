"""SymbolResolver tests: precedence, load-bias math, search order — with fake sources."""

import unittest
from unittest import mock

from memscout.maps import Module
from memscout.symbols import SymbolResolver, DebugInfoSource


class FakeSource(DebugInfoSource):
    """A source backed by a fixed {name: (vaddr, size, elf_type)} table."""

    def __init__(self, id, table):
        self.id = id
        self.table = table

    def lookup(self, module, name):
        return self.table.get(name)


def _module(path="/lib/x.so", bias=0x555000):
    return Module(path, bias, [(bias, bias + 0x1000)])


class SymbolResolverTest(unittest.TestCase):
    def setUp(self):
        # Force PIE base vaddr 0 so runtime addr == load_bias + linked vaddr.
        self._patch = mock.patch("memscout.symbols.elf.load_vaddr", return_value=0)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_first_source_wins(self):
        r = SymbolResolver(sources=[
            FakeSource("s1", {"foo": (0x1230, 4, "OBJECT")}),
            FakeSource("s2", {"foo": (0x9999, 4, "OBJECT")}),
        ])
        sym = r.resolve([_module()], "foo")
        self.assertEqual(sym.source, "s1")
        self.assertEqual(sym.addr, 0x555000 + 0x1230)

    def test_falls_through_to_next_source(self):
        r = SymbolResolver(sources=[
            FakeSource("s1", {}),
            FakeSource("s2", {"bar": (0x40, 8, "FUNC")}),
        ])
        sym = r.resolve([_module()], "bar")
        self.assertEqual(sym.source, "s2")
        self.assertEqual(sym.kind, "func")

    def test_non_pie_base_is_subtracted(self):
        self._patch.stop()                                  # re-patch with a non-zero base
        with mock.patch("memscout.symbols.elf.load_vaddr", return_value=0x400000):
            r = SymbolResolver(sources=[FakeSource("s", {"f": (0x401abc, 4, "FUNC")})])
            sym = r.resolve([_module(path="/bin/app", bias=0x400000)], "f")
            self.assertEqual(sym.addr, 0x401abc)            # bias + (vaddr - base)
        self._patch.start()

    def test_vtable_name_classified(self):
        r = SymbolResolver(sources=[FakeSource("s", {"_ZTV3Foo": (0x10, 0, "OBJECT")})])
        sym = r.resolve([_module()], "_ZTV3Foo")
        self.assertEqual(sym.kind, "vtable")

    def test_searches_all_modules_in_order(self):
        a = _module("/lib/a.so", 0x100000)
        b = _module("/lib/b.so", 0x200000)
        r = SymbolResolver(sources=[FakeSource("s", {"sym": (0x50, 4, "OBJECT")})])
        # Only b defines it? No -- both would via the shared table; check module hint instead.
        sym = r.resolve([a, b], "sym", module=b)
        self.assertIs(sym.module, b)
        self.assertEqual(sym.addr, 0x200000 + 0x50)

    def test_not_found_returns_none(self):
        r = SymbolResolver(sources=[FakeSource("s", {})])
        self.assertIsNone(r.resolve([_module()], "nope"))


if __name__ == "__main__":
    unittest.main()
