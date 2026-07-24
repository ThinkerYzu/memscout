"""Remote-source tests that stay offline by building real ELF fixtures.

- Debuginfod: an injected fetcher returns a real (unstripped) ELF, so the source's
  fetch->parse->lookup path is exercised without a debuginfod server.
- Success Criterion 4: a symbol absent from a *stripped* on-disk binary resolves
  through a separate debug file. That is exactly the LocalDebugFile path, proven
  end-to-end with objcopy/strip in a temp dir -- no network, no root.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from memscout import elf
from memscout.debuginfod import Debuginfod
from memscout.maps import Module
from memscout.symbols import LocalSymtab, LocalDebugFile, SymbolResolver


def _which_all(*names):
    return all(shutil.which(n) for n in names)


_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "fixtures", "target.cpp")
_VTABLE = "_ZTV6Widget"


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_CXX, "no C++ compiler")
class DebuginfodTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-dbgd-")
        cls.exe = os.path.join(cls.tmp, "target")
        subprocess.run([_CXX, _SRC, "-O0", "-o", cls.exe], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lookup_uses_fetched_debuginfo(self):
        mod = Module(self.exe, 0x400000, [(0x400000, 0x500000)])
        # Fetcher stands in for debuginfod, returning our real ELF for any build-id.
        src = Debuginfod(fetcher=lambda hexid: self.exe)
        info = src.lookup(mod, _VTABLE)
        self.assertIsNotNone(info, "vtable should resolve from fetched debuginfo")
        vaddr, size, elf_type = info
        self.assertGreater(vaddr, 0)

    def test_no_build_id_returns_none(self):
        mod = Module(self.exe, 0x400000, [(0x400000, 0x500000)])
        mod._build_id = None
        self.assertIsNone(Debuginfod(fetcher=lambda hexid: self.exe).lookup(mod, _VTABLE))

    def test_failed_fetch_is_not_fatal(self):
        mod = Module(self.exe, 0x400000, [(0x400000, 0x500000)])
        def boom(hexid):
            raise RuntimeError("network down")
        self.assertIsNone(Debuginfod(fetcher=boom).lookup(mod, _VTABLE))


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_CXX and _which_all("objcopy", "strip"), "needs c++ + binutils")
class StrippedBinaryResolutionTest(unittest.TestCase):
    """SC4: resolve a symbol that a stripped binary no longer carries."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-strip-")
        full = os.path.join(cls.tmp, "target_full")
        cls.stripped = os.path.join(cls.tmp, "target")
        cls.debug = os.path.join(cls.tmp, "target.debug")
        # hidden visibility keeps the vtable out of .dynsym, so strip fully removes it.
        subprocess.run([_CXX, _SRC, "-O0", "-fvisibility=hidden", "-o", full], check=True)
        shutil.copy(full, cls.stripped)
        subprocess.run(["objcopy", "--only-keep-debug", cls.stripped, cls.debug], check=True)
        subprocess.run(["strip", "--strip-all", cls.stripped], check=True)
        # add-gnu-debuglink stores the basename, so run it from the temp dir.
        subprocess.run(["objcopy", "--add-gnu-debuglink=target.debug", "target"],
                       cwd=cls.tmp, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _module(self):
        return Module(self.stripped, 0x400000, [(0x400000, 0x500000)])

    def test_symbol_absent_from_stripped_binary(self):
        self.assertIsNone(LocalSymtab().lookup(self._module(), _VTABLE),
                          "the stripped binary must not carry the vtable symbol")

    def test_resolves_via_separate_debug_file(self):
        r = SymbolResolver(sources=[LocalSymtab(), LocalDebugFile()])
        sym = r.resolve([self._module()], _VTABLE)
        self.assertIsNotNone(sym, "should resolve from the .gnu_debuglink debug file")
        self.assertEqual(sym.source, "local-debug")
        self.assertEqual(sym.kind, "vtable")


if __name__ == "__main__":
    unittest.main()
