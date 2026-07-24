"""The raw read channel to a live process: /proc/<pid>/mem, read-only.

This is the only module that touches the target's memory. It preserves the
safety posture proven in scripts/procmem-vptr-scan.py: it never writes, and it
never stops the tracee. If a plain open() of /proc/<pid>/mem is refused by Yama,
it falls back to PTRACE_SEIZE -- which, unlike PTRACE_ATTACH, does not stop the
target -- and never PTRACE_DETACHes, so the tracee keeps running untouched when
memscout exits.
"""

import ctypes
import os


# PTRACE_SEIZE attaches for /proc/mem access without ever stopping the tracee;
# PTRACE_ATTACH would stop it, which we must never do.
_PTRACE_SEIZE = 0x4206


def _ptrace_seize(pid):
    """SEIZE the process so /proc/<pid>/mem opens under Yama, without stopping it.

    Raises OSError on failure; the caller decides whether that is fatal.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    ctypes.set_errno(0)
    res = libc.ptrace(ctypes.c_long(_PTRACE_SEIZE), ctypes.c_long(pid),
                      ctypes.c_void_p(0), ctypes.c_void_p(0))
    if res == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class MemorySource:
    """A read-only handle on one process's memory via /proc/<pid>/mem.

    Open it on a pid, call read(); it returns None (never raises) for memory
    that isn't mapped or readable, so scanners can probe freely. close() drops
    the file descriptor; the SEIZE'd tracee is auto-detached by the kernel and
    keeps running.
    """

    def __init__(self, pid):
        self.pid = pid
        self._fd = self._open(pid)

    @staticmethod
    def _open(pid):
        """Open /proc/<pid>/mem read-only, SEIZE-ing first only if open() is refused."""
        path = "/proc/%d/mem" % pid
        try:
            return os.open(path, os.O_RDONLY)
        except PermissionError:
            pass
        try:
            _ptrace_seize(pid)
        except OSError as e:
            raise SystemExit(
                "cannot read %s and PTRACE_SEIZE failed (%s).\n"
                "Try running as root, or lower Yama restrictions: "
                "echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope" % (path, e))
        return os.open(path, os.O_RDONLY)

    def read(self, addr, n):
        """Return n bytes at addr, or None if that range is unmapped/unreadable."""
        try:
            return os.pread(self._fd, n, addr)
        except OSError:
            return None

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
