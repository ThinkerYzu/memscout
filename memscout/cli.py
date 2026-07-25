"""The `memscout` command: subcommands built on the framework package.

Phase 2.1 ships `modules`. `scan` and `resolve` land once symbol resolution and
the decoder registry exist. Each subcommand is a thin driver over the Target API,
so anything the CLI does is available to a plain `import memscout` script.
"""

import argparse
import os
import sys

from . import __version__
from .target import Target


def _cmd_modules(args):
    """List the ELF modules loaded in the target, with build-ids and ranges."""
    with Target(args.pid) as t:
        for m in t.modules:
            bid = m.build_id.hex() if m.build_id else "-"
            span = "%#x-%#x" % (m.ranges[0][0], m.ranges[-1][1])
            print("%-40s bias=%#014x build=%s  %s" % (m.name, m.load_bias, bid, span))


def _cmd_resolve(args):
    """Resolve one symbol to its runtime address and its module-relative (module, offset).

    The `module+offset` form is the load-base-relative address a developer hands to a
    reporter's script, which turns it back into a live address with relocate().
    """
    with Target(args.pid) as t:
        sym = t.resolve(args.name, module=args.module)
        if sym is None:
            print("symbol %r not found" % args.name)
            return 1
        offset = sym.addr - sym.module.load_bias
        print("%s = %#x  =  %s+%#x  (%s, size=%s, via %s)"
              % (sym.name, sym.addr, sym.module.name, offset,
                 sym.kind, sym.size, sym.source))


def _cmd_dump(args):
    """Print the content of the object at an address.

    With `OFF:TYPE:NAME` field specs, decodes those named fields (same decoders as
    `scan`); with none, shows the class-annotated raw slots.
    """
    with Target(args.pid) as t:
        cls = t.identify_class(args.addr)
        head = "object @ %#x" % args.addr
        if cls:
            head += "  (class %s)" % cls
        print(head)
        if args.fields:
            for name, val in t.decode(args.addr, args.fields).items():
                print("  %s = %s" % (name, _fmt_value(val)))
        else:
            for line in t.dump_object(args.addr, count=args.count):
                print(line)


def _cmd_bundle(args):
    """Inline the reporter runtime into a script -> one self-contained file (or stdout)."""
    from . import bundle
    text = bundle.bundle(args.script, minify_runtime=args.minify)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        os.chmod(args.out, 0o755)
        print("wrote self-contained script to %s" % args.out)
    else:
        sys.stdout.write(text)


# For each decoder token: the C/C++ type(s) it decodes, and what the decode returns. Used by
# `memscout decoders` so the command doubles as a C/C++-type -> token reference (`memscout
# offsets` picks the token for you; this is for reading a member's type by hand). The token
# *list* comes from the live registry
# (runtime.registered_tokens), so it can't drift; this only annotates what's there. Tokens
# without an entry still print (flagged), so a newly registered decoder shows up here too.
_DECODER_DOCS = {
    #             C/C++ type(s)                          -> return value
    "u8":  ("uint8_t / unsigned char",                   "int"),
    "u16": ("uint16_t / unsigned short",                 "int"),
    "u32": ("uint32_t / unsigned int",                   "int"),
    "u64": ("uint64_t / size_t / unsigned long",         "int"),
    "i8":  ("int8_t / signed char",                      "int"),
    "i16": ("int16_t / short",                           "int"),
    "i32": ("int32_t / int",                             "int"),
    "i64": ("int64_t / long / ptrdiff_t",                "int"),
    "bool": ("bool (1 byte)",                            "int 0/1"),
    "ptr": ("T* (any raw pointer)",                      "int (the address it holds)"),
    "atomic": ("mozilla::Atomic<T>  [write atomic:<T>]", "as T (e.g. atomic:u32 -> int)"),
    "nsstring": ("nsString / nsAString / nsStringBuffer-backed (UTF-16)", "str"),
    "nsastring": ("nsAString (UTF-16)  [alias of nsstring]", "str"),
    "nsautostring": ("nsAutoString (UTF-16)  [alias of nsstring]", "str"),
    "nscstring": ("nsCString / nsACString (UTF-8)",      "str"),
    "nsacstring": ("nsACString (UTF-8)  [alias of nscstring]", "str"),
    "nsautocstring": ("nsAutoCString (UTF-8)  [alias of nscstring]", "str"),
    "refptr": ("RefPtr<T> / already_AddRefed<T>",        "int (raw pointee address)"),
    "nscomptr": ("nsCOMPtr<T>",                          "int (raw pointee address)"),
    "uniqueptr": ("mozilla::UniquePtr<T> (default deleter)", "int (owned pointer)"),
    "owningnonnull": ("mozilla::OwningNonNull<T>",       "int (raw pointee address)"),
    "nsatom": ("RefPtr<nsAtom> / nsStaticAtom* / nsAtom*", "str (the atom's text)"),
    "maybe": ("mozilla::Maybe<T>  [write maybe:<mIsSome offset>]",
              "{engaged, value}  (value = &T storage)"),
    "nstarray": ("nsTArray<T> / AutoTArray<T,N> / FallibleTArray<T>",
                 "{length, data}  (data = &element[0])"),
    "pldhash": ("mozilla::PLDHashTable (XPCOM nsTHashtable/nsBaseHashtable), opt build",
                "{count, capacity, entry_size, live[]}"),
    "mhashtable": ("mozilla::HashMap<K,V> / HashSet<T> (mfbt)  [write mhashtable[:entry_size]]",
                   "{count, capacity, live[]}"),
    "linkedlist": ("mozilla::LinkedList<T> / AutoCleanLinkedList<T>  [write linkedlist[:max]]",
                   "{count, nodes[]}  (nodes = LinkedListElement addrs)"),
}


def _cmd_decoders(args):
    """List every decoder token, the C/C++ type(s) it maps, and what it returns."""
    from .runtime import registered_tokens
    print("Decoder TYPE tokens for OFF:TYPE:NAME field specs (scan / dump / Reporter.decode).")
    print("Pick the token whose C/C++ type matches the member; "
          "`memscout offsets` picks it for you from the debug info.\n")
    print("  %-14s %-52s %s" % ("token", "C/C++ type(s)", "returns"))
    print("  %-14s %-52s %s" % ("-----", "-------------", "-------"))
    for token in registered_tokens():
        doc = _DECODER_DOCS.get(token)
        if doc is None:
            print("  %-14s %s" % (token, "(custom / undocumented)"))
        else:
            ctype, ret = doc
            print("  %-14s %-52s %s" % (token, ctype, ret))
    print("\nSpec form: OFF:TYPE:NAME (OFF via int(x,0)). Register your own with "
          "memscout.runtime.register(token, fn).")


def _cmd_offsets(args):
    """Developer-side: emit OFF:TYPE:NAME specs for a type from a debug-info ELF (DWARF)."""
    from . import dwarf
    try:
        specs = dwarf.field_specs(args.debuginfo, args.type, args.fields or None)
    except (RuntimeError, ValueError) as e:
        raise SystemExit(str(e))
    for spec in specs:
        print(spec)


def _fmt_value(val):
    """Render a decoded field value for the scan listing: strings quoted, dicts compact."""
    if isinstance(val, str):
        return repr(val)
    if isinstance(val, dict):
        return "{%s}" % ", ".join("%s=%s" % (k, v) for k, v in val.items())
    return str(val)


def _cmd_scan(args):
    """Find live instances of a class by its vtable, decoding named fields.

    Mirrors scripts/procmem-vptr-scan.py's output so the two can be diffed.
    """
    with Target(args.pid) as t:
        sym = t.resolve(args.symbol, module=args.module)
        if sym is None:
            raise SystemExit("symbol %r not found" % args.symbol)
        needle = sym.addr + args.secondary_offset
        regions = list(t.scan_regions(args.include_js))
        bases = t.find_objects(needle, include_js=args.include_js, limit=args.max)
        scanned_mb = sum(hi - lo for lo, hi in regions) / 1048576.0
        print("vptr %#x (from %s): %d hit(s) across %.1fMB"
              % (needle, sym.module.path, len(bases), scanned_mb))
        for base in bases:
            if args.fields:
                decoded = t.decode(base, args.fields)
                cols = " ".join("%s=%s" % (n, _fmt_value(v)) for n, v in decoded.items())
                print("  %#x  %s" % (base, cols))
            elif args.annotate:
                cls = t.identify_class(base)
                print("object @ %#x%s" % (base, "  (class %s)" % cls if cls else ""))
                for line in t.dump_object(base):
                    print(line)
            else:
                print("object @ %#x" % base)
                for line in t.dump_slots(base):
                    print(line)
        print("=> %d object(s)" % len(bases))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="memscout",
        description="Read-only runtime inspection of a live Linux process.")
    parser.add_argument("--version", action="version",
                        version="memscout %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_modules = sub.add_parser("modules", help="list loaded ELF modules")
    p_modules.add_argument("pid", type=int)
    p_modules.set_defaults(func=_cmd_modules)

    p_resolve = sub.add_parser("resolve", help="resolve a symbol to a runtime address")
    p_resolve.add_argument("pid", type=int)
    p_resolve.add_argument("name", help="symbol name (mangled linker symbol)")
    p_resolve.add_argument("--module", help="restrict to this module (basename or suffix)")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_scan = sub.add_parser(
        "scan", help="find live instances of a class by vtable and decode fields",
        epilog="Field TYPEs: run `memscout decoders` for the full authoritative list. "
               "No fields dumps raw slots.")
    p_scan.add_argument("pid", type=int)
    p_scan.add_argument("symbol", help="vtable linker symbol, e.g. _ZTVN7mozilla3dom8WakeLockE")
    p_scan.add_argument("fields", nargs="*", help="OFF:TYPE:NAME, repeatable")
    p_scan.add_argument("--module", help="module defining the vtable (basename or suffix)")
    p_scan.add_argument("--max", type=int, default=1000, help="stop after this many hits")
    p_scan.add_argument("--include-js", action="store_true", help="also scan JS/file-backed regions")
    p_scan.add_argument("--secondary-offset", type=int, default=16,
                        help="vtable header size; override for a multiply-inherited secondary base")
    p_scan.add_argument("--annotate", action="store_true",
                        help="for objects without field specs, print class-aware annotated slots")
    p_scan.set_defaults(func=_cmd_scan)

    p_dump = sub.add_parser(
        "dump", help="print an object's content at an address (class + fields or annotated slots)",
        epilog="Field TYPEs: run `memscout decoders` for the full authoritative list. "
               "With no fields, dumps annotated slots.")
    p_dump.add_argument("pid", type=int)
    p_dump.add_argument("addr", type=lambda x: int(x, 0), help="object address (hex or decimal)")
    p_dump.add_argument("fields", nargs="*", help="OFF:TYPE:NAME, repeatable")
    p_dump.add_argument("--count", type=int, default=12,
                        help="number of 8-byte slots to show when no fields are given")
    p_dump.set_defaults(func=_cmd_dump)

    p_decoders = sub.add_parser(
        "decoders", help="list the decoder type tokens a field spec's TYPE may use",
        epilog="The list is read from the live decoder registry, so it always matches what "
               "scan/dump/Reporter.decode actually accept.")
    p_decoders.set_defaults(func=_cmd_decoders)

    p_offsets = sub.add_parser(
        "offsets", help="(developer/authoring) emit OFF:TYPE:NAME specs for a type from DWARF",
        epilog="Reads a debug-info ELF's DWARF through gdb (needs gdb on PATH; scales to "
               "libxul). Offline authoring aid; the reporter side never runs this. Fields "
               "default to all members in offset order.")
    p_offsets.add_argument("debuginfo", help="ELF file with DWARF (unstripped build or .debug)")
    p_offsets.add_argument("type", help="C++ type name, qualified (e.g. mozilla::dom::WakeLock)")
    p_offsets.add_argument("fields", nargs="*", help="member names to emit (default: all)")
    p_offsets.set_defaults(func=_cmd_offsets)

    p_bundle = sub.add_parser(
        "bundle", help="inline the reporter runtime into a script -> one self-contained file",
        epilog="The script should import primitives `from memscout.runtime import ...`; that "
               "import is stripped and the runtime inlined, so the output runs on a stock Python.")
    p_bundle.add_argument("script", help="developer's collection script (imports memscout.runtime)")
    p_bundle.add_argument("-o", "--out", help="output file (default: stdout); made executable")
    p_bundle.add_argument("--minify", action="store_true",
                          help="strip comments/docstrings from the inlined runtime (needs Python 3.9+)")
    p_bundle.set_defaults(func=_cmd_bundle)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
