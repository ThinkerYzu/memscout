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
  addresses and field offsets **offline**, and generate a small script + config.
- **The reporter** (a user hitting the bug) runs that script on their machine. It only
  **relocates + scans + decodes** — it needs no symbols, DWARF, `readelf`, or network.

Everything symbol/DWARF-related happens on your side. The reporter runs one self-contained
file. This skill walks you from source code to that shipped file.

Prerequisites: `pip install -e '.[authoring]'` (adds `pyelftools` for DWARF). The reporter
needs only a stock Python 3.

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

# d) get field specs from DWARF (needs a build with debug info; see step 4 for stripped)
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
`nstarray`, `refptr`/`nscomptr`, and hashtables `pldhash` / `mhashtable[:entry_size]`.
`memscout offsets` picks these for you from DWARF.

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

Assemble the config with the authoring helper (it resolves the vtable and emits the config):

```bash
python examples/author.py <pid> _ZTVN7mozilla3dom8WakeLockE \
    --debuginfo /path/to/libxul.so --type mozilla::dom::WakeLock \
    --fields mLocked mHidden mTopic > wakelock.json
```

`wakelock.json` holds `{class, module, vtable_offset, build_id, field_specs}` — exactly what
the reporter needs and nothing that requires symbols on their end.

### 5. Generate the reporter script

Start from `examples/collect.py` (reporter-side: relocate → scan → decode → JSON-lines log,
imports only `memscout.runtime`). Adapt it only if you need custom logging/sampling — its
format and cadence are the script's job, not the framework's. Then bundle it into one file:

```bash
memscout bundle examples/collect.py --minify -o collect_wakelock.py
```

Send **`collect_wakelock.py` + `wakelock.json`** to the reporter. They run, with a stock
Python 3 and no memscout install:

```bash
python3 collect_wakelock.py <pid> wakelock.json --out wakelock.jsonl
```

They send back `wakelock.jsonl`. Analyze it (the first line is a `meta` record with the
build-id match + object count; each following line is one object's decoded fields):

```bash
grep '"type": "object"' wakelock.jsonl | jq .
```

---

## CLI cheat-sheet

| Command | Side | Purpose |
|---------|------|---------|
| `memscout modules <pid>` | either | loaded modules + build-ids (find libxul + build-id) |
| `memscout resolve <pid> <sym> [--module]` | developer | symbol → runtime addr **and** `module+offset` |
| `memscout offsets <debuginfo> <type> [fields]` | developer | DWARF → `OFF:TYPE:NAME` specs |
| `memscout scan <pid> <vtable-sym> [specs] [--annotate]` | developer | find + decode live objects locally |
| `memscout dump <pid> <addr> [specs]` | developer | one object's class + fields/annotated slots |
| `examples/author.py` | developer | resolve + (optionally DWARF) → a config.json |
| `memscout bundle <script> [--minify] -o out.py` | developer | inline runtime → one self-contained reporter file |
| `examples/collect.py` (bundled) | reporter | relocate → scan → decode → JSON-lines log |

## Gotchas

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
- **Stripped local build?** You can't `offsets`/`resolve` locally — fetch the build's debug
  info via debuginfod / the Mozilla symbol server (memscout does this for `resolve`; hand a
  fetched debug ELF to `offsets`).

## See also

- `examples/README.md` — a runnable end-to-end walkthrough on a bundled demo app.
- `../../proj_docs/memscout/SPEC.md` — the full workflow spec and requirements.
