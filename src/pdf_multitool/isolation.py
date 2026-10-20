"""Linux native network/process-escape filters; not a filesystem sandbox."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys

from .core import JobError


def install():
    """Fail closed if the advertised Linux filters cannot be installed.

    Filters survive exec, so native parser code and helper binaries receive the
    same restrictions. NPROC in worker._limits bounds forks/threads for a nonroot
    UID; denying setsid/setpgid keeps descendants in the supervised group.
    Filesystem isolation requires an external sandbox and is not claimed here.
    """
    if sys.platform != "linux":
        return {"native_network_filter": False, "process_group_confinement": False}
    name = ctypes.util.find_library("seccomp")
    if not name:
        raise JobError("Linux processing requires libseccomp for native network restrictions")
    lib = ctypes.CDLL(name, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise JobError("Could not allocate native network filter")
    try:
        for syscall in (
            "socket",
            "socketpair",
            "socketcall",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "sendto",
            "sendmsg",
            "sendmmsg",
            "setsid",
            "setpgid",
            "unshare",
            "setns",
            "ptrace",
            "mount",
            "umount2",
            "bpf",
            "open_by_handle_at",
            "io_uring_setup",
        ):
            number = lib.seccomp_syscall_resolve_name(syscall.encode("ascii"))
            if number >= 0 and lib.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0) != 0:
                raise JobError("Could not configure native network/process filter")
        if lib.seccomp_load(context) != 0:
            raise JobError("Host denied native network/process filter installation")
    finally:
        lib.seccomp_release(context)
    return {
        "native_network_filter": True,
        "process_group_confinement": True,
        "uid_process_limit_enforced": os.getuid() != 0,
        "filesystem_isolation": False,
    }
