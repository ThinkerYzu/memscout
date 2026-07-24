"""End-to-end tests against a real (owned) child process.

These spawn a `sleep` we own, so /proc/<pid>/mem opens without SEIZE and no
special privileges are needed. They exercise the whole pipeline: attach, module
map, and symbol resolution through the real ELF symbol table. Skipped off Linux.
"""

import subprocess
import sys
import time
import unittest

import memscout


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc + ELF")
class LiveChildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(["sleep", "30"])
        time.sleep(0.3)                          # let the loader map libc etc.

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def test_modules_include_libc_with_build_id(self):
        with memscout.Target(self.proc.pid) as t:
            libc = t.module("libc.so.6")
            self.assertIsNotNone(libc, "libc should be mapped into sleep")
            self.assertTrue(libc.load_bias > 0)
            self.assertIsNotNone(libc.build_id, "libc should have a build-id")

    def test_resolve_malloc_lands_in_libc(self):
        with memscout.Target(self.proc.pid) as t:
            libc = t.module("libc.so.6")
            sym = t.resolve("malloc", module="libc.so.6")
            self.assertIsNotNone(sym, "malloc should resolve from libc's dynsym")
            self.assertEqual(sym.kind, "func")
            self.assertTrue(libc.contains(sym.addr),
                            "resolved malloc must fall inside libc's mapped range")

    def test_read_returns_none_off_the_end(self):
        with memscout.Target(self.proc.pid) as t:
            self.assertIsNone(t.read(0x1, 8))    # page 0 is never mapped


if __name__ == "__main__":
    unittest.main()
