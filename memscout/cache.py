"""On-disk cache for fetched debug artifacts (~/.cache/memscout).

Remote sources (debuginfod, Mozilla) download debug info once and reuse it, so a
second run -- and offline analysis afterwards -- is fast. Honors XDG_CACHE_HOME.
"""

import os


def cache_dir():
    """The memscout cache root, created if needed (XDG_CACHE_HOME or ~/.cache)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "memscout")
    os.makedirs(path, exist_ok=True)
    return path


def cache_path(*parts):
    """Absolute path under the cache root for the given sub-path, parent dirs made.

    e.g. cache_path("mozilla", debugid, "libxul.so.sym") -> the file's cache path,
    with its containing directory created. The file itself is not created.
    """
    path = os.path.join(cache_dir(), *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path
