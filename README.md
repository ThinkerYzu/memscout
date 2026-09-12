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

memscout's primary use case is collecting runtime info from a machine you can't attach a
debugger to: a bug reporter's Firefox, a customer's server. The job is split by what each side
needs to have on hand. The **developer** has the build's symbols and debug info and does all the
symbol work up front; the **reporter** gets one Python file that only reads memory and needs no
symbols, no DWARF, no symbol server, and no memscout install.

```
  developer's machine                                   reporter's machine
  (own copy of the same build, symbols)                  (the affected process)
  ┌────────────────────────────────────┐                ┌────────────────────────────────┐
  │ 1. resolve → module + offset       │                │                                │
  │    offsets → field specs           │                │ 3. python3 collect_bundled.py  │
  │ 2. write collect.py, bake those in │ ── one file ─▶ │      relocate → scan → decode  │
  │    memscout bundle                 │                │      → sessions.jsonl          │
  │ 4. analyze the log                 │ ◀── the log ── │                                │
  └────────────────────────────────────┘                └────────────────────────────────┘
```

| Side | Needs installed | Needs symbols/DWARF? | Job |
|------|-----------------|----------------------|-----|
| **Developer** (+ AI agent) | memscout, binutils, gdb; a running copy of the reporter's build | **yes** | resolve addresses + field layouts, write the script, analyze the log |
| **Reporter** | Python 3 | **no** | run the script on the affected process, send back the log |

The rest of this section walks through the example that ships in [`examples/`](examples/): a
small program (`demo_target.cpp`) that allocates three `Session` objects and parks, standing in
for the reporter's application. Build it with `c++ examples/demo_target.cpp -O0 -g -o demo_target`
and run it; it prints its pid.

### Before you start: run the reporter's build yourself

"Offline" here means *without the reporter's process*, not without any process. `memscout
resolve` and `memscout modules` read a **live** process (that is how they learn where a module
is loaded and which build it is), so the developer runs their **own copy of the same build**
(the same Firefox version, or the same binary) and resolves against that. The numbers that come
out are relative to the module file, not to any particular run, so they carry over to the
reporter's process as long as the build matches.

The build is identified by its **build-id**, a hash the linker stamps into every ELF file. Two
copies of the same release have the same build-id; a different release (or a local rebuild) has
a different one. The workflow checks this for you: step 1 records the developer's build-id, and
the script from step 2 compares it against the reporter's process at run time.

If your local build is stripped (a release Firefox is), `resolve` fetches symbols from
**debuginfod** (set `DEBUGINFOD_URLS`) and the **Mozilla symbol server**, in that order, without
any flags; results are cached under `~/.cache/memscout`. `memscout offsets` needs DWARF, which
a stripped file doesn't carry. Point it at the matching debug file instead: the unstripped
build, a `.debug` file, or what `debuginfod-find debuginfo <build-id>` downloads.

### 1. Developer resolves the build-specific numbers

Four values are specific to the build, and they are all the reporter's script will ever need:
the **module** the class lives in, the module's **build-id**, the **offset of the class's
vtable** within that module, and the **field specs**. Three CLI commands read them off a
running copy of the build (pid `1234` here):

```console
$ memscout modules 1234 | grep demo_target
demo_target        bias=0x634282e8b000 build=7bcc4e96…  0x634282e8b000-0x634282e90000

$ memscout resolve 1234 _ZTV7Session --module demo_target
_ZTV7Session = 0x634282e8ed50  =  demo_target+0x3d50  (vtable, size=40, via local-symtab)

$ memscout offsets demo_target Session mActive mId mRequests
8:bool:mActive
12:i32:mId
16:u64:mRequests
```

- `modules` prints every loaded ELF module with its **load bias** (the address the module
  happened to be mapped at in *this* run) and its build-id. Keep the build-id.
- `resolve` finds the vtable symbol and prints its address two ways. The absolute address
  (`0x634282e8ed50`) is useless to the reporter, since their process maps the module somewhere
  else; `demo_target+0x3d50` is the same address as *bias + offset*, and the offset part is what
  carries over. Keep `0x3d50`.
- `offsets` reads the class's layout from DWARF and prints one `OFF:TYPE:NAME` spec per member,
  choosing the decoder token from the member's C++ type (an `nsCString` member comes out as
  `nscstring`, a `RefPtr<T>` as `refptr`, and so on). For the demo's `mUser`, which fakes a
  Firefox string with a raw `const char*` + length pair, the token is written by hand:
  `24:nscstring:mUser`. You can always write specs by hand instead of using `offsets`; the
  quick way to check a spec is `memscout scan 1234 _ZTV7Session 24:nscstring:mUser` on your
  local copy, which decodes it against real objects.

`examples/author.py` does all three in one command and prints them as JSON, and with
`--debuginfo`/`--type` it generates the field specs from DWARF at the same time:

```console
$ python examples/author.py 1234 _ZTV7Session --debuginfo demo_target --type Session \
      --fields mActive mId mRequests
{
  "class": "_ZTV7Session",
  "module": "demo_target",
  "vtable_offset": 15696,
  "build_id": "7bcc4e96c558cb3c154bb638128dc8ffc525dba5",
  "field_specs": [
    "8:bool:mActive",
    "12:i32:mId",
    "16:u64:mRequests"
  ]
}
```

(`15696` is `0x3d50` in decimal.)

### 2. Developer writes the collection script

The script runs on the reporter's machine, so it imports **only `memscout.runtime`** and uses
its `Reporter` class, the reporter-side API, which knows nothing about symbols. The numbers
from step 1 go in as a literal, so the reporter gets one file with nothing to pair it with:

```python
#!/usr/bin/env python3
"""Collect every live Session in a running demo_target -- runs on the reporter's machine."""
import json
import sys

from memscout.runtime import Reporter

# The only build-specific part: the numbers the developer resolved in step 1.
CONFIG = {
    "module": "demo_target",
    "vtable_offset": 0x3d50,
    "build_id": "7bcc4e96c558cb3c154bb638128dc8ffc525dba5",
    "field_specs": ["8:bool:mActive", "12:i32:mId", "24:nscstring:mUser"],
}

pid, out_path = int(sys.argv[1]), sys.argv[2]
with Reporter(pid) as r, open(out_path, "w") as log:
    mod = r.module(CONFIG["module"])
    if mod is None:
        sys.exit("%s is not loaded in pid %d" % (CONFIG["module"], pid))
    build_ok = (mod.build_id or b"").hex() == CONFIG["build_id"]
    needle = r.relocate(mod, CONFIG["vtable_offset"]) + 16   # the value a live object's first word holds
    bases = r.find_objects(needle)
    log.write(json.dumps({"type": "meta", "build_match": build_ok, "count": len(bases)}) + "\n")
    for base in bases:
        record = {"type": "object", "addr": hex(base)}
        record.update(r.decode(base, CONFIG["field_specs"]))
        log.write(json.dumps(record) + "\n")
print("wrote %d object(s) to %s" % (len(bases), out_path))
```

Line by line, this is the whole reporter-side job:

- **`r.module(name)`** looks the module up in the reporter's `/proc/<pid>/maps`. Its `build_id`
  is read straight from the file's ELF note (no `readelf` needed), which is how the script
  tells whether the baked-in numbers apply to this process.
- **`r.relocate(mod, offset)`** is `load bias + offset`: it turns the build-relative vtable
  offset back into an address in the reporter's process. This is the *only* thing the reporter
  side does with symbols, and it needs none to do it.
- **`+ 16`** is the part that is easy to miss. The vtable symbol points at the start of the
  vtable, but a vtable begins with two bookkeeping words (offset-to-top and the typeinfo
  pointer), and an object's first word points *past* them at the first virtual-function slot.
  So the value to scan for is the symbol address plus 16. (`memscout scan` and `Target.vtable()`
  add it for you; with `Reporter` you add it yourself.)
- **`r.find_objects(needle)`** scans the writable, non-file-backed regions of the process for
  that 8-byte value and returns the address of every hit. It stops after 1000 by default;
  pass `limit=` if you expect more.
- **`r.decode(base, specs)`** reads each field spec relative to the object's address and returns
  `{name: value}`.

The log format, the fields, and whether to sample once or every few seconds are all the
script's decisions; memscout only provides the reads. Test it against your local copy before
shipping (`python3 collect.py 1234 /tmp/test.jsonl`) and compare with `memscout scan`.

### 3. Bundle it, ship it, reporter runs it

`memscout bundle` prepends `memscout/runtime.py` to the script and strips the
`from memscout.runtime import …` line, so the result is one self-contained file
(about 15 KB with `--minify`) that runs on a stock Python 3:

```console
$ memscout bundle collect.py --minify -o collect_bundled.py
wrote self-contained script to collect_bundled.py
```

Send `collect_bundled.py` to the reporter. On their side:

1. **Find the pid** of the affected process: `pgrep -f demo_target`, or for Firefox, the
   process list in `about:processes`. Firefox is many processes: DOM objects live in the tab's
   content process, browser-wide state in the parent; the developer should say which.
2. **Have permission** to read that process's memory, the same rule as in [Install](#install):
   a process the reporter started themselves is fine on most systems, but Ubuntu and Debian's
   default ptrace restriction blocks even that, so they run it with `sudo` or lower
   `ptrace_scope` first. A permission problem shows up as
   `cannot read /proc/<pid>/mem and PTRACE_SEIZE failed`.
3. **Run it and send back the log:**

```console
$ python3 collect_bundled.py 4321 sessions.jsonl
wrote 3 object(s) to sessions.jsonl
```

The script only reads; the process never notices. It is also plain Python the reporter can
open and audit before running.

### 4. Developer analyzes the log

```json
{"type": "meta", "build_match": true, "count": 3}
{"type": "object", "addr": "0x6342b4c68020", "mActive": 1, "mId": 1000, "mUser": "alice"}
{"type": "object", "addr": "0x6342b4c69060", "mActive": 0, "mId": 1001, "mUser": "bob"}
{"type": "object", "addr": "0x6342b4c69090", "mActive": 1, "mId": 1002, "mUser": "carol"}
```

Check `build_match` first: if it is `false`, the reporter's binary is not the build the offsets
were resolved against, and every decoded value is suspect. Get their exact version and redo
step 1. Otherwise the log is ordinary JSON lines; e.g.
`jq 'select(.type=="object" and .mActive==1) | .mUser' sessions.jsonl` lists the active users.

[`examples/`](examples/) has this same walkthrough as runnable files, with the real output of
each step: `demo_target.cpp`, `author.py`, and a `collect.py` that reads the config from a JSON
file instead of baking it in (handy while you iterate, or when one script must serve several
builds).

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
