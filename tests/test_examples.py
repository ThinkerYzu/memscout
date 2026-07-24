"""End-to-end test of the worked example (examples/author.py + collect.py).

Builds examples/demo_target.cpp, runs it, drives the developer step (author) and the
reporter step (collect), and checks the resulting JSON-lines log against the app's own
printed ground truth. Guards the example scripts from bitrot. Skipped without a compiler.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "examples")
sys.path.insert(0, _EXAMPLES)

_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_SRC = os.path.join(_EXAMPLES, "demo_target.cpp")
_LINE = re.compile(r"session (0x[0-9a-f]+) id=(\d+) active=(\d) requests=(\d+) user=(\S+)")


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_CXX, "no C++ compiler")
class WorkedExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-example-")
        cls.exe = os.path.join(cls.tmp, "demo_target")
        subprocess.run([_CXX, _SRC, "-O0", "-o", cls.exe], check=True)
        cls.proc = subprocess.Popen([cls.exe], stdout=subprocess.PIPE, text=True)
        cls.truth = {}                          # addr -> ground-truth dict
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
                    "mRequests": int(m.group(4)), "mUser": m.group(5)}
            elif line.startswith("READY"):
                cls.pid = int(line.split("=")[1])
                break

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_author_then_collect_matches_ground_truth(self):
        import author
        import collect

        self.assertIsNotNone(self.pid, "demo_target never reported READY")
        self.assertEqual(len(self.truth), 3)

        config = author.author(self.pid, "_ZTV7Session",
                               ["8:bool:mActive", "12:i32:mId",
                                "16:u64:mRequests", "24:nscstring:mUser"])
        self.assertEqual(config["module"], "demo_target")
        self.assertIsNotNone(config["build_id"])

        log_path = os.path.join(self.tmp, "sessions.jsonl")
        count, build_ok = collect.collect(self.pid, config, log_path)
        self.assertEqual(count, 3)
        self.assertTrue(build_ok, "same process -> build-id must match")

        with open(log_path) as f:
            records = [json.loads(line) for line in f]
        meta, objects = records[0], records[1:]
        self.assertEqual(meta["type"], "meta")
        self.assertEqual(meta["count"], 3)

        by_addr = {int(o["addr"], 16): o for o in objects}
        self.assertEqual(set(by_addr), set(self.truth),
                         "logged objects must be exactly the ones the app allocated")
        for addr, truth in self.truth.items():
            got = by_addr[addr]
            for field, value in truth.items():
                self.assertEqual(got[field], value,
                                 "logged %s must match the app's ground truth" % field)


if __name__ == "__main__":
    unittest.main()
