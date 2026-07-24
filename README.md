# memscout

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
    memscout offsets  <debuginfo-elf> <type> [field ...]       # (developer) DWARF -> spec strings
    memscout bundle   <script.py> [-o out.py] [--minify]       # inline runtime -> one self-contained file

As a library: `import memscout; with memscout.Target(pid) as t: ...` — everything the CLI does is
available programmatically (`resolve`, `relocate`, `find_objects`, `decode`, `identify_class`, …).
A reporter-only script imports the self-contained `memscout.runtime` (the `Reporter` facade:
`relocate`/`scan`/`read`/`decode`, no symbols/DWARF); `memscout bundle` ships it as one file.

## Remote reporter → developer workflow

memscout's primary use case is collecting runtime info from a machine you can't touch:

- A **reporter** runs a small script that **relocates** developer-supplied `(module, offset)`
  addresses, **scans** the heap, **decodes** fields, and writes a log — needing no symbols, DWARF,
  or symbol server.
- A **developer** (with an AI agent) authors that script for the reporter's exact build, resolving
  symbols and field offsets offline, and analyzes the log. The two authoring commands line up with
  what the reporter needs: `memscout resolve` prints the `(module, offset)` to relocate, and
  `memscout offsets` prints the field specs to decode.

See [`examples/`](examples/) for a runnable end-to-end walkthrough (`author.py` → `collect.py`).

**Working with an AI agent?** This repo ships an agent skill,
[`skills/memscout-collect/SKILL.md`](skills/memscout-collect/SKILL.md), that teaches the full
authoring flow — study the target class, pick objects/fields, verify with the CLI, gather the
`(module, offset)` + specs for the build, and generate the reporter's script. Point your agent at
it, or make it an auto-discovered Claude Code skill by symlinking it in:
`mkdir -p .claude/skills && ln -s ../../skills/memscout-collect .claude/skills/`.

## Install

    pip install -e .                 # runtime + CLI (standard library only)
    pip install -e .[authoring]      # + pyelftools, for the developer-side `offsets` (Level 2)

The runtime/reporter core has no third-party dependencies; only the DWARF authoring aid needs
`pyelftools`. Requires Linux, ELF, x86-64, and `readelf` (binutils) for symbol resolution.

## Tests

    ./run-tests.sh        # or: make test

## Docs

Full spec, design, and handoff live in the task repo under `proj_docs/memscout/`.

## License

MIT — see [LICENSE](LICENSE).
