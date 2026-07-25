---
name: memscout-collect
description: >-
  Author a memscout collection script for the remote reporter → developer workflow —
  gathering the live state of C++ objects (Firefox or any Linux app) from a machine you
  can't touch. Use when someone needs to collect runtime/memory info remotely: study the
  target class in source, pick the object and fields, verify locally with the memscout
  CLI, gather the (module, offset) + OFF:TYPE:NAME specs for the reporter's build, and
  generate one self-contained script the reporter runs. Triggers on "collect runtime info
  from a user's Firefox", "inspect object X in a running build remotely", "memscout script",
  "what's the state of Y in the field".
---

# Authoring a memscout remote collection script

memscout inspects the live internal state of a running Linux process **read-only** (via
`/proc/<pid>/mem`, never stopping it). Its main job is a **two-party workflow**:

- **You (the developer)** have the symbols/debug info. You study the target, resolve
  addresses and field offsets **offline**, and generate **one self-contained script** with
  the build-specific config baked in.
- **The reporter** (a user hitting the bug) runs that script on their machine. It only
  **relocates + scans + decodes** — it needs no symbols, DWARF, `readelf`, or network.

Everything symbol/DWARF-related happens on your side. The reporter runs one self-contained
file. This skill walks you from source code to that shipped file.

> **Ship one file, not two.** Prefer baking the config (module, offset, specs, build-id)
> directly into the script as a literal, then bundling that into a single `.py`. A separate
> `config.json` is a second thing to keep paired, version-match, and explain to the reporter —
> and a mismatch between the two is a silent-wrong-data trap. The examples below take a config
> argument to keep authoring and collection decoupled while you iterate, but the **shipped**
> artifact should be one file the reporter runs with nothing else attached. Only split config
> out when the reporter must run the *same* script against several builds/targets — then the
> config is the only thing that varies, and pairing it is a deliberate choice, not an accident.

Prerequisites: `pip install -e .` (no third-party Python deps). The developer-side `offsets`
command works on debug info of any size, including Firefox `libxul`. The reporter needs only a
stock Python 3.

> **Don't read memscout's source to author a script — read [`REFERENCE.md`](REFERENCE.md)**
> (next to this file). It documents the complete reporter-side surface: every `Reporter`
> method with its return type, the **exact value each decoder token returns** (scalars,
> strings, `refptr`, `nstarray`, the hashtables), the spec-string grammar, the config schema,
> and the scan limits/gotchas. That's authoritative; the source shouldn't be needed.

---

## The five steps

### 1. Study the target code

Find the class/struct you want and learn its shape. For Firefox, search `sources/firefox/`.

- **Locate the definition** and note: is it **polymorphic** (has `virtual` methods → a
  vtable)? memscout finds objects by their **vtable pointer**, so a non-virtual class can't
  be located this way — pick a virtual one, or a virtual base you can scan for.
- List the **fields** you care about and their C++ types (bool, int, `nsString`,
  `RefPtr<T>`, `nsTArray<T>`, a hashtable, …).
- Note **inheritance** — a field may live in a base class (memscout's DWARF tool accounts
  for base offsets automatically).
- Identify the **module** that defines it (usually `libxul.so`).

Deliverables from this step: the **type name** (e.g. `mozilla::dom::WakeLock`), the field
names, and the module.

### 2. Choose the target object and fields

Pick something that actually works with a heap vtable scan:

- **Concrete, not abstract.** Instances store the *concrete* class's vtable; an abstract
  base has no direct instances. Prefer concrete leaf classes.
- **Heap-allocated** (`new`). Stack objects aren't in the scanned heap regions.
- **Fields at stable data-member offsets** (not computed getters).

Keep the field set small — collect what answers the question, nothing more (this is
read from a stranger's process; minimize and stay auditable).

### 3. Test locally with the CLI

Run a **local build** (or any process of a matching build) and verify the mechanics before
shipping anything. `<pid>` is your local process.

```bash
# a) find the module + its build-id (you'll match this against the reporter's build)
memscout modules <pid> | grep libxul

# b) find the mangled vtable symbol for your class (resolve/scan need the linker symbol):
LIB=/path/to/libxul.so
readelf -sW "$LIB" | awk '$8 ~ /^_ZTV/ {print $8}' \
  | while read s; do printf '%s\t%s\n' "$(echo "$s" | c++filt)" "$s"; done \
  | grep -F "vtable for mozilla::dom::WakeLock	"      # -> the _ZTV... symbol

# c) confirm it resolves, and read off (module, offset)  [the reporter relocates this]
memscout resolve <pid> _ZTVN7mozilla3dom8WakeLockE --module libxul.so
#   -> _ZTVN...E = 0x...  =  libxul.so+0xNNNN  (vtable, ...)

# d) get field offsets from DWARF (needs a build with debug info; see step 4 for stripped).
#    `memscout offsets` works on debug info of any size, including Firefox libxul.
memscout offsets "$LIB" mozilla::dom::WakeLock mLocked mHidden mTopic
#   -> 40:bool:mLocked  41:bool:mHidden  48:nsstring:mTopic

# e) prove it end-to-end: scan for live objects and decode the fields
memscout scan <pid> _ZTVN7mozilla3dom8WakeLockE 40:bool:mLocked 48:nsstring:mTopic --module libxul.so
# or inspect one object's raw content / class:
memscout dump <pid> <addr>
```

If `scan` finds objects and the decoded values look right, the specs and vtable are correct.
Iterate here until they are. (No wake lock held → 0 objects; pick a class with live
instances to exercise decoding, e.g. try a concrete DOM class in a tab process.)

**Decoder type tokens** for the specs: `u8 u16 u32 u64 i8 i16 i32 i64 bool ptr`,
`atomic:<T>`, Firefox strings `nsstring`/`nscstring` (and `ns[A]String` variants),
`nsatom` (atom text), `nstarray`, `refptr`/`nscomptr`/`uniqueptr`/`owningnonnull`,
`maybe:<mIsSome_off>`, `linkedlist`, and hashtables `pldhash` / `mhashtable[:entry_size]`.
**For the authoritative, complete list run `memscout decoders`** — it prints straight from the
live registry, showing each token's **C/C++ type(s)** and what it returns, so it doubles as the
type→token map when you read a member's C++ type. It never drifts and you needn't grep the
source. `memscout offsets` picks these tokens for you from DWARF. For **what each token
returns** — scalars give ints, strings give a `str` (or a `"<…>"` sentinel), `refptr` gives the
raw pointee address, `nstarray` gives `{length, data}`, hashtables give
`{count, capacity, live:[…]}` — see [`REFERENCE.md`](REFERENCE.md); knowing the shape is
essential when a field points at more to read.

> **Use `memscout offsets` — don't parse DWARF yourself.** It handles debug info of any size,
> including Firefox `libxul`; there's no separate path for big binaries. If it can't map a
> member's type it emits a commented `# OFF:<class …>:name` placeholder — pick the token for that
> member from `memscout decoders` (matching its C/C++ type) and fill the spec in by hand.

### 4. Collect the spec strings and locations (the config)

The config is build-specific. It must be resolved against **the reporter's exact build**,
not just your local one:

- If your local build matches the reporter's (same build-id), the values from step 3 are
  final.
- Otherwise, get the reporter's Firefox **version/build-id** (they can run `memscout
  modules <pid> | grep libxul` and share the `build=` id, or you know the released version),
  then fetch that build's debug info: memscout's `resolve` already falls through to
  **debuginfod** and the **Mozilla symbol server**, and `offsets` reads a debug ELF you
  fetch for that build. Resolve/compute against *that*.

Assemble the config with the authoring helper. It resolves the vtable and emits the config;
`--debuginfo/--type` generates the field specs from DWARF (works on `libxul` too):

```bash
python examples/author.py <pid> _ZTVN7mozilla3dom8WakeLockE \
    --debuginfo /path/to/libxul.so --type mozilla::dom::WakeLock \
    --fields mLocked mHidden mTopic > wakelock.json
```

You can also skip `--debuginfo` and pass hand-written specs positionally (e.g. for a member
`offsets` left as an unrecognized-type placeholder) — then `author.py` only resolves the vtable:

```bash
python examples/author.py <pid> _ZTVN7mozilla3dom8WakeLockE \
    40:bool:mLocked 41:bool:mHidden 48:nsstring:mTopic > wakelock.json
```

`wakelock.json` holds `{class, module, vtable_offset, build_id, field_specs}` — exactly what
the reporter needs and nothing that requires symbols on their end.

### 5. Generate the reporter script — one file

Start from `examples/collect.py` (reporter-side: relocate → scan → decode → JSON-lines log,
imports only `memscout.runtime`). Adapt it only if you need custom logging/sampling — its
format and cadence are the script's job, not the framework's.

**Bake the config in, then bundle — so the reporter runs a single file.** Instead of leaving
`collect.py` to read `wakelock.json` at runtime, embed that dict as a literal in your copy of
the script (drop the config-file argument), then bundle:

```python
# collect_wakelock.py (before bundling): config baked in, no external file
CONFIG = {
    "class": "mozilla::dom::WakeLock",
    "module": "libxul.so",
    "vtable_offset": 0xNNNN,
    "build_id": "…",                       # stamped + checked at run time
    "field_specs": ["40:bool:mLocked", "48:nsstring:mTopic"],
}
```

```bash
memscout bundle collect_wakelock.py --minify -o collect_wakelock_bundled.py
```

Send the reporter **one file**. They run it with a stock Python 3 and no memscout install —
no config to pair, no second download to keep in sync:

```bash
python3 collect_wakelock_bundled.py <pid> --out wakelock.jsonl
```

(The `examples/author.py … > wakelock.json` step from step 4 is your authoring scratch: read
those values, drop them into `CONFIG`, ship the one bundled file. Keep a separate `config.json`
only for the multi-build case called out at the top.)

They send back `wakelock.jsonl`. Analyze it (the first line is a `meta` record with the
build-id match + object count; each following line is one object's decoded fields):

```bash
grep '"type": "object"' wakelock.jsonl | jq .
```

---

## CLI cheat-sheet

| Command | Side | Purpose |
|---------|------|---------|
| `memscout decoders` | developer | list every decoder TYPE token + its C/C++ type(s) and return (authoritative; from the live registry) |
| `memscout modules <pid>` | either | loaded modules + build-ids (find libxul + build-id) |
| `memscout resolve <pid> <sym> [--module]` | developer | symbol → runtime addr **and** `module+offset` |
| `memscout offsets <debuginfo> <type> [fields]` | developer | DWARF → `OFF:TYPE:NAME` specs |
| `memscout scan <pid> <vtable-sym> [specs] [--annotate]` | developer | find + decode live objects locally |
| `memscout dump <pid> <addr> [specs]` | developer | one object's class + fields/annotated slots |
| `examples/author.py` | developer | resolve + (optionally DWARF) → a config.json |
| `memscout bundle <script> [--minify] -o out.py` | developer | inline runtime → one self-contained reporter file |
| `examples/collect.py` (bundled) | reporter | relocate → scan → decode → JSON-lines log |

## Reporter API — what a collection script may call

A reporter script imports only the self-contained runtime and drives a `Reporter`. These are the
**only** primitives available on the reporter side (no symbols, DWARF, or network). The quick
surface is below; **[`REFERENCE.md`](REFERENCE.md) has the full signatures, return types, and
gotchas** (the silent `find_objects(limit=1000)` cap, the JS-heap scan skip, the `+16` vtable
rule, and each decoder's exact return shape):

```python
from memscout.runtime import Reporter, register

with Reporter(pid) as r:                       # attach read-only; auto-closes
    addr   = r.relocate(module, offset)        # (module, offset) -> live addr | None
    bases  = r.find_objects(needle)            # [addr, ...] where the 8-byte needle appears
    fields = r.decode(base, "8:bool:mActive 12:i32:mId")   # -> {name: value}
    # raw / typed reads (None if unmapped, never raises):
    r.read(addr, n)          # -> bytes | None
    r.read_uint(addr, size)  # -> int | None
    r.read_ptr(addr)         # -> int | None
    # modules:
    r.modules                # ModuleMap: iterate; .by_name(name), .for_addr(addr)
    r.module(name)           # Module | None  (.name .path .load_bias .build_id .ranges)
    r.scan_regions()         # -> (lo, hi) writable heap regions
    r.dump_slots(base)       # -> lines: raw 8-byte slots (layout aid, no symbols)
```

**Find objects by vtable:** `needle = r.relocate(module, vtable_offset) + 16`, then
`r.find_objects(needle)`.

**Custom decoder** — register a token you then use in specs (`fn(mem, base, off, arg) -> value`):

```python
register("mykind", lambda mem, base, off, arg: mem.read_uint(base + off, 4))
```

**Not on the reporter side:** `resolve`, `vtable`, `identify_class`, `dump_object` — these need
symbols and live on the developer-side `Target` (a `Reporter` subclass), *not* in a bundled
script. A reporter script uses only the surface above.

## Gotchas

- **Ship one file.** Bake the config into the script and bundle to a single `.py`; don't hand
  the reporter a script + a `config.json` to keep paired. Two files drift, get separated in
  chat, and version-mismatch silently. Split config out only when one script must serve several
  builds (see the note at the top).
- **Reporter build must match.** Baked offsets are silently wrong on a different build.
  `collect.py` stamps and checks the build-id (`build_match` in the meta line) — heed it.
- **Auto-detect picks the first module.** `resolve` without `--module` returns the first
  match in `/proc/maps` order (e.g. Firefox interposes its own `malloc`). Pass `--module`
  to be sure.
- **Abstract bases have no direct instances**; scan concrete classes.
- **DWARF gives the literal layout.** A raw `char*` maps to `ptr`, not a string decoder —
  use the string tokens only for real `ns*String` members.
- **Read-only, safe to run.** The script never writes or stops the process — reassure the
  reporter, and keep the field set minimal and the script auditable (bundle without
  `--minify` if they want to read it).
- **`offsets` needs debug info.** `memscout offsets` / `author.py --debuginfo` read field
  offsets from DWARF, so they need a build with debug info (they work on Firefox `libxul`).
  `resolve`/`scan`/`modules` use the symbol table, not DWARF.
- **Stripped local build?** You can't `offsets`/`resolve` locally — fetch the build's debug
  info via debuginfod / the Mozilla symbol server (memscout does this for `resolve`; hand a
  fetched debug ELF to `offsets`).

## See also

Bundled with this skill, next to this file:

- [`REFERENCE.md`](REFERENCE.md) — the complete reporter-side API and decoder reference (consult
  this instead of reading memscout's source).
- `examples/author.py` and `examples/collect.py` — the developer- and reporter-side scripts this
  skill drives; copy `collect.py` as your reporter-script starting point (step 5).
- `examples/demo_target.cpp` — a tiny stand-in app (a `Session` class with a known layout) to
  exercise both scripts end to end before you retarget to Firefox.

In the memscout project repository (not part of this skill): the top-level `README.md` for the
CLI overview, and `examples/README.md` for a full runnable walkthrough of the workflow.
