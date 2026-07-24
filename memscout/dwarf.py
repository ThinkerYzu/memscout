"""Level 2: turn a type's DWARF into memscout field specs -- developer-side, offline.

Given a debug-info ELF (with DWARF) and a C++ type name, this computes each member's
byte offset and maps its type to a decoder token, emitting the `OFF:TYPE:NAME` spec
strings a collection script embeds. It runs on the *developer's* machine while authoring
a script; the reporter never parses DWARF (see SPEC). It needs `pyelftools` -- an optional
dependency installed only for authoring (`pip install memscout[authoring]`).

    from memscout import dwarf
    dwarf.field_specs("libxul.debug", "mozilla::dom::WakeLock", ["mLocked", "mTopic"])
    # -> ["40:bool:mLocked", "48:nsstring:mTopic"]   (offsets/types from that build)

Member offsets account for inheritance (base-class members are found at their base
offset). Types resolve as: base ints -> u8/u16/u32/u64/i8/i16/i32/i64, bool -> bool,
pointers -> ptr, enums -> the underlying unsigned int, and recognized Firefox classes
(ns*String, nsTArray, RefPtr/nsCOMPtr, the hashtables) -> their decoder tokens. An
unrecognized class member is reported with a `<class ...>` placeholder for the developer
to resolve by hand.
"""

try:
    from elftools.elf.elffile import ELFFile
    _HAVE_PYELFTOOLS = True
except ImportError:                             # optional dependency, authoring-only
    _HAVE_PYELFTOOLS = False


class Member:
    """One resolved struct member: its name, byte offset, decoder token, and DWARF type text."""

    def __init__(self, name, offset, token, type_desc):
        self.name = name
        self.offset = offset
        self.token = token                      # decoder token, or None if unrecognized
        self.type_desc = type_desc              # human-readable DWARF type, for diagnostics

    def spec(self):
        """The `OFF:TYPE:NAME` string, or a commented placeholder if the type is unknown."""
        if self.token is None:
            return "# %d:<%s>:%s  (unrecognized type -- resolve by hand)" % (
                self.offset, self.type_desc, self.name)
        return "%d:%s:%s" % (self.offset, self.token, self.name)


# --- type -> decoder token mapping ------------------------------------------

_UNSIGNED = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}
_SIGNED = {1: "i8", 2: "i16", 4: "i32", 8: "i64"}

# DW_ATE encodings we care about.
_ATE_BOOLEAN = 0x02
_ATE_SIGNED = 0x05
_ATE_SIGNED_CHAR = 0x06


def _strip(die):
    """Follow typedef/const/volatile wrappers to the underlying type DIE."""
    while die is not None and die.tag in (
            "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type"):
        if "DW_AT_type" not in die.attributes:
            break
        die = die.get_DIE_from_attribute("DW_AT_type")
    return die


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
    if name.startswith(("RefPtr", "StaticRefPtr")):
        return "refptr"
    if name.startswith(("nsTHashtable", "nsBaseHashtable", "nsClassHashtable",
                        "nsInterfaceHashtable", "nsRefPtrHashtable", "nsTHashMap",
                        "nsTHashSet", "PLDHashTable")):
        return "pldhash"
    if "HashMap" in name or "HashSet" in name or name.startswith("mozilla::HashTable"):
        return "mhashtable"
    return None


def _type_token(tdie):
    """Return (token, type_desc) for a member's type DIE. token is None if unrecognized."""
    t = _strip(tdie)
    if t is None:
        return None, "void"
    tag = t.tag
    if tag == "DW_TAG_pointer_type":
        return "ptr", "pointer"
    if tag == "DW_TAG_enumeration_type":
        size = t.attributes.get("DW_AT_byte_size")
        n = size.value if size else 4
        return _UNSIGNED.get(n), "enum(%d)" % n
    if tag == "DW_TAG_base_type":
        size = t.attributes["DW_AT_byte_size"].value
        enc = t.attributes.get("DW_AT_encoding")
        encoding = enc.value if enc else None
        desc = t.attributes.get("DW_AT_name")
        desc = desc.value.decode() if desc else "base(%d)" % size
        if encoding == _ATE_BOOLEAN:
            return "bool", desc
        table = _SIGNED if encoding in (_ATE_SIGNED, _ATE_SIGNED_CHAR) else _UNSIGNED
        return table.get(size), desc
    if tag in ("DW_TAG_class_type", "DW_TAG_structure_type"):
        name = t.attributes.get("DW_AT_name")
        nm = name.value.decode() if name else "anon"
        return _firefox_token(nm), "class %s" % nm
    return None, "<%s>" % tag


# --- struct traversal --------------------------------------------------------

def _member_offset(die):
    """Byte offset from DW_AT_data_member_location (constant or DW_OP_plus_uconst form)."""
    attr = die.attributes.get("DW_AT_data_member_location")
    if attr is None:
        return None
    value = attr.value
    if isinstance(value, int):
        return value
    # exprloc: a DW_OP_plus_uconst (0x23) followed by a ULEB128 offset.
    if isinstance(value, (list, bytes)) and value and value[0] == 0x23:
        result = shift = 0
        for byte in value[1:]:
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return result
    return None


def _walk_members(die, base_offset=0):
    """Yield (name, offset, type_die) for members, recursing into base classes."""
    for child in die.iter_children():
        if child.tag == "DW_TAG_member":
            if "DW_AT_data_member_location" not in child.attributes:
                continue                        # static data member -- not in the object
            off = _member_offset(child)
            if off is None:
                continue
            name = child.attributes.get("DW_AT_name")
            if name is None:
                continue
            yield (name.value.decode(), base_offset + off,
                   child.get_DIE_from_attribute("DW_AT_type"))
        elif child.tag == "DW_TAG_inheritance":
            off = _member_offset(child)
            if off is None:
                continue
            base = _strip(child.get_DIE_from_attribute("DW_AT_type"))
            if base is not None:
                yield from _walk_members(base, base_offset + off)


def _qualified_name(die):
    """Fully-qualified name of a DIE from its namespace/class ancestry (e.g. ns::Cls)."""
    parts = []
    cur = die
    while cur is not None:
        if cur.tag in ("DW_TAG_namespace", "DW_TAG_class_type", "DW_TAG_structure_type"):
            name = cur.attributes.get("DW_AT_name")
            if name is not None:
                parts.append(name.value.decode())
        cur = cur.get_parent()
    return "::".join(reversed(parts))


def _find_type_die(dwarfinfo, type_name):
    """Locate the class/struct DIE named `type_name` (qualified or by final component)."""
    simple = type_name.rsplit("::", 1)[-1]
    for cu in dwarfinfo.iter_CUs():
        for die in cu.iter_DIEs():
            if die.tag not in ("DW_TAG_class_type", "DW_TAG_structure_type"):
                continue
            if "DW_AT_declaration" in die.attributes:   # forward decl, no members
                continue
            name = die.attributes.get("DW_AT_name")
            if name is None or name.value.decode() != simple:
                continue
            if "::" in type_name and _qualified_name(die) != type_name:
                continue
            if any(c.tag == "DW_TAG_member" or c.tag == "DW_TAG_inheritance"
                   for c in die.iter_children()):
                return die
    return None


def struct_layout(path, type_name):
    """Return the [Member] layout of `type_name` from the DWARF in `path`.

    Raises RuntimeError if pyelftools is missing, or ValueError if the file has no
    DWARF or the type isn't found.
    """
    if not _HAVE_PYELFTOOLS:
        raise RuntimeError("pyelftools is required for DWARF (pip install memscout[authoring])")
    with open(path, "rb") as f:
        elf = ELFFile(f)
        if not elf.has_dwarf_info():
            raise ValueError("%s has no DWARF debug info" % path)
        dwarfinfo = elf.get_dwarf_info()
        die = _find_type_die(dwarfinfo, type_name)
        if die is None:
            raise ValueError("type %r not found in %s" % (type_name, path))
        members = []
        for name, offset, tdie in _walk_members(die):
            token, desc = _type_token(tdie)
            members.append(Member(name, offset, token, desc))
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
