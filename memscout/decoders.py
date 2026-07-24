"""Decoder registry: turn the bytes of a named field into a typed Python value.

A decoder is `fn(mem, base, off, arg) -> value`, where `mem` is any reader with
read/read_uint/read_ptr (a Target, or a test double) and `arg` is the optional
suffix of a `type:arg` token (e.g. the `u32` in `atomic:u32`). Fields are named
with `OFF:TYPE:NAME` specs; scripts add their own decoders with register().

Returned value types: ints for numbers, str for strings, and small dicts for
containers/hashtables (so callers get structure, not a pre-formatted string).
"""

# token -> fn(mem, base, off, arg)
_REGISTRY = {}


def register(token, fn):
    """Register (or replace) the decoder for a type token."""
    _REGISTRY[token] = fn


def get(token):
    """The decoder registered for `token`, or None."""
    return _REGISTRY.get(token)


def parse_spec(spec):
    """Split an `OFF:TYPE:NAME` field spec into (offset, type_token, name).

    TYPE may itself contain a colon (`atomic:u32`, `mhashtable:24`): the offset is
    the first `:`-part and the name is the last, so everything between is the type.
    """
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError("bad field spec %r (want OFF:TYPE:NAME)" % spec)
    off = int(parts[0], 0)
    name = parts[-1]
    type_token = ":".join(parts[1:-1])
    return off, type_token, name


def decode_field(mem, base, spec):
    """Decode one `OFF:TYPE:NAME` field of the object at `base`; return (name, value)."""
    off, type_token, name = parse_spec(spec)
    token, _, arg = type_token.partition(":")
    fn = _REGISTRY.get(token)
    if fn is None:
        return name, "<bad-type:%s>" % type_token
    return name, fn(mem, base, off, arg or None)


# --- primitives -------------------------------------------------------------

def _make_int_decoder(size, signed):
    """Build a decoder reading a `size`-byte little-endian int (signed or not)."""
    def dec(mem, base, off, arg):
        val = mem.read_uint(base + off, size)
        if val is None:
            return None
        if signed and val >> (size * 8 - 1):
            val -= 1 << (size * 8)              # sign-extend
        return val
    return dec


for _tok, _sz, _sg in (("u8", 1, False), ("u16", 2, False), ("u32", 4, False),
                       ("u64", 8, False), ("i32", 4, True), ("i64", 8, True),
                       ("bool", 1, False), ("ptr", 8, False)):
    register(_tok, _make_int_decoder(_sz, _sg))


def _decode_atomic(mem, base, off, arg):
    """mozilla::Atomic<T> is layout-compatible with T, so decode as the T named in arg."""
    fn = _REGISTRY.get(arg or "")
    if fn is None:
        return "<bad-atomic:%s>" % arg
    return fn(mem, base, off, None)


register("atomic", _decode_atomic)


# --- Firefox strings --------------------------------------------------------

# All ns*String variants store a data pointer then a uint32 length; only the
# character width differs. Guard against absurd lengths (freed/garbage headers).
_MAX_STRLEN = 4096


def _make_string_decoder(char_bytes, encoding):
    def dec(mem, base, off, arg):
        hdr = mem.read(base + off, 12)          # T* mData; uint32 mLength
        if hdr is None:
            return "<unreadable>"
        ptr = int.from_bytes(hdr[0:8], "little")
        length = int.from_bytes(hdr[8:12], "little")
        if not ptr or length > _MAX_STRLEN:
            return "<len=%d ptr=%#x?>" % (length, ptr)
        raw = mem.read(ptr, length * char_bytes)
        return raw.decode(encoding, "replace") if raw else "<unreadable>"
    return dec


_utf16 = _make_string_decoder(2, "utf-16-le")
_utf8 = _make_string_decoder(1, "utf-8")
for _tok in ("nsstring", "nsastring", "nsautostring"):
    register(_tok, _utf16)
for _tok in ("nscstring", "nsacstring", "nsautocstring"):
    register(_tok, _utf8)


# --- Firefox pointers and containers ---------------------------------------

def _decode_ptr_member(mem, base, off, arg):
    """RefPtr/nsCOMPtr hold a single raw pointer member; return its value."""
    return mem.read_ptr(base + off)


register("refptr", _decode_ptr_member)
register("nscomptr", _decode_ptr_member)


def _decode_nstarray(mem, base, off, arg):
    """nsTArray<T>: a Header* whose first uint32 is the length; elements follow at +8.

    Returns {"length", "data"} where data is the address of element 0. An empty
    array points at a shared zero-length header, so length is read before use.
    """
    hdr = mem.read_ptr(base + off)
    if not hdr:
        return {"length": 0, "data": 0}
    length = mem.read_uint(hdr, 4)
    return {"length": length, "data": hdr + 8}


register("nstarray", _decode_nstarray)


# --- Firefox hashtables (layouts: see DESIGN.md) ---------------------------

def _live_slots(mem, store, capacity, entry_size, count, live_mask):
    """Addresses of live entries in a slot array: first uint32 (keyhash) passes live_mask.

    A slot is live when (keyhash & live_mask) > 1 (0 = free, 1 = removed). Stops
    once `count` live slots are found, so a full table isn't over-walked.
    """
    live = []
    if not (store and entry_size and capacity):
        return live
    for i in range(capacity):
        slot = store + i * entry_size
        keyhash = mem.read_uint(slot, 4)
        if keyhash is not None and (keyhash & live_mask) > 1:
            live.append(slot)
            if count is not None and len(live) >= count:
                break
    return live


def _decode_pldhash(mem, base, off, arg):
    """XPCOM PLDHashTable at base+off -> {count, capacity, entry_size, live[]}.

    Layout (opt build): mEntryStore@8, mHashShift@18, mEntrySize@19, mEntryCount@20.
    Entry size is stored, so live entries are enumerated with no caller input.
    """
    t = base + off
    store = mem.read_ptr(t + 8)
    hash_shift = mem.read_uint(t + 18, 1)
    entry_size = mem.read_uint(t + 19, 1)
    count = mem.read_uint(t + 20, 4)
    if count is None:
        return None
    capacity = (1 << (32 - hash_shift)) if hash_shift else 0
    live = _live_slots(mem, store, capacity, entry_size, count, 0xFFFFFFFF)
    return {"count": count, "capacity": capacity, "entry_size": entry_size, "live": live}


def _decode_mhashtable(mem, base, off, arg):
    """mfbt mozilla::HashMap/HashSet mImpl at base+off -> {count, capacity, live[]}.

    Layout: mGenAndHashShift@0 (low 8 bits = hash shift), mTable@8, mEntryCount@16.
    No stored entry size; pass it as the token arg (`mhashtable:<size>`) to walk
    entries. Live mask ignores the top collision bit. count/capacity work regardless.
    """
    t = base + off
    gen_shift = mem.read_uint(t, 8)
    table = mem.read_ptr(t + 8)
    count = mem.read_uint(t + 16, 4)
    if count is None:
        return None
    hash_shift = (gen_shift & 0xFF) if gen_shift is not None else 0
    capacity = (1 << (32 - hash_shift)) if hash_shift else 0
    entry_size = int(arg, 0) if arg else None
    live = _live_slots(mem, table, capacity, entry_size, count, 0x7FFFFFFF)
    return {"count": count, "capacity": capacity, "live": live}


register("pldhash", _decode_pldhash)
register("mhashtable", _decode_mhashtable)
