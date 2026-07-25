"""Level 2: turn a type's DWARF into memscout field specs -- developer-side, offline.

Given a debug-info ELF (with DWARF) and a C++ type name, this computes each member's
byte offset and maps its type to a decoder token, emitting the `OFF:TYPE:NAME` spec
strings a collection script embeds. It runs on the *developer's* machine while authoring
a script; the reporter never parses DWARF (see SPEC).

    from memscout import dwarf
    dwarf.field_specs("libxul.debug", "mozilla::dom::WakeLock", ["mLocked", "mTopic"])
    # -> ["40:bool:mLocked", "48:nsstring:mTopic"]   (offsets/types from that build)

The DWARF is read through **gdb** (its Python API), not a Python DWARF parser: gdb loads
debug info lazily -- only the one looked-up type is expanded -- so this scales to
Firefox-sized `libxul` debug info that an in-process parser can't hold. `gdb` must be on
PATH; nothing else is needed (no pyelftools).

Member offsets account for inheritance (base-class members are found at their base
offset). Types resolve as: base ints -> u8/u16/u32/u64/i8/i16/i32/i64, bool -> bool,
pointers -> ptr, enums -> the underlying unsigned int, and recognized Firefox classes
(ns*String, nsTArray, RefPtr/nsCOMPtr, the hashtables) -> their decoder tokens. An
unrecognized class member is reported with a `<class ...>` placeholder for the developer
to resolve by hand.
"""

import json
import os
import shutil
import subprocess
import tempfile


class Member:
    """One resolved struct member: its name, byte offset, decoder token, and C++ type text."""

    def __init__(self, name, offset, token, type_desc):
        self.name = name
        self.offset = offset
        self.token = token                      # decoder token, or None if unrecognized
        self.type_desc = type_desc              # human-readable C++ type, for diagnostics

    def spec(self):
        """The `OFF:TYPE:NAME` string, or a commented placeholder if the type is unknown."""
        if self.token is None:
            return "# %d:<%s>:%s  (unrecognized type -- resolve by hand)" % (
                self.offset, self.type_desc, self.name)
        return "%d:%s:%s" % (self.offset, self.token, self.name)


# --- type -> decoder token mapping ------------------------------------------

_UNSIGNED = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}
_SIGNED = {1: "i8", 2: "i16", 4: "i32", 8: "i64"}


def _firefox_token(name):
    """Decoder token for a recognized Firefox class name, or None.

    Names come from stripped template types (e.g. `nsTString<char16_t>`), so matching
    is by prefix/substring. char width picks the string decoder.
    """
    if name.startswith(("nsString", "nsAString", "nsAutoString", "nsLiteralString")):
        return "nsstring"                       # named UTF-16 typedefs
    if name.startswith(("nsCString", "nsACString", "nsAutoCString", "nsLiteralCString")):
        return "nscstring"                      # named 8-bit typedefs
    if name.startswith(("nsTString", "nsTSubstring", "nsTAutoString", "nsTLiteralString")):
        # Template form carries the char type: char16_t -> UTF-16, char -> 8-bit.
        return "nsstring" if "char16" in name else "nscstring"
    if name.startswith(("nsTArray", "AutoTArray", "nsTObserverArray", "FallibleTArray")):
        return "nstarray"
    if name.startswith("nsCOMPtr"):
        return "nscomptr"
    if "nsAtom" in name and name.startswith(("RefPtr", "StaticRefPtr", "nsStaticAtom")):
        return "nsatom"                         # RefPtr<nsAtom>, nsStaticAtom
    if name.startswith(("RefPtr", "StaticRefPtr")):
        return "refptr"
    if name.startswith("UniquePtr"):
        return "uniqueptr"
    if name.startswith("OwningNonNull"):
        return "owningnonnull"
    if name.startswith(("nsTHashtable", "nsBaseHashtable", "nsClassHashtable",
                        "nsInterfaceHashtable", "nsRefPtrHashtable", "nsTHashMap",
                        "nsTHashSet", "PLDHashTable")):
        return "pldhash"
    if "HashMap" in name or "HashSet" in name or name.startswith("mozilla::HashTable"):
        return "mhashtable"
    return None


def _token_for(cat, size, signed, type_name):
    """Map one gdb-extracted member (category/size/signedness/type name) to (token, desc).

    `cat` is the coarse type category from the gdb helper: ptr / bool / int / enum /
    struct / other. token is None for anything unrecognized (caller emits a placeholder).
    """
    if cat == "ptr":
        return "ptr", "pointer"
    if cat == "bool":
        return "bool", type_name or "bool"
    if cat == "int":
        table = _SIGNED if signed else _UNSIGNED
        return table.get(size), type_name or ("int(%d)" % size)
    if cat == "enum":
        return _UNSIGNED.get(size), "enum(%d)" % size
    if cat == "struct":
        return _firefox_token(type_name), "class %s" % type_name
    return None, "<%s>" % (type_name or cat)


# --- gdb extraction ----------------------------------------------------------

# A gdb Python program that dumps a type's flat member layout as one JSON line. gdb reads
# DWARF lazily, so looking up a single type never loads the whole file -- the reason this
# scales to libxul. It walks base classes recursively (adding each base's own offset), skips
# static members and virtual bases (no constant `bitpos`) and the synthetic `_vptr` slot, and
# coarsely categorizes each member's stripped type. Signedness of an int comes from its name
# ("unsigned" appears in every unsigned builtin, e.g. "long unsigned int"), which is stable
# across gdb versions where `Type.is_signed` is not. Output is marked with a sentinel so the
# parent can pick it out of any other gdb chatter.
_SENTINEL = "@@MEMSCOUT@@"
_GDB_EXTRACT = r'''
import gdb, json, os, sys

_CAT = {gdb.TYPE_CODE_PTR: "ptr", gdb.TYPE_CODE_BOOL: "bool",
        gdb.TYPE_CODE_INT: "int", gdb.TYPE_CODE_ENUM: "enum",
        gdb.TYPE_CODE_STRUCT: "struct"}


def _emit(obj):
    sys.stdout.write("%s%s\n" % (os.environ["MEMSCOUT_SENTINEL"], json.dumps(obj)))


def _walk(ty, base, out):
    try:
        fields = ty.fields()
    except Exception:
        return
    for f in fields:
        bitpos = getattr(f, "bitpos", None)
        if bitpos is None:
            continue                            # static member or virtual base
        off = base + bitpos // 8
        ftype = f.type.strip_typedefs()
        if getattr(f, "is_base_class", False):
            _walk(ftype, off, out)
            continue
        if not f.name or f.name.startswith("_vptr"):
            continue
        tname = ftype.name or str(ftype)
        try:
            size = int(ftype.sizeof or 0)
        except Exception:
            size = 0
        out.append({"name": f.name, "offset": off,
                    "cat": _CAT.get(int(ftype.code), "other"),
                    "size": size, "signed": "unsigned" not in tname,
                    "type_name": tname})


def _main():
    name = os.environ["MEMSCOUT_TYPE"]
    try:
        ty = gdb.lookup_type(name).strip_typedefs()
    except Exception as e:
        _emit({"error": "type %r not found (%s)" % (name, e)})
        return
    if int(ty.code) not in (gdb.TYPE_CODE_STRUCT,):
        _emit({"error": "%r is not a class/struct" % name})
        return
    out = []
    _walk(ty, 0, out)
    _emit({"members": out})


_main()
'''


def _extract(path, type_name):
    """Run gdb over `path` to get the flat member layout of `type_name` (a list of dicts).

    Raises RuntimeError if gdb is missing or fails, ValueError if the type isn't found or
    the file carries no usable DWARF.
    """
    if shutil.which("gdb") is None:
        raise RuntimeError(
            "gdb not found on PATH -- memscout reads DWARF through gdb "
            "(install gdb; no Python DWARF parser is required)")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(_GDB_EXTRACT)
        script = f.name
    try:
        env = dict(os.environ, MEMSCOUT_TYPE=type_name, MEMSCOUT_SENTINEL=_SENTINEL)
        proc = subprocess.run(
            ["gdb", "--batch", "-nx", "-x", script, path],
            capture_output=True, text=True, env=env)
    finally:
        os.unlink(script)

    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            payload = json.loads(line[len(_SENTINEL):])
    if payload is None:
        raise RuntimeError("gdb produced no layout for %r in %s\n%s"
                           % (type_name, path, proc.stderr.strip()))
    if "error" in payload:
        raise ValueError("%s in %s" % (payload["error"], path))
    return payload["members"]


def struct_layout(path, type_name):
    """Return the [Member] layout of `type_name` from the DWARF in `path`, via gdb.

    Raises RuntimeError if gdb is missing/unusable, or ValueError if the type isn't found
    or the file has no DWARF. Members are returned in ascending offset order.
    """
    members = []
    for m in _extract(path, type_name):
        token, desc = _token_for(m["cat"], m["size"], m["signed"], m["type_name"])
        members.append(Member(m["name"], m["offset"], token, desc))
    members.sort(key=lambda m: m.offset)
    return members


def field_specs(path, type_name, field_names=None):
    """Return `OFF:TYPE:NAME` spec strings for `type_name`'s fields.

    With `field_names`, returns those fields in the requested order (raising ValueError
    for any missing name); without, returns every member in offset order.
    """
    layout = struct_layout(path, type_name)
    if field_names is None:
        return [m.spec() for m in layout]
    by_name = {m.name: m for m in layout}
    specs = []
    for want in field_names:
        if want not in by_name:
            raise ValueError("field %r not found in %s (have: %s)"
                             % (want, type_name, ", ".join(sorted(by_name))))
        specs.append(by_name[want].spec())
    return specs
