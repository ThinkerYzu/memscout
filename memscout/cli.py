"""The `memscout` command: subcommands built on the framework package.

Phase 2.1 ships `modules`. `scan` and `resolve` land once symbol resolution and
the decoder registry exist. Each subcommand is a thin driver over the Target API,
so anything the CLI does is available to a plain `import memscout` script.
"""

import argparse

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
    """Resolve one symbol to its runtime address and print where it came from."""
    with Target(args.pid) as t:
        sym = t.resolve(args.name, module=args.module)
        if sym is None:
            print("symbol %r not found" % args.name)
            return 1
        print("%s = %#x  (%s, size=%s, via %s) in %s"
              % (sym.name, sym.addr, sym.kind, sym.size, sym.source, sym.module.name))


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
        epilog="Field types: u8 u16 u32 u64 i32 i64 bool ptr nsstring nscstring "
               "nstarray refptr atomic:T pldhash mhashtable[:size]. No fields dumps raw slots.")
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
        epilog="Field types: u8 u16 u32 u64 i32 i64 bool ptr nsstring nscstring nstarray "
               "refptr atomic:T pldhash mhashtable[:size]. With no fields, dumps annotated slots.")
    p_dump.add_argument("pid", type=int)
    p_dump.add_argument("addr", type=lambda x: int(x, 0), help="object address (hex or decimal)")
    p_dump.add_argument("fields", nargs="*", help="OFF:TYPE:NAME, repeatable")
    p_dump.add_argument("--count", type=int, default=12,
                        help="number of 8-byte slots to show when no fields are given")
    p_dump.set_defaults(func=_cmd_dump)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
