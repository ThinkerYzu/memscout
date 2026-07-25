#!/usr/bin/env python3
"""Developer-side authoring helper for the memscout remote-collection workflow.

This runs on the DEVELOPER's machine (which has symbols/debug info for the target's
build). It resolves a class's vtable to a build-relative `(module, offset)` and emits a
small JSON config that the reporter's `collect.py` consumes. The reporter never runs this
and never needs symbols.

Field specs can be supplied by hand, or generated from DWARF (Level 2) when you pass a
debug-info ELF and a type name:

    # specs by hand:
    python author.py <pid> _ZTV7Session 12:i32:mId 24:nscstring:mUser > config.json

    # specs from DWARF (developer-side only):
    python author.py <pid> _ZTV7Session --debuginfo demo_target_g --type Session \
        --fields mActive mId mRequests > config.json

`pid` is any running process of the target build (the developer's own copy is fine).
"""

import argparse
import json
import sys

import memscout


def author(pid, symbol, specs=None, debuginfo=None, type_name=None, fields=None):
    """Resolve `symbol`'s vtable and return the reporter config dict.

    Specs come from `specs` (hand-written) or, if `debuginfo` and `type_name` are given,
    are generated from that build's DWARF (dropping any member whose type memscout can't
    map yet -- those are reported on stderr for manual resolution).
    """
    if debuginfo and type_name:
        from memscout import dwarf
        generated = dwarf.field_specs(debuginfo, type_name, fields)
        specs = [s for s in generated if not s.startswith("#")]
        for dropped in (s for s in generated if s.startswith("#")):
            sys.stderr.write("note: %s\n" % dropped)
    if specs is None:
        specs = []

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
            "field_specs": specs,
        }


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pid", type=int)
    p.add_argument("symbol", help="vtable linker symbol, e.g. _ZTV7Session")
    p.add_argument("specs", nargs="*", help="OFF:TYPE:NAME field specs (if not using DWARF)")
    p.add_argument("--debuginfo", help="ELF with DWARF, to generate specs from (Level 2)")
    p.add_argument("--type", dest="type_name", help="C++ type name for --debuginfo")
    p.add_argument("--fields", nargs="*", help="member names to emit (default: all)")
    args = p.parse_args(argv)

    config = author(args.pid, args.symbol, specs=args.specs or None,
                    debuginfo=args.debuginfo, type_name=args.type_name, fields=args.fields)
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
