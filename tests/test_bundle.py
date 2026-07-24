"""Test `memscout bundle`: the bundled collection script is truly self-contained.

Builds the demo app, authors a config, bundles examples/collect.py, then runs the
bundled file in a subprocess where memscout is NOT importable (empty PYTHONPATH, cwd
outside the repo) -- proving it needs only a stock Python. Skipped without a compiler.
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
_REPO = os.path.dirname(_HERE)
_EXAMPLES = os.path.join(_REPO, "examples")
sys.path.insert(0, _EXAMPLES)

from memscout import bundle                       # noqa: E402

_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_SRC = os.path.join(_EXAMPLES, "demo_target.cpp")
_LINE = re.compile(r"session (0x[0-9a-f]+) id=(\d+) active=(\d) requests=(\d+) user=(\S+)")


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux + ELF")
@unittest.skipUnless(_CXX, "no C++ compiler")
class BundleSelfContainedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-bundle-")
        cls.exe = os.path.join(cls.tmp, "demo_target")
        subprocess.run([_CXX, "-O0", _SRC, "-o", cls.exe], check=True)
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
                cls.truth[int(m.group(1), 16)] = {"mId": int(m.group(2)),
                                                  "mUser": m.group(5)}
            elif line.startswith("READY"):
                cls.pid = int(line.split("=")[1])
                break

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_bundled_script_runs_without_memscout(self):
        import author

        self.assertIsNotNone(self.pid)
        # Developer side (has memscout): author a config.
        config = author.author(self.pid, "_ZTV7Session", ["12:i32:mId", "24:nscstring:mUser"])
        cfg_path = os.path.join(self.tmp, "session.json")
        with open(cfg_path, "w") as f:
            json.dump(config, f)

        # Bundle examples/collect.py into one self-contained file.
        bundled = os.path.join(self.tmp, "collect_bundled.py")
        with open(bundled, "w") as f:
            f.write(bundle.bundle(os.path.join(_EXAMPLES, "collect.py")))
        source = open(bundled).read()
        self.assertNotIn("import memscout", source, "bundle must inline, not import, memscout")

        # Run it where memscout can't be imported: scrubbed env, cwd in tmp.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = ""
        log_path = os.path.join(self.tmp, "out.jsonl")
        subprocess.run([sys.executable, bundled, str(self.pid), cfg_path, "--out", log_path],
                       cwd=self.tmp, env=env, check=True)

        with open(log_path) as f:
            records = [json.loads(line) for line in f]
        meta, objects = records[0], records[1:]
        self.assertEqual(meta["count"], 3)
        by_addr = {int(o["addr"], 16): o for o in objects}
        self.assertEqual(set(by_addr), set(self.truth))
        for addr, truth in self.truth.items():
            self.assertEqual(by_addr[addr]["mId"], truth["mId"])
            self.assertEqual(by_addr[addr]["mUser"], truth["mUser"])

    @unittest.skipUnless(hasattr(__import__("ast"), "unparse"), "--minify needs Python 3.9+")
    def test_minified_bundle_is_smaller_and_still_runs(self):
        import author

        config = author.author(self.pid, "_ZTV7Session", ["12:i32:mId", "24:nscstring:mUser"])
        cfg_path = os.path.join(self.tmp, "m_session.json")
        with open(cfg_path, "w") as f:
            json.dump(config, f)

        plain = bundle.bundle(os.path.join(_EXAMPLES, "collect.py"))
        mini = bundle.bundle(os.path.join(_EXAMPLES, "collect.py"), minify_runtime=True)
        self.assertLess(len(mini), len(plain), "minified bundle should be smaller")
        compile(mini, "<mini>", "exec")                # valid Python
        self.assertNotIn('"""', mini.split("collection script")[0],
                         "runtime docstrings should be stripped in the minified half")

        bundled = os.path.join(self.tmp, "min_bundled.py")
        with open(bundled, "w") as f:
            f.write(mini)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = ""
        log_path = os.path.join(self.tmp, "min_out.jsonl")
        subprocess.run([sys.executable, bundled, str(self.pid), cfg_path, "--out", log_path],
                       cwd=self.tmp, env=env, check=True)
        with open(log_path) as f:
            objects = [json.loads(l) for l in f][1:]
        by_addr = {int(o["addr"], 16): o["mId"] for o in objects}
        self.assertEqual({a: t["mId"] for a, t in self.truth.items()}, by_addr)

    def test_bundled_python_cannot_import_memscout(self):
        # Sanity: the scrubbed env we use really does hide memscout.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = ""
        r = subprocess.run([sys.executable, "-c", "import memscout"],
                           cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "memscout must NOT be importable in the test env")


if __name__ == "__main__":
    unittest.main()
