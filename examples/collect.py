#!/usr/bin/env python3
"""Reporter-side collection script for the memscout remote-collection workflow.

This runs on the REPORTER's machine. Given the config the developer produced (a
class's `(module, offset)` and field specs), it:

  1. RELOCATES the vtable offset to a live address using only /proc/<pid>/maps,
  2. SCANS the writable heap for that class's objects,
  3. DECODES the named fields,
  4. writes a JSON-lines log to share back with the developer.

It performs NO symbol resolution and needs no DWARF, readelf, or symbol server --
only the module map, memory reads, and the baked-in constants from the config. It
imports only `memscout.runtime`, so `memscout bundle collect.py -o out.py` turns it
into one self-contained file a reporter runs with a stock Python:

    python collect.py <pid> config.json [--out sessions.jsonl]      # during development
    memscout bundle collect.py -o collect_bundled.py               # ship one file
    python collect_bundled.py <pid> config.json                    # reporter runs this

The log's first line is a `meta` record (class, module, expected vs. actual build-id,
object count); each following line is one object's decoded fields. If the reporter's
build-id doesn't match the developer's, offsets may be wrong -- the meta record flags
it and the script warns, so a mismatched log isn't silently trusted.
"""

import argparse
import json
import time

from memscout.runtime import Reporter


def collect(pid, config, out_path):
    """Run the reporter-side collection and write a JSON-lines log. Returns (count, build_ok)."""
    module = config["module"]
    specs = config["field_specs"]
    with Reporter(pid) as t:
        mod = t.module(module)
        if mod is None:
            raise SystemExit("module %r is not loaded in pid %d" % (module, pid))

        # Optional sanity check: is the reporter's build the one the developer resolved
        # against? build_id here uses readelf; a fully self-contained reporter script
        # could read the .note.gnu.build-id note directly (see SPEC) -- the relocate/
        # scan/decode core below needs neither.
        want = config.get("build_id")
        got = mod.build_id.hex() if mod.build_id else None
        build_ok = want is None or got == want

        # Reporter-side core: relocate -> scan -> decode. No symbols involved.
        needle = t.relocate(module, config["vtable_offset"]) + 16
        bases = t.find_objects(needle)

        with open(out_path, "w") as log:
            meta = {"type": "meta", "collected_at": int(time.time()),
                    "class": config.get("class"), "module": module,
                    "build_id_expected": want, "build_id_actual": got,
                    "build_match": build_ok, "count": len(bases)}
            log.write(json.dumps(meta) + "\n")
            for base in bases:
                record = {"type": "object", "addr": hex(base)}
                record.update(t.decode(base, specs))
                log.write(json.dumps(record) + "\n")
    return len(bases), build_ok


def main():
    # A literal description (not __doc__): once bundled, __doc__ is the module's, not ours.
    parser = argparse.ArgumentParser(description="memscout reporter-side collection")
    parser.add_argument("pid", type=int)
    parser.add_argument("config", help="config.json produced by author.py")
    parser.add_argument("--out", default="collect.jsonl", help="log file to write")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)
    count, build_ok = collect(args.pid, config, args.out)
    if not build_ok:
        print("WARNING: build-id mismatch -- baked offsets may be wrong for this build")
    print("wrote %d object record(s) to %s" % (count, args.out))


if __name__ == "__main__":
    main()
