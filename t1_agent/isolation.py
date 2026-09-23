"""Linux local execution guard: Landlock filesystem allowlist plus seccomp.

PRoot supplies path aliases only; the kernel controls file access and networking.
This local development backend is not a replacement for the official container runtime.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path


def restrict(read_paths: list[str], write_paths: list[str]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    sec = ctypes.CDLL(ctypes.util.find_library("seccomp") or "libseccomp.so.2")
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise RuntimeError("Landlock is unavailable; use the Docker runtime")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13  # REFER
    if abi >= 3:
        handled |= 1 << 14  # TRUNCATE

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    attr = Ruleset(handled)
    fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for path, writable in [(p, False) for p in read_paths] + [
            (p, True) for p in write_paths
        ]:
            p = Path(path)
            if not p.exists():
                continue
            allowed = handled if writable else (1 | (1 << 2) | (1 << 3))
            if not p.is_dir():
                allowed &= 1 | (1 << 1) | (1 << 2) | ((1 << 14) if abi >= 3 else 0)
            parent = os.open(p, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(allowed, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
                    raise OSError(ctypes.get_errno(), "landlock_add_rule")
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)

    sec.seccomp_init.argtypes = [ctypes.c_uint32]
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        for name in (
            "socket",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "mount",
            "mknod",
            "mknodat",
            "umount2",
            "pivot_root",
            "open_by_handle_at",
            "bpf",
            "io_uring_setup",
            "chmod",
            "fchmod",
            "fchmodat",
            "chown",
            "fchown",
            "lchown",
            "fchownat",
            "truncate",
            "truncate64",
        ):
            nr = sec.seccomp_syscall_resolve_name(name.encode())
            if nr >= 0 and sec.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, nr, 0):
                raise RuntimeError("seccomp rule setup failed")
        if sec.seccomp_load(ctx):
            raise RuntimeError("seccomp_load failed")
    finally:
        sec.seccomp_release(ctx)
