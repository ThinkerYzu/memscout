# memscout

[![CI](https://github.com/ThinkerYzu/memscout/actions/workflows/ci.yml/badge.svg)](https://github.com/ThinkerYzu/memscout/actions/workflows/ci.yml)

memscout reads data out of a **running** Linux program — without stopping it and without attaching
a debugger. Give it a process ID and it can answer questions like *"how many objects of type `X`
exist right now?"* or *"what are their field values?"* by reading the process's memory directly
through `/proc/<pid>/mem`. It only ever **reads**, and never pauses the target, so it is safe to
point at even a production process.

Good for: inspecting live state in a running Firefox during a bug hunt, sampling a long-running
service, or collecting a one-off snapshot from a machine where you can't run a debugger.

## A quick taste

Find every live `Session` object in a running process (pid `1234`) and print two of its fields:

```console
$ memscout scan 1234 _ZTV7Session 12:i32:mId 24:nscstring:mUser
vptr 0x55f… (from /tmp/demo_target): 3 hit(s) across 0.5MB
  0x6070fd2c8320  mId=1000 mUser=alice
  0x6070fd2c9360  mId=1001 mUser=bob
  0x6070fd2c9390  mId=1002 mUser=carol
=> 3 object(s)
```

Two ideas do the work here:

- **Finding the objects** — the `_ZTV7Session` argument is a *vtable symbol*. Every C++ class with
  virtual methods gets one unique symbol from the compiler (`_ZTV7Session` is the mangled name for
  "vtable for `Session`"), and every live object of that class begins with a pointer to it — the
  `vptr` in the output above — so scanning memory for that pointer finds them all.
- **Reading the fields** — each `offset:type:name` argument is a *field spec*: `12:i32:mId` means
  *"at byte 12 there's a 32-bit int; call it `mId`."* Run `memscout decoders` for every type you can
  name (ints, bools, pointers, Firefox strings, arrays, hashtables, …).

That example is the simplest case: one machine that has both the running process **and** its debug
symbols. When the symbols live somewhere else, memscout splits the job so the machine running the
process needs no symbols at all (see the workflow below).

## Install

    pip install -e .                 # runtime + CLI (standard library only)

No third-party Python dependencies. memscout runs on Linux, on x86-64, against ELF binaries.
Symbol resolution shells out to `readelf`, so binutils must be installed. The developer-side
`offsets` command reads DWARF through **gdb** — that is what lets it handle debug info as large as
Firefox's `libxul` — so gdb is needed for that one command. The reporter side (below) needs none of
this; stock Python 3 is enough.

**Permissions.** Opening `/proc/<pid>/mem` takes the same permission as attaching a debugger:
processes you started yourself are fine, anything else is not. On distributions that default to
Yama's restricted ptrace (Ubuntu and Debian do), "anything else" includes other processes of your
own user. Either run memscout as root, or lift the restriction once:

    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope

Without one of those, memscout exits with `cannot read /proc/<pid>/mem and PTRACE_SEIZE failed`.

## CLI

The full set of subcommands:

```
memscout modules  <pid>                                     # loaded ELF modules + build-ids
memscout resolve  <pid> <symbol> [--module NAME]            # symbol -> addr + module+offset
memscout scan     <pid> <vtable-symbol> [OFF:TYPE:NAME ...] # find + decode live objects
memscout dump     <pid> <addr> [OFF:TYPE:NAME ...]          # an object's class + fields/slots
memscout decoders                                           # list the field TYPE tokens
memscout offsets  <debuginfo-elf> <type> [field ...]        # (developer) DWARF -> spec strings
memscout bundle   <script.py> [-o out.py] [--minify]        # inline runtime -> one self-contained file
```

`memscout decoders` reads the live decoder registry, so it is always the authoritative list of
field types — no need to go looking in the source.

As a library there are two entry points, and which one you import matters:

| Import | Use for | Needs symbols? | Survives `bundle`? |
|--------|---------|----------------|--------------------|
| `from memscout import Target` | analysis on your own machine | yes | **no** |
| `from memscout.runtime import Reporter` | collection scripts you ship to a reporter | no | yes |

`Target` adds symbol resolution and class identification on top of everything `Reporter` can do:

```python
from memscout import Target

with Target(1234) as t:
    needle = t.vtable("_ZTV7Session", module="demo_target")
    for base in t.find_objects(needle):
        print(t.identify_class(base), t.decode(base, "12:i32:mId 24:nscstring:mUser"))
```

Note that `find_objects` takes that needle *value*, not a symbol name. `vtable()` resolves the
symbol and steps past the two vtable header words, which is what a live object actually stores in
its first slot.

**A script you intend to `bundle` must import `Reporter` from `memscout.runtime`.** `bundle` only
strips the three `memscout.runtime` import forms; a `from memscout import Target` line survives into
the bundled file and fails with `ModuleNotFoundError: No module named 'memscout'` on a reporter's
machine. Custom field types have the same split: register them with `register` from
`memscout.runtime` (the top level exports the same function as `register_decoder`).

## Remote reporter → developer workflow

memscout's primary use case is collecting runtime info from a machine you can't attach a debugger
to. The work splits across two roles by what each side needs to have on hand — the reporter's
machine never needs symbols, DWARF, or a symbol server:

| Side | Where | Has symbols/DWARF? | Job |
|------|-------|--------------------|-----|
| **Developer** (+ AI agent) | offline, on a copy of the target's build | **yes** | resolve addresses + field layouts, author a script, analyze the log |
| **Reporter** | on the affected machine | **no** | run the script, share the log |

**1. Developer resolves the addresses** offline, against the reporter's exact build. `resolve`
gives the `(module, offset)` pair the reporter will turn back into an address; `offsets` turns a
type's DWARF into the `OFF:TYPE:NAME` field specs (or write them by hand):

```console
$ memscout resolve 1234 _ZTV7Session --module demo_target
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

`vtable_offset` is the `0x3d50` from above, written in decimal. It counts from the address the
module happened to be loaded at — its *load bias* — which differs on every run, so the reporter
adds the two together. `build_id` lets the reporter confirm the build matches before trusting the
offsets.

**2. Reporter runs one self-contained file.** `bundle` inlines the runtime so the script needs only
a stock Python 3 — no memscout install, no packages. The script does `from memscout.runtime import
Reporter`, and that class is the entire reporter-side API — `relocate`, `scan_regions`,
`find_objects`, `read`, `decode` — with nothing that touches symbols or DWARF. It **relocates**
(`load_bias + offset`), **scans** the heap for live objects, **decodes** the fields, and writes a
JSON-lines log:

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

## Tests

    ./run-tests.sh        # or: make test

## Docs

- [`examples/`](examples/) — the runnable reporter → developer walkthrough, end to end
- [`skills/memscout-collect/SKILL.md`](skills/memscout-collect/SKILL.md) — the script-authoring
  flow, written for an AI agent
- [`skills/memscout-collect/REFERENCE.md`](skills/memscout-collect/REFERENCE.md) — the reporter API
  and each decoder's exact return shape

## Releasing

CI runs the suite on every push/PR; pushing a `v*` tag builds and publishes a GitHub Release.
See [RELEASING.md](RELEASING.md).

## License

MIT — see [LICENSE](LICENSE).
