# memscout

[![CI](https://github.com/ThinkerYzu/memscout/actions/workflows/ci.yml/badge.svg)](https://github.com/ThinkerYzu/memscout/actions/workflows/ci.yml)

A lightweight, read-only runtime data-collection framework for live Linux processes.

Inspect the internal state of a running application or service by reading its memory directly
through `/proc/<pid>/mem` — no debugger, and the target is never stopped. memscout is a toolbox
of read-only primitives (attach, module map, symbol resolution, memory/heap scan, typed field
decoding); scripts compose them to answer a specific runtime question.

## CLI

    memscout modules  <pid>                                    # loaded ELF modules + build-ids
    memscout resolve  <pid> <symbol> [--module NAME]           # symbol -> addr + module+offset
    memscout scan     <pid> <vtable-symbol> [OFF:TYPE:NAME ...] # find + decode live objects
    memscout dump     <pid> <addr> [OFF:TYPE:NAME ...]         # an object's class + fields/slots
    memscout decoders                                          # list the field TYPE tokens (authoritative)
    memscout offsets  <debuginfo-elf> <type> [field ...]       # (developer) DWARF -> spec strings
    memscout bundle   <script.py> [-o out.py] [--minify]       # inline runtime -> one self-contained file

As a library: `import memscout; with memscout.Target(pid) as t: ...` — everything the CLI does is
available programmatically (`resolve`, `relocate`, `find_objects`, `decode`, `identify_class`, …).
A reporter-only script imports the self-contained `memscout.runtime` (the `Reporter` facade:
`relocate`/`scan`/`read`/`decode`, no symbols/DWARF); `memscout bundle` ships it as one file.

## Remote reporter → developer workflow

memscout's primary use case is collecting runtime info from a machine you can't attach a debugger
to. The work splits across two roles by what each side is allowed to have — the reporter's machine
never needs symbols, DWARF, or a symbol server:

| Side | Runs | Has symbols/DWARF? | Job |
|------|------|--------------------|-----|
| **Developer** (+ AI agent) | offline, on a copy of the target's build | **yes** | resolve addresses + field layouts, author a script, analyze the log |
| **Reporter** | on the affected machine | **no** | run the script, share the log |

**1. Developer resolves the addresses** offline, keyed to the reporter's exact build. `resolve`
gives the `(module, offset)` the reporter will relocate; `offsets` turns a type's DWARF into the
`OFF:TYPE:NAME` field specs (or write them by hand):

```console
$ memscout resolve <pid> _ZTV7Session --module demo_target
_ZTV7Session = 0x… = demo_target+0x3d50   (vtable, size=…, via local-symtab)
$ memscout offsets demo_target-with-debug Session mActive mId mUser
8:bool:mActive
12:i32:mId
24:nscstring:mUser
```

Those go into a small **config** the reporter's script reads — the only thing that's build-specific:

```json
{ "class": "_ZTV7Session", "module": "demo_target", "vtable_offset": 15696,
  "build_id": "f3e8279a…", "field_specs": ["8:bool:mActive", "12:i32:mId", "24:nscstring:mUser"] }
```

`vtable_offset` is relative to the module's load base; `build_id` lets the reporter confirm the
build matches before trusting the offsets.

**2. Reporter runs one self-contained file.** `bundle` inlines the runtime so the script needs only
a stock Python 3 — no memscout install, no packages. It **relocates** (`load_bias + offset`),
**scans** the heap for live objects, **decodes** the fields, and writes a JSON-lines log:

```console
$ memscout bundle collect.py -o collect_bundled.py     # developer builds the one file
$ python3 collect_bundled.py <pid> session.json --out sessions.jsonl   # reporter runs it
```

**3. Developer analyzes** the shared log offline.

See [`examples/`](examples/) for the full runnable walkthrough (`demo_target.cpp` → `author.py` →
`collect.py`, with real output and the exact config).

**Working with an AI agent?** This repo ships an agent skill,
[`skills/memscout-collect/SKILL.md`](skills/memscout-collect/SKILL.md), that teaches the full
authoring flow — study the target class, pick objects/fields, verify with the CLI, gather the
`(module, offset)` + specs for the build, and generate the reporter's script. Point your agent at
it, or make it an auto-discovered Claude Code skill by symlinking it in:
`mkdir -p .claude/skills && ln -s ../../skills/memscout-collect .claude/skills/`.

## Install

    pip install -e .                 # runtime + CLI (standard library only)

No third-party Python dependencies. Requires Linux, ELF, x86-64, and `readelf` (binutils) for
symbol resolution. The developer-side `offsets` command (Level 2 DWARF authoring) reads DWARF
through **gdb**, so gdb must be on PATH for that one command — this is what lets it scale to
Firefox-sized `libxul` debug info. The reporter/runtime core needs none of this.

## Tests

    ./run-tests.sh        # or: make test

## Docs

Full spec, design, and handoff live in the task repo under `proj_docs/memscout/`.

## Releasing

CI runs the suite on every push/PR; pushing a `v*` tag builds and publishes a GitHub Release.
See [RELEASING.md](RELEASING.md).

## License

MIT — see [LICENSE](LICENSE).
