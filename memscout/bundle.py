"""Inline the reporter runtime ahead of a collection script → one self-contained file.

A developer writes a collection script that imports the reporter primitives from
`memscout.runtime`; `bundle()` prepends that module's source and strips the script's
`from memscout.runtime import ...` line (the names are now in scope), producing a single
file a reporter runs with a stock Python — no memscout install, no third-party packages.

It's a plain concatenation, not a dependency analyzer: `runtime.py` is already exactly the
reporter-side surface, so there's nothing to tree-shake.
"""

import ast
import inspect
import re

from . import runtime

# Lines that pull in the runtime during development; dropped when bundled (names are inlined).
_RUNTIME_IMPORT = re.compile(
    r"^\s*(from\s+memscout\.runtime\s+import\b"
    r"|import\s+memscout\.runtime\b"
    r"|from\s+memscout\s+import\s+runtime\b).*$")


def minify(source):
    """Strip comments and docstrings from Python source, returning valid equivalent code.

    Parses to an AST, drops module/class/function docstrings, and re-emits with
    `ast.unparse` (which also discards comments). This is layout-lossy but semantically
    faithful -- far safer than text munging. Needs Python 3.9+ on the authoring machine
    (the bundled output still runs on any Python the reporter has).
    """
    if not hasattr(ast, "unparse"):
        raise RuntimeError("--minify requires Python 3.9+ (ast.unparse) on the authoring machine")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        first = body[0] if body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            del body[0]
            if not body:
                body.append(ast.Pass())         # keep an empty block legal
    return ast.unparse(tree)


def bundle(script_path, minify_runtime=False):
    """Return a self-contained script: the reporter runtime inlined ahead of `script_path`.

    With minify_runtime, the inlined runtime is stripped of comments/docstrings first (the
    developer's script is left readable/auditable).
    """
    runtime_src = inspect.getsource(runtime)
    if minify_runtime:
        runtime_src = minify(runtime_src)
    with open(script_path) as f:
        script_src = f.read()
    kept = [line for line in script_src.splitlines() if not _RUNTIME_IMPORT.match(line)]
    header = (
        "#!/usr/bin/env python3\n"
        "# Self-contained memscout collection script (bundled by `memscout bundle`).\n"
        "# The reporter-side runtime is inlined below; needs only a stock Python 3.\n")
    return "%s\n%s\n\n# ==== collection script ====\n\n%s\n" % (
        header, runtime_src, "\n".join(kept))
