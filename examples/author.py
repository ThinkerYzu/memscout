#!/usr/bin/env python3
"""Developer-side authoring helper for the memscout remote-collection workflow.

This runs on the DEVELOPER's machine (which has symbols/debug info for the target's
build). It resolves a class's vtable to a build-relative `(module, offset)` and emits
a small JSON config that the reporter's `collect.py` consumes. The reporter never runs
this and never needs symbols.

    python author.py <pid> <vtable_symbol> [OFF:TYPE:NAME ...] > config.json

`pid` is any running process of the target build (the developer's own copy is fine).
Field specs are the fields to collect; supply them from your knowledge of the layout
(a future Level 2 tool can generate them from DWARF).

Example:
    python author.py 4242 _ZTV7Session 12:i32:mId 24:nscstring:mUser > session.json
"""

import json
import sys

import memscout


def author(pid, symbol, specs):
    """Resolve `symbol`'s vtable in the process and return the reporter config dict.

    The config carries everything the reporter needs and nothing that requires
    symbols on their end: the module name, the vtable's offset from that module's
    load base, the build-id it was resolved against, and the field specs.
    """
    with memscout.Target(pid) as t:
        sym = t.resolve(symbol)
        if sym is None:
            raise SystemExit("could not resolve %r (need symbols for this build)" % symbol)
        module = sym.module
        return {
            "class": symbol,
            "module": module.name,
            "vtable_offset": sym.addr - module.load_bias,
            "build_id": module.build_id.hex() if module.build_id else None,
            "field_specs": list(specs),
        }


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: author.py <pid> <vtable_symbol> [OFF:TYPE:NAME ...]")
    config = author(int(argv[0]), argv[1], argv[2:])
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
