"""Level 2 tests: DWARF -> field specs, and the full spec-generate -> decode loop.

Builds the example app with -g and checks that the offsets/types pyelftools reads match
the documented layout, that the specs it emits decode correctly against the live process,
and (as a pure unit) that Firefox class names map to the right decoder tokens.

Skipped without pyelftools or a C++ compiler.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import memscout

try:
    from memscout import dwarf
    _HAVE = dwarf._HAVE_PYELFTOOLS
except Exception:
    _HAVE = False

_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo_target.cpp")
_LINE = re.compile(r"session (0x[0-9a-f]+) id=(\d+) active=(\d) requests=(\d+) user=(\S+)")


@unittest.skipUnless(_HAVE, "needs pyelftools")
class FirefoxTokenMapTest(unittest.TestCase):
    """Pure mapping check for the Firefox class-name -> decoder-token heuristics."""

    def test_known_classes(self):
        cases = {
            "nsString": "nsstring", "nsCString": "nscstring",
            "nsTString<char16_t>": "nsstring", "nsTString<char>": "nscstring",
            "nsTArray<int>": "nstarray", "AutoTArray<int, 8>": "nstarray",
            "RefPtr<Foo>": "refptr", "nsCOMPtr<nsIFoo>": "nscomptr",
            "nsTHashMap<nsCStringHashKey, int>": "pldhash",
            "nsClassHashtable<K, V>": "pldhash",
            "mozilla::HashMap<K, V>": "mhashtable",
        }
        for name, token in cases.items():
            self.assertEqual(dwarf._firefox_token(name), token, name)
        self.assertIsNone(dwarf._firefox_token("SomeRandomClass"))


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_HAVE and _CXX, "needs pyelftools + C++ compiler")
class DwarfLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-dwarf-")
        cls.exe = os.path.join(cls.tmp, "demo_target_g")
        subprocess.run([_CXX, "-g", "-O0", _SRC, "-o", cls.exe], check=True)
        cls.proc = subprocess.Popen([cls.exe], stdout=subprocess.PIPE, text=True)
        cls.truth = {}
        cls.pid = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = cls.proc.stdout.readline()
            if not line:
                break
            m = _LINE.match(line)
            if m:
                cls.truth[int(m.group(1), 16)] = {
                    "mId": int(m.group(2)), "mActive": int(m.group(3)),
                    "mRequests": int(m.group(4))}
            elif line.startswith("READY"):
                cls.pid = int(line.split("=")[1])
                break

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_layout_offsets_and_tokens(self):
        layout = {m.name: m for m in dwarf.struct_layout(self.exe, "Session")}
        expected = {"mActive": (8, "bool"), "mId": (12, "i32"),
                    "mRequests": (16, "u64"), "mUser": (24, "ptr"),
                    "mUserLen": (32, "u32")}
        for name, (offset, token) in expected.items():
            self.assertIn(name, layout)
            self.assertEqual((layout[name].offset, layout[name].token), (offset, token), name)

    def test_field_specs_selected_order(self):
        specs = dwarf.field_specs(self.exe, "Session", ["mId", "mActive", "mRequests"])
        self.assertEqual(specs, ["12:i32:mId", "8:bool:mActive", "16:u64:mRequests"])

    def test_generated_specs_decode_against_live_process(self):
        # The whole point: specs computed from DWARF decode correctly at runtime.
        specs = dwarf.field_specs(self.exe, "Session", ["mActive", "mId", "mRequests"])
        with memscout.Target(self.pid) as t:
            needle = t.vtable("_ZTV7Session")
            bases = t.find_objects(needle)
            self.assertEqual(len(bases), 3)
            for base in bases:
                got = t.decode(base, specs)
                self.assertEqual(got, self.truth[base],
                                 "DWARF-derived specs must decode to the app's ground truth")


if __name__ == "__main__":
    unittest.main()
