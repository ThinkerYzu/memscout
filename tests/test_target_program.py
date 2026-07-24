"""End-to-end test against a purpose-built C++ target with known ground truth.

Builds tests/fixtures/target.cpp, runs it, and checks that memscout finds exactly
the Widget objects the program itself reports (by address) and decodes each field
to the value the program printed. This exercises the whole pipeline on a real
process with a layout we fully control: attach, vtable symbol resolution, heap
scan, and every scalar/string decoder.

Skipped if no C++ compiler is available.
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import memscout
import memscout.cli


def _find_compiler():
    for name in ("c++", "g++", "clang++"):
        path = shutil.which(name)
        if path:
            return path
    return None


_CXX = _find_compiler()
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "fixtures", "target.cpp")

# The field specs matching target.cpp's documented layout.
_FIELDS = "8:bool:active 9:bool:hidden 12:i32:id 16:u64:value 24:nscstring:name"
_OBJ_RE = re.compile(
    r"OBJ (0x[0-9a-f]+) active=(\d) hidden=(\d) id=(\d+) value=(\d+) name=(\S+)")


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc + ELF")
@unittest.skipUnless(_CXX, "no C++ compiler found (c++/g++/clang++)")
class CppTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="memscout-cpp-")
        cls.exe = os.path.join(cls.workdir, "target")
        # Keep symbols (no -s) so _ZTV6Widget is in the local symbol table.
        subprocess.run([_CXX, _SRC, "-O0", "-o", cls.exe], check=True)

        cls.proc = subprocess.Popen([cls.exe], stdout=subprocess.PIPE, text=True)
        cls.truth = {}                          # address -> parsed ground-truth dict
        cls.pid = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = cls.proc.stdout.readline()
            if not line:
                break
            m = _OBJ_RE.match(line)
            if m:
                addr = int(m.group(1), 16)
                cls.truth[addr] = {
                    "active": int(m.group(2)), "hidden": int(m.group(3)),
                    "id": int(m.group(4)), "value": int(m.group(5)),
                    "name": m.group(6),
                }
            elif line.startswith("READY"):
                cls.pid = int(line.split()[1])
                break

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait()
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def setUp(self):
        self.assertIsNotNone(self.pid, "target program never reported READY")
        self.assertEqual(len(self.truth), 3, "expected 3 ground-truth objects")

    def test_resolve_vtable_symbol(self):
        with memscout.Target(self.pid) as t:
            sym = t.resolve("_ZTV6Widget")
            self.assertIsNotNone(sym, "_ZTV6Widget should resolve from the target's symtab")
            self.assertEqual(sym.kind, "vtable")

    def test_finds_exactly_the_reported_objects(self):
        with memscout.Target(self.pid) as t:
            needle = t.vtable("_ZTV6Widget")
            self.assertIsNotNone(needle)
            found = set(t.find_objects(needle))
        self.assertEqual(found, set(self.truth),
                         "memscout must find exactly the objects the program reported")

    def test_decodes_every_field_to_ground_truth(self):
        with memscout.Target(self.pid) as t:
            needle = t.vtable("_ZTV6Widget")
            for base in t.find_objects(needle):
                got = t.decode(base, _FIELDS)
                self.assertEqual(got, self.truth[base],
                                 "decoded fields must match the program's own report")

    def test_relocate_matches_symbol_resolution(self):
        # The reporter side gets (module, offset) from the developer and relocates it;
        # that must land at the same address symbol resolution produces on this build.
        with memscout.Target(self.pid) as t:
            sym = t.resolve("_ZTV6Widget")
            self.assertIsNotNone(sym)
            offset = sym.addr - sym.module.load_bias      # what the developer would ship
            self.assertEqual(t.relocate(sym.module.name, offset), sym.addr)
            self.assertIsNone(t.relocate("no-such-module.so", 0x10))

    def test_identify_class_from_vptr(self):
        with memscout.Target(self.pid) as t:
            for base in self.truth:
                self.assertEqual(t.identify_class(base), "Widget",
                                 "reverse vtable lookup should name the object's class")

    def test_dump_object_annotates_vtable_and_string(self):
        with memscout.Target(self.pid) as t:
            base = next(iter(self.truth))
            lines = list(t.dump_object(base))
        blob = "\n".join(lines)
        self.assertIn("vtable Widget", blob, "slot 0 should be annotated as the Widget vtable")
        # mData points at one of the string literals; its content should be shown.
        self.assertTrue(any(n in blob for n in ("alpha", "bravo", "charlie")),
                        "the string field's content should appear in the dump")

    def test_dump_cli_with_field_specs(self):
        # `dump <pid> <addr> OFF:TYPE:NAME ...` decodes via the same registry as scan.
        base = next(iter(self.truth))
        truth = self.truth[base]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            memscout.cli.main(["dump", str(self.pid), hex(base),
                               "12:i32:id", "24:nscstring:name"])
        text = out.getvalue()
        self.assertIn("(class Widget)", text)
        self.assertIn("id = %d" % truth["id"], text)
        self.assertIn("name = %r" % truth["name"], text)


if __name__ == "__main__":
    unittest.main()
