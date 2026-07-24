# Worked example: the remote reporter → developer workflow

This directory shows memscout's primary use case end to end: a **reporter** runs a small
script that collects runtime info into a log and shares it; a **developer** authored that
script (resolving symbols offline for the reporter's build) and analyzes the log.

The split matters:

| Side | Runs | Needs symbols/DWARF? |
|------|------|----------------------|
| **Developer** (`author.py`) | offline, on the target's build | **yes** — resolves the vtable |
| **Reporter** (`collect.py`) | on the affected machine | **no** — only relocates, scans, decodes |

Files:

- **`demo_target.cpp`** — a stand-in "application": allocates `Session` objects with a known
  layout, prints them, and parks. Think of it as the app the reporter is running.
- **`author.py`** — developer-side: resolves a class's vtable to `(module, offset)` and emits a
  config. Uses symbol resolution.
- **`collect.py`** — reporter-side: relocates + scans + decodes from that config and writes a
  JSON-lines log. No symbol resolution.

## Run it

```bash
cd examples
c++ demo_target.cpp -O0 -o /tmp/demo_target        # build the "app"
/tmp/demo_target &                                  # run it; note the PID it prints
PID=<pid from "READY pid=...">

# memscout must be importable (pip install -e .. , or set PYTHONPATH):
export PYTHONPATH=..

# DEVELOPER (offline, has symbols): resolve the vtable + choose fields -> config
python author.py $PID _ZTV7Session \
    8:bool:mActive 12:i32:mId 16:u64:mRequests 24:nscstring:mUser > /tmp/session.json

# REPORTER (no symbols): relocate + scan + decode -> shareable log
python collect.py $PID /tmp/session.json --out /tmp/sessions.jsonl
```

### What the app reported (ground truth)

```
session 0x6070fd2c8320 id=1000 active=1 requests=100 user=alice
session 0x6070fd2c9360 id=1001 active=0 requests=107 user=bob
session 0x6070fd2c9390 id=1002 active=1 requests=114 user=carol
READY pid=134708
```

### The config `author.py` produced

```json
{
  "class": "_ZTV7Session",
  "module": "demo_target",
  "vtable_offset": 15696,
  "build_id": "f3e8279a81a76545ac4c172c77d22ceb3aaeda70",
  "field_specs": ["8:bool:mActive", "12:i32:mId", "16:u64:mRequests", "24:nscstring:mUser"]
}
```

`vtable_offset` is relative to the module's load base; the reporter turns it into a live
address with `load_bias + offset`. `build_id` lets the reporter confirm it's the same build.

### The log `collect.py` wrote (shared back to the developer)

```json
{"type": "meta", "collected_at": 1784919692, "class": "_ZTV7Session", "module": "demo_target", "build_id_expected": "f3e8...", "build_id_actual": "f3e8...", "build_match": true, "count": 3}
{"type": "object", "addr": "0x6070fd2c8320", "mActive": 1, "mId": 1000, "mRequests": 100, "mUser": "alice"}
{"type": "object", "addr": "0x6070fd2c9360", "mActive": 0, "mId": 1001, "mRequests": 107, "mUser": "bob"}
{"type": "object", "addr": "0x6070fd2c9390", "mActive": 1, "mId": 1002, "mRequests": 114, "mUser": "carol"}
```

Every decoded field matches the app's own output — via relocation and a heap scan, with no
symbols on the reporter's side. The developer analyzes the JSON-lines log however they like,
e.g.:

```bash
# active sessions and their users
grep '"type": "object"' /tmp/sessions.jsonl | jq 'select(.mActive==1) | {mId, mUser}'
```

## Retargeting to Firefox

The pattern is identical against a real Firefox; only two things change:

1. **The class and its offsets.** Use a real vtable symbol (e.g. `_ZTVN7mozilla3dom8WakeLockE`)
   and the field offsets for that build. Generate the offsets from DWARF with the Level 2 tool
   instead of by hand:

   ```bash
   # build with -g (or use a .debug file) so it carries DWARF, then:
   python author.py $PID _ZTV7Session --debuginfo /tmp/demo_target_g --type Session \
       --fields mActive mId mRequests > /tmp/session.json
   # or standalone:  memscout offsets /tmp/demo_target_g Session mActive mId mRequests
   ```

   This needs `pyelftools` (`pip install memscout[authoring]`) and runs only on the developer's
   side; `collect.py` is unchanged.
2. **Resolving on a stripped build.** Release Firefox is stripped, so `author.py`'s `resolve`
   leans on the remote sources (debuginfod / the Mozilla symbol server) that memscout already
   integrates — still entirely on the developer's side. The reporter's `collect.py` is unchanged.

## Notes

- `collect.py` is intentionally a **toolbox composition**, not a framework feature: the log
  format, the one-shot-vs-sampling choice, and the build-id check are all the script's own
  decisions. Copy it and adapt it per investigation.
- It is strictly read-only and never stops the target — safe to ask a reporter to run.
- The build-id check in `collect.py` uses `readelf` via the framework; a maximally
  self-contained reporter script could read the build-id note from the ELF file directly. The
  relocate/scan/decode core needs no external tools.
