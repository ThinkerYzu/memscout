# memscout reporter-side reference

Everything a **reporter collection script** can rely on, so you can author one without
reading `memscout/runtime.py`. This is the *self-contained* surface: `from memscout.runtime
import Reporter, register`. It uses the Python standard library only — no symbols, DWARF,
`readelf`, or network. (Symbol/vtable/class-id calls are developer-side `Target` methods and
are **not** available here — see the bottom of this file.)

All addresses are integers. Every read returns `None` (never raises) for memory that isn't
mapped or readable, so guard results.

---

## `Reporter(pid)`

Construct with a target pid; use as a context manager so the memory fd always closes.
Attaches read-only via `/proc/<pid>/mem` (with a non-stopping `PTRACE_SEIZE` fallback under
Yama). Never writes, never stops the process, never detaches.

```python
from memscout.runtime import Reporter
with Reporter(pid) as r:
    ...
```

### Raw / typed reads

| Call | Returns | Notes |
|------|---------|-------|
| `r.read(addr, n)` | `bytes` of length `n`, or `None` | one `pread`; `None` if any of the range is unmapped |
| `r.read_uint(addr, size)` | `int`, or `None` | little-endian unsigned, `size` bytes |
| `r.read_ptr(addr)` | `int`, or `None` | 8-byte pointer-sized value |

### Modules and relocation

| Call | Returns | Notes |
|------|---------|-------|
| `r.modules` | `ModuleMap` | built once, lazily; iterate it, or use the helpers below |
| `r.module(name)` | `Module` or `None` | basename **or** path-suffix match (`"libxul.so"`) |
| `r.relocate(module, offset)` | `int` addr, or `None` | `module.load_bias + offset`; `module` may be a name or a `Module`. `None` if the module isn't loaded |

`ModuleMap`: iterable; `by_name(name)` → `Module`/`None`; `for_addr(addr)` → the module whose
mapped ranges cover `addr`, or `None`.

`Module`: `.name` (basename), `.path`, `.load_bias` (runtime addr of file offset 0),
`.build_id` (bytes, read from the ELF `.note.gnu.build-id` on first access; `None` if absent —
call `.hex()` to compare against a config's `build_id`), `.ranges` (list of `(lo, hi)`),
`.contains(addr)`.

### Heap scanning

| Call | Returns | Notes |
|------|---------|-------|
| `r.scan_regions(include_js=False)` | yields `(lo, hi)` | **writable, anonymous** regions only; skips file-backed maps, `js-gc-heap`, and regions larger than 256 MB |
| `r.find_objects(needle8, include_js=False, limit=1000)` | `list[int]` | addresses where the 8-byte little-endian value `needle8` appears |

**Finding objects by vtable — the `+16` rule.** A vtable *symbol* (`_ZTV…`) addresses the
start of the vtable, but the pointer stored at an object's `+0` points **16 bytes past** it
(past the `offset-to-top` and `typeinfo` words on x86-64 Itanium ABI). So:

```python
needle = r.relocate(module, vtable_offset) + 16      # the value at object+0
bases  = r.find_objects(needle)                       # each is an object's base address
```

**`limit=1000` silently caps results.** If a class can have more instances than you want to
miss, raise `limit` and note in your log if you hit it. `find_objects` scans only the regions
`scan_regions` yields — objects on the **JS GC heap** or in file-backed memory are **not**
found unless you pass `include_js=True` (and even then only the writable non-file case). If a
scan returns 0 and you expected hits, that skip is the usual reason.

### Field decoding

| Call | Returns | Notes |
|------|---------|-------|
| `r.decode(base, fields)` | `dict {name: value}` | `fields` is a space-separated `"OFF:TYPE:NAME …"` string **or** a list of specs |
| `r.dump_slots(base, count=12)` | yields text lines | first `count` 8-byte slots as hex, with a UTF-16 string guess per slot; a layout aid, no symbols |

An unknown type token doesn't raise — the field's value becomes the string
`"<bad-type:TOKEN>"`. A read failure inside a decoder yields `None` or a `"<…>"` sentinel
(see each decoder below), never an exception.

---

## Spec-string grammar

A field spec is `OFF:TYPE:NAME`:

- **`OFF`** — byte offset into the object, parsed with `int(x, 0)`, so decimal (`40`) or hex
  (`0x28`) both work.
- **`NAME`** — the **last** colon-separated segment; it's the key in the output dict.
- **`TYPE`** — everything between the first and last `:`. This means a type may itself carry a
  colon-delimited **argument**: `atomic:u32`, `mhashtable:24`. So
  `24:atomic:u32:mRefCnt` parses as offset `24`, type `atomic:u32`, name `mRefCnt`.

---

## Decoder tokens — and exactly what each returns

`r.decode` maps each token to a Python value. Knowing the **return shape** matters: several
tokens don't give you a final value but a handle you read further from.

The tables below are the **complete** built-in set. To confirm it live (it can't drift from the
code), run **`memscout decoders`** — it prints each token's **C/C++ type(s)** and return value,
so it also serves as the type→token map when you're reading a member's C++ type — or from Python
`memscout.runtime.registered_tokens()`; both read the actual registry. Anything not listed isn't
built in; add it with `register(...)`.

### Scalars

| Token | Returns | Notes |
|-------|---------|-------|
| `u8 u16 u32 u64` | `int` or `None` | little-endian unsigned |
| `i8 i16 i32 i64` | `int` or `None` | sign-extended |
| `bool` | `int` `0`/`1` or `None` | it's a 1-byte read — a Python **int**, not `True`/`False` |
| `ptr` | `int` or `None` | 8-byte raw pointer value (the address it holds) |
| `atomic:T` | whatever `T` returns | `mozilla::Atomic<T>` is layout-compatible with `T`; decodes as `T` (e.g. `atomic:u32`) |

### Firefox strings → a decoded `str`

Tokens `nsstring` / `nsastring` / `nsautostring` (UTF-16) and `nscstring` / `nsacstring` /
`nsautocstring` (UTF-8). The member is read as `{char* mData; uint32 mLength}` at the offset;
the pointed-to bytes are decoded.

Returns a Python `str`, or a sentinel string on trouble: `"<unreadable>"`, or
`"<len=N ptr=0xADDR?>"` when the pointer is null or the length exceeds 4096 (a sanity cap).
Treat a value starting with `<` as "not a real string."

### Pointers and containers — you read further yourself

| Token | Returns | What to do with it |
|-------|---------|--------------------|
| `refptr`, `nscomptr` | `int` or `None` — the raw held pointer (the **pointee's** address) | to see the pointee's fields, `r.decode(that_addr, "…")` or `r.read*` from it |
| `uniqueptr`, `owningnonnull` | `int` or `None` — the owned/held raw pointer | same as `refptr`; `uniqueptr` assumes the default (stateless) deleter, so the pointer is at offset 0 |
| `nsatom` | `str` or `None` — the atom's text | follows a `RefPtr<nsAtom>` / `nsStaticAtom*` member and reads the UTF-16 chars (handles static vs. dynamic atoms). Great for DOM tag/attribute/event names |
| `maybe:<mIsSome_off>` | `dict {"engaged": bool, "value": int}` | `mozilla::Maybe<T>`. `value` is the address of the T storage (offset 0) when engaged, else 0 — decode it with another spec. You must pass the `mIsSome` byte offset (it sits after the T storage, so it depends on `sizeof(T)`) |
| `nstarray` | `dict {"length": int, "data": int}` | `data` is the address of element 0; elements are contiguous. Read element `i` at `data + i * sizeof(T)` — **you** must know `sizeof(T)`; the decoder doesn't |

### Hashtables → counts + live-entry addresses

Both return a dict describing the table and a `live` list of **entry addresses**; you decode
each entry yourself with more specs relative to each slot address. The storage is a split block —
all cached hashes first (`capacity` × 4 bytes), then all entries (`capacity` × `entry_size`) —
so a returned `addr` points at the **entry**, which does *not* contain the key-hash (the hash
lives back in the hashes block). Decode the entry's own fields from `addr`.

| Token | Returns | Notes |
|-------|---------|-------|
| `pldhash` | `{"count", "capacity", "entry_size", "live": [addr, …]}` | XPCOM `PLDHashTable`. Layout is the **opt build**'s; DEBUG builds shift offsets |
| `mhashtable[:entry_size]` | `{"count", "capacity", "live": [addr, …]}` | mfbt `mozilla::HashMap`/`HashSet`. `live` is only populated if you pass the entry size, e.g. `mhashtable:24`, since the stride isn't stored inline |

`live` is capped at `count` entries. Each `addr` is `store + capacity*4 + i*entry_size`
(entries block base + i × entry stride).

### Linked lists → node addresses

| Token | Returns | Notes |
|-------|---------|-------|
| `linkedlist` | `{"count": int, "nodes": [addr, …]}` | `mozilla::LinkedList<T>`. Walks `mNext` from the sentinel (the list object at offset 0) until it loops back. Each `addr` is a `LinkedListElement` — for the usual `class T : public LinkedListElement<T>` that *is* the `T` object (element is the first base at offset 0); otherwise subtract the element's offset within `T`. Decode each node's fields from its `addr` |

### Custom decoders

Register a token before you use it in a spec:

```python
from memscout.runtime import register
# fn(mem, base, off, arg) -> value.  `mem` is the Reporter, so use mem.read_uint / mem.read etc.
# `arg` is the text after the first ':' in the type token (or None): for "mykind:7", arg == "7".
register("mykind", lambda mem, base, off, arg: mem.read_uint(base + off, int(arg or 4)))
```

---

## Config schema (what the developer hands the reporter)

`author.py` emits, and `collect.py` consumes, this dict (ship it **baked into the script** as a
literal for one-file delivery — see SKILL.md step 5):

```json
{
  "class":         "_ZTV7Session",          // vtable linker symbol (informational in the log)
  "module":        "libxul.so",             // module to relocate against + build-id check
  "vtable_offset": 15696,                    // link-relative offset of the vtable symbol
  "build_id":      "f3e8…",                  // hex build-id the offsets were resolved against
  "field_specs":   ["8:bool:mActive", "24:nscstring:mUser"]
}
```

The reporter core is exactly: `needle = relocate(module, vtable_offset) + 16` →
`find_objects(needle)` → `decode(base, field_specs)` per object, plus a build-id check
(`module.build_id.hex() == build_id`) so a mismatched build is flagged, not silently trusted.

---

## Not available on the reporter side

These need symbols/DWARF and live on the developer-side `Target` (a `Reporter` subclass) or the
CLI — **never** call them from a bundled reporter script:

`resolve` (symbol → address), `vtable` / `identify_class` / `dump_object` (reverse vtable
lookup by class), and anything in `memscout.dwarf` / `memscout.symbols` / `memscout.elf`. Do
all of that offline while authoring; ship only relocate + scan + decode.
