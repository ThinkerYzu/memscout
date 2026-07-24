"""Unit tests for the pure-logic parts of the module map (no live process)."""

import os
import unittest

from memscout.maps import Module, ModuleMap


class ModuleTest(unittest.TestCase):
    def test_name_is_basename(self):
        m = Module("/usr/lib/firefox/libxul.so", 0x1000, [(0x1000, 0x2000)])
        self.assertEqual(m.name, "libxul.so")

    def test_contains_checks_all_ranges(self):
        m = Module("/x", 0, [(0x1000, 0x2000), (0x3000, 0x4000)])
        self.assertTrue(m.contains(0x1500))
        self.assertTrue(m.contains(0x3000))       # inclusive low bound
        self.assertFalse(m.contains(0x2000))      # exclusive high bound
        self.assertFalse(m.contains(0x2500))      # in the gap between ranges


class ModuleMapTest(unittest.TestCase):
    def setUp(self):
        self.a = Module("/usr/lib/libc.so.6", 0x1000, [(0x1000, 0x2000)])
        self.b = Module("/opt/app/libxul.so", 0x5000, [(0x5000, 0x9000)])
        self.mm = ModuleMap([self.a, self.b])

    def test_by_name_basename_and_suffix(self):
        self.assertIs(self.mm.by_name("libxul.so"), self.b)
        self.assertIs(self.mm.by_name("app/libxul.so"), self.b)
        self.assertIsNone(self.mm.by_name("libnss3.so"))

    def test_for_addr(self):
        self.assertIs(self.mm.for_addr(0x1500), self.a)
        self.assertIs(self.mm.for_addr(0x8000), self.b)
        self.assertIsNone(self.mm.for_addr(0x4000))


class ElfMagicFilterTest(unittest.TestCase):
    def test_python_binary_is_elf_our_test_module_is_not(self):
        from memscout.maps import _is_elf
        self.assertFalse(_is_elf(__file__))                  # this .py file
        self.assertFalse(_is_elf("/no/such/path"))           # unreadable
        # The running interpreter is a real ELF on Linux.
        self.assertTrue(_is_elf(os.path.realpath("/proc/self/exe")))


if __name__ == "__main__":
    unittest.main()
