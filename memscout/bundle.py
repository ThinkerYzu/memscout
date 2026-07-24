"""Inline the reporter runtime ahead of a collection script → one self-contained file.

A developer writes a collection script that imports the reporter primitives from
`memscout.runtime`; `bundle()` prepends that module's source and strips the script's
`from memscout.runtime import ...` line (the names are now in scope), producing a single
file a reporter runs with a stock Python — no memscout install, no third-party packages.

It's a plain concatenation, not a dependency analyzer: `runtime.py` is already exactly the
reporter-side surface, so there's nothing to tree-shake.
"""

import inspect
import re

from . import runtime

# Lines that pull in the runtime during development; dropped when bundled (names are inlined).
_RUNTIME_IMPORT = re.compile(
    r"^\s*(from\s+memscout\.runtime\s+import\b"
    r"|import\s+memscout\.runtime\b"
    r"|from\s+memscout\s+import\s+runtime\b).*$")


def bundle(script_path):
    """Return a self-contained script: the reporter runtime inlined ahead of `script_path`."""
    runtime_src = inspect.getsource(runtime)
    with open(script_path) as f:
        script_src = f.read()
    kept = [line for line in script_src.splitlines() if not _RUNTIME_IMPORT.match(line)]
    header = (
        "#!/usr/bin/env python3\n"
        "# Self-contained memscout collection script (bundled by `memscout bundle`).\n"
        "# The reporter-side runtime is inlined below; needs only a stock Python 3.\n")
    return "%s\n%s\n\n# ==== collection script ====\n\n%s\n" % (
        header, runtime_src, "\n".join(kept))
