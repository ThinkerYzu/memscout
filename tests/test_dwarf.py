"""Level 2 tests: DWARF -> field specs (via gdb), and the full generate -> decode loop.

Builds small C++ programs with -g and checks that the offsets/types gdb reads match the
documented layout (including flattened inheritance offsets), that the specs decode correctly
against the live process, and (as a pure unit) that Firefox class names map to the right
decoder tokens.

Skipped without gdb or a C++ compiler.
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
from memscout import dwarf

_HAVE_GDB = shutil.which("gdb") is not None
_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo_target.cpp")
_LINE = re.compile(r"session (0x[0-9a-f]+) id=(\d+) active=(\d) requests=(\d+) user=(\S+)")


class FirefoxTokenMapTest(unittest.TestCase):
    """Pure mapping check for the Firefox class-name -> decoder-token heuristics (no gdb)."""

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
@unittest.skipUnless(_HAVE_GDB and _CXX, "needs gdb + C++ compiler")
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
        # the synthetic vtable slot must not leak in as a member
        self.assertNotIn("_vptr.Session", layout)

    def test_field_specs_selected_order(self):
        specs = dwarf.field_specs(self.exe, "Session", ["mId", "mActive", "mRequests"])
        self.assertEqual(specs, ["12:i32:mId", "8:bool:mActive", "16:u64:mRequests"])

    def test_missing_field_and_type_raise(self):
        with self.assertRaises(ValueError):
            dwarf.field_specs(self.exe, "Session", ["nope"])
        with self.assertRaises(ValueError):
            dwarf.struct_layout(self.exe, "NoSuchType")

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


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_HAVE_GDB and _CXX, "needs gdb + C++ compiler")
class InheritanceOffsetTest(unittest.TestCase):
    """Base-class members must be reported at their flattened (base + member) offset."""

    _CPP = (
        "#include <cstdint>\n"
        "struct Base { virtual ~Base(){} int32_t a; };\n"       # vptr@0, a@8
        "struct Mid : Base { int64_t b; };\n"                    # b@16
        "namespace ns { struct Derived : Mid { int32_t c; bool d; }; }\n"  # c@24, d@28
        "ns::Derived g;\n"
        "int main(){ return (int)(intptr_t)&g; }\n")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-inh-")
        src = os.path.join(cls.tmp, "inh.cpp")
        with open(src, "w") as f:
            f.write(cls._CPP)
        cls.exe = os.path.join(cls.tmp, "inh")
        subprocess.run([_CXX, "-g", "-O0", src, "-o", cls.exe], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_flattened_offsets_across_two_levels(self):
        layout = {m.name: (m.offset, m.token)
                  for m in dwarf.struct_layout(self.exe, "ns::Derived")}
        self.assertEqual(layout["a"], (8, "i32"))    # from Base
        self.assertEqual(layout["b"], (16, "i64"))   # from Mid
        self.assertEqual(layout["c"], (24, "i32"))   # from Derived
        self.assertEqual(layout["d"], (28, "bool"))


if __name__ == "__main__":
    unittest.main()
