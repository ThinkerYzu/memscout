"""Tests for the self-contained reporter runtime (memscout.runtime).

Covers the standalone Reporter facade against an owned child and the native
(readelf-free) build-id reader. These are the primitives `memscout bundle` ships.
"""

import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from memscout.runtime import Reporter, _read_build_id


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc + ELF")
class ReporterLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(["sleep", "30"])
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def test_modules_relocate_and_reads(self):
        with Reporter(self.proc.pid) as r:
            libc = r.module("libc.so.6")
            self.assertIsNotNone(libc, "libc should be mapped")
            self.assertIsNotNone(libc.build_id, "native reader should find libc's build-id")
            # relocate is load_bias + offset; matches the module's own base.
            self.assertEqual(r.relocate("libc.so.6", 0x1000), libc.load_bias + 0x1000)
            self.assertIsNone(r.relocate("no-such.so", 0))
            self.assertIsNone(r.read(0x1, 8))              # page 0 never mapped


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(shutil.which("c++") or shutil.which("g++"), "no C++ compiler")
class NativeBuildIdTest(unittest.TestCase):
    def test_matches_readelf(self):
        cxx = shutil.which("c++") or shutil.which("g++")
        tmp = tempfile.mkdtemp(prefix="memscout-bid-")
        try:
            exe = tmp + "/tiny"
            with open(tmp + "/t.c", "w") as src:
                src.write("int main(){return 0;}")
            subprocess.run([cxx, "-Wl,--build-id", tmp + "/t.c", "-o", exe], check=True)
            got = _read_build_id(exe)
            self.assertIsNotNone(got, "should read the build-id note we asked the linker to add")
            readelf = shutil.which("readelf")
            if readelf:
                out = subprocess.run([readelf, "-n", exe], capture_output=True, text=True).stdout
                for line in out.splitlines():
                    if "Build ID:" in line:
                        self.assertEqual(got.hex(), line.split(":", 1)[1].strip())
                        break
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
