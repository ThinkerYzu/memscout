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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
