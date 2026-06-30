"""Self-contained sandbox launcher (stdlib only).

Spawned as ``python3 _sandbox_exec.py <uid|-> <gid|-> <landlock:0|1> <workspace>
[extra_ro ...] -- <argv...>``. Running the privilege drop *here* (rather than via
``create_subprocess_exec(user=,group=)``) keeps it event-loop agnostic: uvloop —
which uvicorn uses in production — rejects those kwargs, but a plain launcher
process drops privileges itself with ``setgroups``/``setgid``/``setuid``.

This file is read and parsed by the interpreter *before* the sandbox is applied
(so it needs no special access), then it drops to the low-priv user (if asked),
restricts reads to the workspace + runtime via Landlock (if asked), and
``execvp``s the real command. The exec'd program inherits both, so anything it
spawns (a shell, python, cat) runs unprivileged and cannot read the data dir.

Kept import-free of the sophagent package on purpose: the sandbox denies reading
the project source, which would break ``import sophagent`` here.
"""
import ctypes
import os
import struct
import sys

_NR_create_ruleset = 444
_NR_add_rule = 445
_NR_restrict_self = 446
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# ABI v1 read access rights — confidentiality only (we don't confine writes or
# execution, so normal tool behaviour is preserved).
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_RO = _READ_FILE | _READ_DIR

# Read-only roots the language runtime / common shell tools need. Deliberately
# minimal: broad scratch dirs (/tmp, /var, /opt) are NOT granted — they can hold
# the data dir or other secrets, and the workspace covers the tool's own scratch
# needs. Writes/exec are unconfined, so this only restricts *reads*.
_SYSTEM_ROOTS = (
    "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc", "/dev",
)


def _restrict(workspace: str, extra_ro: list[str]) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    attr = struct.pack("=Q", _RO)  # struct landlock_ruleset_attr { __u64 handled_access_fs; }
    buf = ctypes.create_string_buffer(attr, len(attr))
    rs = libc.syscall(_NR_create_ruleset, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint(0))
    if rs < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")

    def allow(path: str) -> None:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return  # missing path → nothing to grant
        try:
            # struct landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; } packed
            pb = struct.pack("=Qi", _RO, fd)
            pbuf = ctypes.create_string_buffer(pb, len(pb))
            r = libc.syscall(_NR_add_rule, rs, ctypes.c_uint(_RULE_PATH_BENEATH),
                             pbuf, ctypes.c_uint(0))
            if r < 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule({path})")
        finally:
            os.close(fd)

    roots = [
        *_SYSTEM_ROOTS,
        sys.prefix, sys.base_prefix,
        os.path.dirname(os.path.realpath(sys.executable)),
        *extra_ro,
        workspace,
    ]
    for p in roots:
        allow(p)

    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
    r = libc.syscall(_NR_restrict_self, rs, ctypes.c_uint(0))
    os.close(rs)
    if r < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")


def _drop_privs(uid: int, gid: int) -> None:
    """Drop to the low-priv user. ``setgroups([])`` first to shed the root
    parent's supplementary groups (else the child keeps gid 0 and could read
    group-root files); then setgid before setuid (setgid needs privilege we
    lose at setuid)."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main(argv: list[str]) -> "int":
    sep = argv.index("--")
    uid_s, gid_s, landlock_s, workspace = argv[1], argv[2], argv[3], argv[4]
    extra_ro = argv[5:sep]
    inner = argv[sep + 1:]
    # /tmp is not readable inside the sandbox; point temp dirs at the workspace
    # so tempfile write-then-read keeps working.
    for var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[var] = workspace
    if uid_s != "-":
        _drop_privs(int(uid_s), int(gid_s))  # as root, before Landlock
    if landlock_s == "1":
        _restrict(workspace, extra_ro)
    os.execvp(inner[0], inner)  # replaces process image; only returns on failure
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
