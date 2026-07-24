#!/usr/bin/env bash
# memscout test harness.
#
# Runs the unittest suite in tests/ with the repo on PYTHONPATH, so no install is
# needed. Fixture tests (decoders, symbols, maps) need nothing external; the
# integration tests spawn their own `sleep` child. Pass extra args straight
# through to unittest, e.g.:
#
#     ./run-tests.sh                       # whole suite
#     ./run-tests.sh -v                    # verbose
#     ./run-tests.sh test_decoders         # one module
#     ./run-tests.sh test_symbols.SymbolResolverTest.test_first_source_wins
#
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

# tests import both the package (memscout) and the shared helpers (support.py),
# so both the repo root and tests/ go on the path.
export PYTHONPATH="$here:$here/tests${PYTHONPATH:+:$PYTHONPATH}"

if [ "$#" -eq 0 ]; then
    # start == top == tests/ so the non-package test modules load as top-level
    # names; memscout resolves via the repo root on PYTHONPATH.
    exec python3 -m unittest discover -s "$here/tests" -t "$here/tests" -v
else
    exec python3 -m unittest "$@"
fi
