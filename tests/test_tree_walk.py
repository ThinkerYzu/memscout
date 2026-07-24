"""Traverse a tree in a live target process by following child pointers.

Builds tests/fixtures/tree_target.cpp (a small binary tree with a documented Node
layout), runs it, then walks the tree from its root using memscout -- reading each
node and following its mLeft/mRight pointers across the heap -- and checks the walk
against the structure the program printed. Exercises pointer-chasing / graph traversal.

Skipped without a C++ compiler.
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

_CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tree_target.cpp")
_NODE = re.compile(
    r"NODE (0x[0-9a-f]+) id=(-?\d+) left=(0x[0-9a-f]+|\(nil\)) right=(0x[0-9a-f]+|\(nil\))")

# Node fields, per the fixture's documented layout.
_FIELDS = "8:i32:mId 16:ptr:mLeft 24:ptr:mRight"


def _ptr(text):
    """Parse a %p value ('0x...' or glibc's '(nil)') to an int address."""
    return 0 if text == "(nil)" else int(text, 16)


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc + ELF")
@unittest.skipUnless(_CXX, "no C++ compiler")
class TreeWalkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memscout-tree-")
        cls.exe = os.path.join(cls.tmp, "tree_target")
        subprocess.run([_CXX, "-O0", _SRC, "-o", cls.exe], check=True)
        cls.proc = subprocess.Popen([cls.exe], stdout=subprocess.PIPE, text=True)
        cls.nodes = {}          # addr -> {"id", "left", "right"}  (ground truth)
        cls.root = None
        cls.pid = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = cls.proc.stdout.readline()
            if not line:
                break
            m = _NODE.match(line)
            if m:
                cls.nodes[int(m.group(1), 16)] = {
                    "id": int(m.group(2)), "left": _ptr(m.group(3)), "right": _ptr(m.group(4))}
            elif line.startswith("ROOT"):
                cls.root = int(line.split()[1], 16)
            elif line.startswith("READY"):
                cls.pid = int(line.split("=")[1])
                break

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _walk(self, reporter):
        """Depth-first walk from the root, following mLeft/mRight in target memory.

        Returns (visited: {addr: decoded fields}, preorder: [id, ...]).
        """
        visited = {}
        preorder = []

        def visit(addr):
            if not addr or addr in visited:
                return
            node = reporter.decode(addr, _FIELDS)
            visited[addr] = node
            preorder.append(node["mId"])
            visit(node["mLeft"])
            visit(node["mRight"])

        visit(self.root)
        return visited, preorder

    def setUp(self):
        self.assertIsNotNone(self.pid, "tree_target never reported READY")
        self.assertIsNotNone(self.root, "no ROOT line")
        self.assertEqual(len(self.nodes), 6)

    def test_walk_visits_every_node_with_matching_links(self):
        with memscout.Reporter(self.pid) as r:
            visited, _ = self._walk(r)
        self.assertEqual(set(visited), set(self.nodes),
                         "the walk must reach exactly the nodes the program built")
        for addr, truth in self.nodes.items():
            node = visited[addr]
            self.assertEqual(node["mId"], truth["id"])
            self.assertEqual(node["mLeft"], truth["left"], "left child pointer must match")
            self.assertEqual(node["mRight"], truth["right"], "right child pointer must match")

    def test_preorder_sequence(self):
        with memscout.Reporter(self.pid) as r:
            _, preorder = self._walk(r)
        # Depth-first, left-before-right, from the fixture's tree.
        self.assertEqual(preorder, [1, 2, 4, 3, 5, 6])


if __name__ == "__main__":
    unittest.main()
