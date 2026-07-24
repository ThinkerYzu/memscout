"""The process's module map: what ELF files are loaded, and where.

Parses /proc/<pid>/maps into one Module per distinct file-backed mapping,
recording its runtime address ranges, its load bias (needed to turn a symbol's
linked address into a runtime one), and its build-id (needed to fetch remote
debug info).
"""

from . import elf


class Module:
    """One ELF file mapped into the target process.

    load_bias is the runtime address of the mapping at file offset 0, i.e. where
    the ELF's base vaddr landed; adding it to a symbol's linked vaddr yields the
    runtime address. ranges are the module's mapped extents, in map order.
    build_id is read lazily from the on-disk file the first time it's asked for.
    """

    def __init__(self, path, load_bias, ranges):
        self.path = path
        self.load_bias = load_bias
        self.ranges = ranges
        self._build_id = _UNSET

    @property
    def name(self):
        """The file's basename, e.g. 'libxul.so' -- how callers usually name it."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def build_id(self):
        """The module's build-id bytes (or None), read from disk on first access."""
        if self._build_id is _UNSET:
            self._build_id = elf.build_id(self.path)
        return self._build_id

    def contains(self, addr):
        """True if addr falls in any of this module's mapped ranges."""
        return any(lo <= addr < hi for lo, hi in self.ranges)

    def __repr__(self):
        return "<Module %s bias=%#x ranges=%d>" % (
            self.name, self.load_bias, len(self.ranges))


# Sentinel distinguishing "build-id not looked up yet" from "looked up, none found".
_UNSET = object()


class ModuleMap:
    """All modules loaded in a process, queryable by name or by address."""

    def __init__(self, modules):
        self.modules = modules

    def __iter__(self):
        return iter(self.modules)

    def by_name(self, name):
        """The module whose path ends in `name` (basename or suffix match), or None."""
        for m in self.modules:
            if m.name == name or m.path.endswith(name):
                return m
        return None

    def for_addr(self, addr):
        """The module whose mapped ranges cover `addr`, or None."""
        for m in self.modules:
            if m.contains(addr):
                return m
        return None


def _is_elf(path):
    """True if the on-disk file at `path` starts with the ELF magic.

    Filters the map down to real ELF modules: non-ELF file mappings (fonts,
    omni.ja archives) and pseudo-files (memfd:, deleted inodes) are excluded, so
    ModuleMap holds only things whose symbols we can resolve. A file that has
    been replaced on disk while mapped (deleted inode) is unreadable here and so
    is dropped -- a rare case worth noting.
    """
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def parse(pid):
    """Build the ModuleMap for a process from /proc/<pid>/maps.

    Groups the map lines by file path: each file's ranges are collected in order,
    and its load bias is taken from the first mapping at file offset 0 (the base
    vaddr's runtime location). Only real ELF files with such a base mapping become
    Modules -- anonymous mappings, pseudo-files, and non-ELF files are skipped.
    """
    order = []                 # paths in first-seen order, for stable output
    ranges = {}                # path -> list[(lo, hi)]
    bias = {}                  # path -> load bias (from the offset-0 mapping)
    for line in open("/proc/%d/maps" % pid):
        parts = line.split()
        if len(parts) < 6:
            continue
        path = parts[5]
        if not path.startswith("/"):
            continue
        lo, hi = (int(x, 16) for x in parts[0].split("-"))
        file_off = int(parts[2], 16)
        if path not in ranges:
            order.append(path)
            ranges[path] = []
        ranges[path].append((lo, hi))
        if file_off == 0 and path not in bias:
            bias[path] = lo

    modules = []
    for path in order:
        # A module needs an offset-0 mapping (its base, to place symbols) and must
        # be a real ELF file; otherwise its symbols can't be resolved against it.
        if path not in bias or not _is_elf(path):
            continue
        modules.append(Module(path, bias[path], ranges[path]))
    return ModuleMap(modules)
