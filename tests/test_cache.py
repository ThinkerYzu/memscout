"""Tests for the on-disk cache path helpers."""

import os
import tempfile
import unittest

from memscout import cache


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memscout-cache-")
        self._saved = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._saved

    def test_cache_dir_under_xdg(self):
        d = cache.cache_dir()
        self.assertEqual(d, os.path.join(self.tmp, "memscout"))
        self.assertTrue(os.path.isdir(d))

    def test_cache_path_creates_parent_not_file(self):
        p = cache.cache_path("mozilla", "ABCD0", "libxul.so.sym")
        self.assertTrue(os.path.isdir(os.path.dirname(p)))
        self.assertFalse(os.path.exists(p))         # only the parent is created
        self.assertTrue(p.startswith(self.tmp))


if __name__ == "__main__":
    unittest.main()
