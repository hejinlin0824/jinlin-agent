"""L1 Windows sandbox: restricted token + Job Object.

Containment model (honest version)
----------------------------------
* **Job Object** — every command runs in its own job with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.  Timeout / ``close()`` / interpreter
  exit call ``TerminateJobObject``, which kills the whole process tree
  (grandchildren included), not just the root process.
* **Write restriction** — the child runs under a *restricted primary token*
  created with ``CreateRestrictedToken(WRITE_RESTRICTED)``.  The token's
  restricting SID list is ``[<logon session SID>, Everyone]``:

  - ``<logon session SID>`` is granted Modify on the workspace directory
    (``icacls /grant:r *<sid>:(OI)(CI)M``) and removed again on ``close()``,
    so the workspace is writable;
  - ``Everyone`` is required because some DLL initialisation paths (notably
    CPython's) perform write-access checks against objects whose DACL grants
    Everyone but not the logon SID.  Writes therefore remain allowed only on
    paths whose ACL grants ``Everyone`` or the logon SID write access;
  - the user's own SID is deliberately **not** in the restricting list, so the
    user profile (Documents, AppData, ...) is *not* writable by the child even
    though the normal token would allow it.

  Because restricted tokens apply the restricting SIDs only to write checks
  (``WRITE_RESTRICTED``), reads/executes are unaffected.

* **Integrity level** — a Low-integrity variant of the token is attempted
  first.  If the workspace itself is still medium-integrity (the normal case)
  a Low token cannot write it because of the mandatory "no write up" policy;
  in that case the sandbox falls back to the medium-integrity restricted token
  and reports ``details["integrity"] == "medium"`` instead of pretending.
  No persistent relabelling of the user's workspace is performed.

Downgrade behaviour (never lying):
  If the restricted-token path cannot be established (token creation, ACL
  grant or a real write probe fails), the sandbox operates as
  ``level="L1-job"`` / ``engine="job-object"``: process-tree containment is
  still real, filesystem write restriction is **not**, and
  ``details["downgrade_reason"]`` says why.  ``run()``/``popen()`` keep working.

Only ``ctypes`` + the standard library are used (no pywin32).
"""

from __future__ import annotations

import atexit
import codecs
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
import uuid
import weakref
from typing import Any, Callable

from .base import ProcessHandle, RunResult, Sandbox

if os.name != "nt":  # pragma: no cover - import happens only on win32
    raise RuntimeError("WindowsSandbox is only available on Windows")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

WRITE_RESTRICTED = 0x00000008
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ALL_ACCESS = 0xF01FF
TOKEN_USER_CLASS = 1
TOKEN_GROUPS_CLASS = 2
TOKEN_INTEGRITY_LEVEL_CLASS = 25
SE_GROUP_LOGON_ID = 0xC0000000
SE_GROUP_INTEGRITY = 0x00000020
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1
LOW_INTEGRITY_SID = "S-1-16-4096"
EVERYONE_SID = "S-1-1-0"

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
HANDLE_FLAG_INHERIT = 0x00000001

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_SANDBOX_API = "restricted-token+job-object"
_JOB_ONLY_API = "job-object"

# Opens argv[1] for append and closes it immediately: this performs the real
# write-access check without modifying an existing file's contents.
_WRITE_PROBE_CODE = "import sys; open(sys.argv[1], 'ab').close()"

# --------------------------------------------------------------------------- #
# ctypes plumbing
# --------------------------------------------------------------------------- #

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

wt.HANDLE = ctypes.c_void_p


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _decl(name: str, lib: Any, restype: Any, argtypes: list[Any]) -> Any:
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_GetCurrentProcess = _decl("GetCurrentProcess", _kernel32, wt.HANDLE, [])
_CloseHandle = _decl("CloseHandle", _kernel32, wt.BOOL, [wt.HANDLE])
_GetExitCodeProcess = _decl(
    "GetExitCodeProcess", _kernel32, wt.BOOL, [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
)
_WaitForSingleObject = _decl(
    "WaitForSingleObject", _kernel32, wt.DWORD, [wt.HANDLE, wt.DWORD]
)
_TerminateProcess = _decl(
    "TerminateProcess", _kernel32, wt.BOOL, [wt.HANDLE, wt.UINT]
)
_ResumeThread = _decl("ResumeThread", _kernel32, wt.DWORD, [wt.HANDLE])
_CreatePipe = _decl(
    "CreatePipe",
    _kernel32,
    wt.BOOL,
    [
        ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wt.DWORD,
    ],
)
_SetHandleInformation = _decl(
    "SetHandleInformation", _kernel32, wt.BOOL, [wt.HANDLE, wt.DWORD, wt.DWORD]
)
_ReadFile = _decl(
    "ReadFile",
    _kernel32,
    wt.BOOL,
    [
        wt.HANDLE,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ],
)
_WriteFile = _decl(
    "WriteFile",
    _kernel32,
    wt.BOOL,
    [
        wt.HANDLE,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ],
)
_CreateFileW = _decl(
    "CreateFileW",
    _kernel32,
    wt.HANDLE,
    [
        wt.LPCWSTR,
        wt.DWORD,
        wt.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wt.DWORD,
        wt.DWORD,
        wt.HANDLE,
    ],
)
_CreateJobObjectW = _decl(
    "CreateJobObjectW", _kernel32, wt.HANDLE, [ctypes.c_void_p, wt.LPCWSTR]
)
_SetInformationJobObject = _decl(
    "SetInformationJobObject",
    _kernel32,
    wt.BOOL,
    [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD],
)
_AssignProcessToJobObject = _decl(
    "AssignProcessToJobObject", _kernel32, wt.BOOL, [wt.HANDLE, wt.HANDLE]
)
_TerminateJobObject = _decl(
    "TerminateJobObject", _kernel32, wt.BOOL, [wt.HANDLE, wt.UINT]
)
_CreateProcessW = _decl(
    "CreateProcessW",
    _kernel32,
    wt.BOOL,
    [
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ],
)
_CreateProcessAsUserW = _decl(
    "CreateProcessAsUserW",
    _advapi32,
    wt.BOOL,
    [
        wt.HANDLE,
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ],
)
_InitializeProcThreadAttributeList = _decl(
    "InitializeProcThreadAttributeList",
    _kernel32,
    wt.BOOL,
    [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)],
)
_UpdateProcThreadAttribute = _decl(
    "UpdateProcThreadAttribute",
    _kernel32,
    wt.BOOL,
    [
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ],
)
_DeleteProcThreadAttributeList = _decl(
    "DeleteProcThreadAttributeList", _kernel32, None, [ctypes.c_void_p]
)
_LocalFree = _decl("LocalFree", _kernel32, ctypes.c_void_p, [ctypes.c_void_p])

# advapi32
_OpenProcessToken = _decl(
    "OpenProcessToken",
    _advapi32,
    wt.BOOL,
    [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)],
)
_GetTokenInformation = _decl(
    "GetTokenInformation",
    _advapi32,
    wt.BOOL,
    [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)],
)
_CreateRestrictedToken = _decl(
    "CreateRestrictedToken",
    _advapi32,
    wt.BOOL,
    [
        wt.HANDLE,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wt.HANDLE),
    ],
)
_DuplicateTokenEx = _decl(
    "DuplicateTokenEx",
    _advapi32,
    wt.BOOL,
    [
        wt.HANDLE,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wt.HANDLE),
    ],
)
_SetTokenInformation = _decl(
    "SetTokenInformation",
    _advapi32,
    wt.BOOL,
    [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD],
)
_ConvertStringSidToSidW = _decl(
    "ConvertStringSidToSidW",
    _advapi32,
    wt.BOOL,
    [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)],
)
_ConvertSidToStringSidW = _decl(
    "ConvertSidToStringSidW",
    _advapi32,
    wt.BOOL,
    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
)
_GetLengthSid = _decl("GetLengthSid", _advapi32, wt.DWORD, [ctypes.c_void_p])


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _last_error() -> int:
    return ctypes.get_last_error()


def _win_error(what: str) -> OSError:
    err = _last_error()
    return OSError(0, f"{what} failed (winerror {err})")


def _close_handle(*handles: Any) -> None:
    for handle in handles:
        if handle:
            try:
                _CloseHandle(handle)
            except Exception:
                pass


def _sid_ptr_to_string(ptr: Any) -> str:
    out = ctypes.c_void_p()
    if not _ConvertSidToStringSidW(ctypes.c_void_p(ptr), ctypes.byref(out)):
        raise _win_error("ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _LocalFree(ctypes.c_void_p(out.value))


def _sid_string_to_ptr(sid: str) -> ctypes.c_void_p:
    ptr = ctypes.c_void_p()
    if not _ConvertStringSidToSidW(sid, ctypes.byref(ptr)):
        raise _win_error(f"ConvertStringSidToSidW({sid})")
    return ptr


def _open_current_token(access: int) -> Any:
    token = wt.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), access, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    return token


def _current_user_sid() -> str:
    token = _open_current_token(TOKEN_QUERY)
    try:
        size = wt.DWORD(0)
        _GetTokenInformation(
            token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size)
        )
        if not size.value:
            raise _win_error("GetTokenInformation(TokenUser,size)")
        buf = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(
            token, TOKEN_USER_CLASS, buf, size.value, ctypes.byref(size)
        ):
            raise _win_error("GetTokenInformation(TokenUser)")
        entry = ctypes.cast(buf, ctypes.POINTER(_SID_AND_ATTRIBUTES))[0]
        return _sid_ptr_to_string(entry.Sid)
    finally:
        _close_handle(token)


def _current_logon_sid() -> str | None:
    """Return the token's logon-session SID (the one that owns the desktop)."""
    token = _open_current_token(TOKEN_QUERY)
    try:
        size = wt.DWORD(0)
        _GetTokenInformation(
            token, TOKEN_GROUPS_CLASS, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(
            token, TOKEN_GROUPS_CLASS, buf, size.value, ctypes.byref(size)
        ):
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
        raw = buf.raw
        offset = ctypes.sizeof(ctypes.c_void_p)  # DWORD GroupCount + alignment
        stride = ctypes.sizeof(_SID_AND_ATTRIBUTES)
        for index in range(count):
            start = offset + index * stride
            entry = _SID_AND_ATTRIBUTES.from_buffer_copy(raw[start : start + stride])
            if entry.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
                return _sid_ptr_to_string(entry.Sid)
        return None
    finally:
        _close_handle(token)


def _create_restricted_token(sids: list[str], low_integrity: bool) -> Any:
    """Create (and duplicate as primary) a WRITE_RESTRICTED token."""
    # Fallback note: CreateProcessWithTokenW was evaluated and rejected.  It
    # requires SeImpersonatePrivilege (absent for an ordinary user; verified on
    # a standard account: winerror 1314) and does not inherit std handles, so
    # it cannot route the stdout/stderr pipes.  CreateProcessAsUser *does* work
    # with a restricted version of the caller's own token without
    # SeAssignPrimaryToken/SeIncreaseQuota (verified), and is the path used
    # here.  If it ever fails, the real workspace write probe below fails and
    # the sandbox downgrades to job-object honestly.
    base = _open_current_token(TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY)
    sid_ptrs: list[ctypes.c_void_p] = []
    restricted = wt.HANDLE()
    try:
        array = (_SID_AND_ATTRIBUTES * max(1, len(sids)))()
        for index, sid in enumerate(sids):
            ptr = _sid_string_to_ptr(sid)
            sid_ptrs.append(ptr)
            array[index].Sid = ptr
            array[index].Attributes = 0
        ok = _CreateRestrictedToken(
            base,
            WRITE_RESTRICTED,
            0,
            None,
            0,
            None,
            len(sids),
            ctypes.cast(array, ctypes.c_void_p) if sids else None,
            ctypes.byref(restricted),
        )
        if not ok:
            raise _win_error("CreateRestrictedToken")
    finally:
        _close_handle(base)
        for ptr in sid_ptrs:
            _LocalFree(ctypes.c_void_p(ptr.value) if ptr.value else None)

    primary = wt.HANDLE()
    try:
        if not _DuplicateTokenEx(
            restricted,
            TOKEN_ALL_ACCESS,
            None,
            SECURITY_IMPERSONATION,
            TOKEN_PRIMARY,
            ctypes.byref(primary),
        ):
            raise _win_error("DuplicateTokenEx")
    finally:
        _close_handle(restricted)

    if low_integrity:
        try:
            _set_token_integrity_low(primary)
        except Exception:
            _close_handle(primary)
            raise
    return primary


def _set_token_integrity_low(token: Any) -> None:
    sid_ptr = _sid_string_to_ptr(LOW_INTEGRITY_SID)
    try:
        label = _SID_AND_ATTRIBUTES()
        label.Sid = sid_ptr
        label.Attributes = SE_GROUP_INTEGRITY
        size = ctypes.sizeof(_SID_AND_ATTRIBUTES) + _GetLengthSid(sid_ptr)
        if not _SetTokenInformation(
            token,
            TOKEN_INTEGRITY_LEVEL_CLASS,
            ctypes.byref(label),
            size,
        ):
            raise _win_error("SetTokenInformation(TokenIntegrityLevel)")
    finally:
        _LocalFree(ctypes.c_void_p(sid_ptr.value))


def _create_job() -> Any:
    job = _CreateJobObjectW(None, None)
    if not job:
        raise _win_error("CreateJobObjectW")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _close_handle(job)
        raise _win_error("SetInformationJobObject")
    return job


def _make_handle_list(handles: list[Any]) -> tuple[Any, Any]:
    """Build a PROC_THREAD_ATTRIBUTE_HANDLE_LIST so only std handles inherit."""
    size = ctypes.c_size_t(0)
    _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise _win_error("InitializeProcThreadAttributeList(size)")
    buffer = ctypes.create_string_buffer(size.value)
    if not _InitializeProcThreadAttributeList(
        buffer, 1, 0, ctypes.byref(size)
    ):
        raise _win_error("InitializeProcThreadAttributeList")
    handle_array = (wt.HANDLE * len(handles))(*handles)
    if not _UpdateProcThreadAttribute(
        buffer,
        0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.cast(handle_array, ctypes.c_void_p),
        ctypes.sizeof(handle_array),
        None,
        None,
    ):
        _DeleteProcThreadAttributeList(buffer)
        raise _win_error("UpdateProcThreadAttribute(handle list)")
    return buffer, handle_array


def _create_pipe(inherit_read: bool) -> tuple[Any, Any]:
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    read_end = wt.HANDLE()
    write_end = wt.HANDLE()
    if not _CreatePipe(
        ctypes.byref(read_end), ctypes.byref(write_end), ctypes.byref(sa), 0
    ):
        raise _win_error("CreatePipe")
    parent_end = write_end if inherit_read else read_end
    if not _SetHandleInformation(
        parent_end, HANDLE_FLAG_INHERIT, 0
    ):
        _close_handle(read_end, write_end)
        raise _win_error("SetHandleInformation")
    return read_end, write_end


def _create_nul(access: int) -> Any:
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    handle = _CreateFileW(
        "NUL",
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        ctypes.byref(sa),
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == wt.HANDLE(-1).value:
        raise _win_error("CreateFileW(NUL)")
    return handle


def _environment_block(env: dict[str, str] | None) -> Any:
    merged: dict[str, str] = {str(k): str(v) for k, v in os.environ.items()}
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    # Forced, not merely defaulted: the sandbox decodes UTF-8.
    merged["PYTHONUTF8"] = "1"
    merged["PYTHONIOENCODING"] = "utf-8:replace"
    entries = [f"{key}={value}" for key, value in merged.items() if "=" not in key]
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _write_all(handle: Any, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        chunk = bytes(view[offset : offset + 65536])
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        written = wt.DWORD(0)
        if not _WriteFile(
            handle,
            ctypes.cast(buf, ctypes.c_void_p),
            len(chunk),
            ctypes.byref(written),
            None,
        ):
            raise _win_error("WriteFile")
        if written.value == 0:
            raise OSError("WriteFile wrote 0 bytes")
        offset += written.value


# --------------------------------------------------------------------------- #
# spawned process bookkeeping
# --------------------------------------------------------------------------- #


class _Spawned:
    """A live child process, its job handle and its stdio reader threads."""

    def __init__(
        self,
        pid: int,
        hprocess: Any,
        job: Any,
        stdin_write: Any,
        stdout_read: Any,
        stderr_read: Any,
        on_job_closed: Callable[[Any], None],
    ) -> None:
        self.pid = pid
        self.hprocess = hprocess
        self.job = job
        self.stdin_write = stdin_write
        self.stdout_read = stdout_read
        self.stderr_read = stderr_read
        self.out_bytes = bytearray()
        self.err_bytes = bytearray()
        self.lock = threading.Lock()
        self._on_job_closed = on_job_closed
        self._exit_code: int | None = None
        self._closed = False
        self._readers = [
            threading.Thread(
                target=self._read_loop,
                args=(stdout_read, self.out_bytes),
                daemon=True,
                name=f"jinlin-sandbox-out-{pid}",
            ),
            threading.Thread(
                target=self._read_loop,
                args=(stderr_read, self.err_bytes),
                daemon=True,
                name=f"jinlin-sandbox-err-{pid}",
            ),
        ]
        for thread in self._readers:
            thread.start()

    # -- io ---------------------------------------------------------------
    def _read_loop(self, handle: Any, sink: bytearray) -> None:
        buffer = ctypes.create_string_buffer(8192)
        try:
            while True:
                got = wt.DWORD(0)
                ok = _ReadFile(
                    handle,
                    ctypes.cast(buffer, ctypes.c_void_p),
                    8192,
                    ctypes.byref(got),
                    None,
                )
                if not ok or got.value == 0:
                    return
                with self.lock:
                    sink.extend(buffer.raw[: got.value])
        except Exception:
            return

    def join_readers(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        for thread in self._readers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def drain_bytes(self) -> tuple[bytes, bytes]:
        with self.lock:
            out, err = bytes(self.out_bytes), bytes(self.err_bytes)
            self.out_bytes.clear()
            self.err_bytes.clear()
        return out, err

    # -- process ----------------------------------------------------------
    def poll(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        code = wt.DWORD(0)
        if not _GetExitCodeProcess(self.hprocess, ctypes.byref(code)):
            return None
        if code.value == STILL_ACTIVE:
            return None
        self._exit_code = code.value
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        milliseconds = INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = _WaitForSingleObject(self.hprocess, milliseconds)
        if result == WAIT_TIMEOUT:
            return None
        if result == WAIT_FAILED:
            raise _win_error("WaitForSingleObject")
        return self.poll()

    def terminate_tree(self) -> None:
        if self.job:
            try:
                _TerminateJobObject(self.job, 124)
            except Exception:
                pass
        if self.hprocess:
            try:
                _WaitForSingleObject(self.hprocess, 5000)
            except Exception:
                pass
        self.poll()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.terminate_tree()
        self.join_readers(5.0)
        _close_handle(self.stdin_write)
        self.stdin_write = None
        # Readers may still be running if a pipe leaked; they are daemons and
        # every process holding the write end has been killed above.
        job = self.job
        _close_handle(self.stdout_read, self.stderr_read, self.hprocess, job)
        self.stdout_read = self.stderr_read = self.hprocess = self.job = None
        try:
            if self._on_job_closed is not None:
                self._on_job_closed(job)
        except Exception:
            pass

    def __del__(self) -> None:  # pragma: no cover - GC dependent
        try:
            self.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# public sandbox
# --------------------------------------------------------------------------- #


class _PopenHandle:
    """``ProcessHandle`` implementation for a job-contained child."""

    def __init__(self, spawned: _Spawned) -> None:
        self._spawned = spawned
        self.pid: int | None = spawned.pid
        self._closed = False
        self._dec_out = codecs.getincrementaldecoder("utf-8")("replace")
        self._dec_err = codecs.getincrementaldecoder("utf-8")("replace")

    def poll(self) -> int | None:
        return self._spawned.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        return self._spawned.wait(timeout)

    def read_available(self) -> tuple[str, str]:
        raw_out, raw_err = self._spawned.drain_bytes()
        out = self._dec_out.decode(raw_out) if raw_out else ""
        err = self._dec_err.decode(raw_err) if raw_err else ""
        return out, err

    def terminate_tree(self) -> None:
        self._spawned.terminate_tree()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._spawned.close()


_SANDBOXES: "weakref.WeakSet[WindowsSandbox]" = weakref.WeakSet()
_ATEXIT_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _cleanup_all() -> None:
    for sandbox in list(_SANDBOXES):
        try:
            sandbox.close()
        except Exception:
            pass


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    with _ATEXIT_LOCK:
        if _ATEXIT_REGISTERED:
            return
        _ATEXIT_REGISTERED = True
        atexit.register(_cleanup_all)


class WindowsSandbox(Sandbox):
    """L1 sandbox for Windows.  Construction raises only if *no* containment
    (not even a job object) can be established; the factory then falls back to
    L0.  Anything short of full L1 is reported through ``level``/``engine`` /
    ``details["downgrade_reason"]``.
    """

    level = "L1-job"
    engine = _JOB_ONLY_API

    def __init__(self, workspace: str, config: Any = None) -> None:
        super().__init__(str(workspace))
        self.workspace = os.path.abspath(str(workspace))
        self._config = config
        self._lock = threading.RLock()
        self._closed = False
        self._live_jobs: dict[int, Any] = {}
        self._token: Any = None
        self._ace_sid: str | None = None
        self.level = "L1-job"
        self.engine = _JOB_ONLY_API
        self.details: dict[str, Any] = {
            "has_job": False,
            "restricted_token": False,
            "integrity": "unknown",
            "downgrade_reason": None,
            "job_kill_on_close": False,
        }

        # 1) Job Object support is mandatory: without it this sandbox makes no
        #    containment promise at all, so let the factory downgrade to L0.
        self._verify_job_support()
        self.details["has_job"] = True
        self.details["job_kill_on_close"] = True

        # 2) Try the full restricted-token path.
        downgrade = self._setup_restricted_token()

        # 3) If the token path is unavailable, validate that the job-only path
        #    really works before advertising L1-job.
        if downgrade is not None:
            if not self._job_only_probe():
                raise RuntimeError(
                    f"Windows L1 unavailable: restricted token failed ({downgrade}) "
                    "and job-object containment could not be verified"
                )
            self._downgrade(downgrade)

        _SANDBOXES.add(self)
        _register_atexit()

    # ------------------------------------------------------------- lifecycle

    def describe(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "engine": self.engine,
            "workspace": self.workspace,
            **self.details,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._live_jobs.values())
            self._live_jobs.clear()
            token = self._token
            self._token = None
            ace_sid = self._ace_sid
            self._ace_sid = None
        for job in jobs:
            try:
                _TerminateJobObject(job, 1)
            except Exception:
                pass
        _close_handle(token)
        if ace_sid:
            self._remove_workspace_ace(ace_sid)
        _SANDBOXES.discard(self)

    def __enter__(self) -> "WindowsSandbox":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - GC dependent
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------ capability

    def _verify_job_support(self) -> None:
        job = _create_job()  # raises on failure
        _close_handle(job)

    def _setup_restricted_token(self) -> str | None:
        """Return a downgrade reason, or ``None`` when the token path works."""
        if not os.path.isdir(self.workspace):
            return f"workspace is not a directory: {self.workspace}"
        try:
            user_sid = _current_user_sid()
            logon_sid = _current_logon_sid()
        except Exception as exc:
            return f"cannot read the current token's SIDs: {exc}"

        # The logon SID owns the interactive desktop (some DLLs need it); the
        # user SID is intentionally absent so the profile stays read-only.
        if logon_sid:
            restricting = [logon_sid, EVERYONE_SID]
            ace_sid = logon_sid
        else:  # pragma: no cover - practically always present
            restricting = [user_sid, EVERYONE_SID]
            ace_sid = user_sid

        if not self._add_workspace_ace(ace_sid):
            return "cannot grant the workspace a write ACE for the sandbox SID"
        self._ace_sid = ace_sid

        prober = os.path.join(self.workspace, f".jinlin_probe_{uuid.uuid4().hex}")
        medium_token = None
        low_token = None
        try:
            try:
                medium_token = _create_restricted_token(restricting, low_integrity=False)
            except Exception as exc:
                return f"CreateRestrictedToken failed: {exc}"

            if not self._token_can_write(medium_token, prober):
                return "the restricted token cannot write inside the workspace"

            try:
                low_token = _create_restricted_token(restricting, low_integrity=True)
            except Exception:
                low_token = None

            if low_token is not None and self._token_can_write(low_token, prober):
                self._token = low_token
                low_token = None
                self.details["integrity"] = "low"
                self.details["integrity_note"] = (
                    "the restricted token was lowered to Low integrity; the "
                    "workspace is writable at Low integrity"
                )
            else:
                self._token = medium_token
                medium_token = None
                self.details["integrity"] = "medium"
                self.details["integrity_note"] = (
                    "low-integrity token rejected by a real workspace write probe: "
                    "a medium-integrity workspace is not writable from Low IL "
                    "(mandatory no-write-up policy); no integrity relabelling of "
                    "the user's workspace was performed"
                )
            self.details["restricted_token"] = True
            self.details["restricting_sids"] = list(restricting)
            self.details["workspace_acl_sid"] = ace_sid
            self.details["workspace_acl_granted"] = True
            self.details["process_creation"] = "CreateProcessAsUserW"
            self.level = "L1"
            self.engine = _SANDBOX_API
            return None
        finally:
            _close_handle(medium_token, low_token)
            try:
                if os.path.exists(prober):
                    os.remove(prober)
            except OSError:
                pass

    def _job_only_probe(self) -> bool:
        """Spawn a trivial command with a job object, no token, and reap it."""
        spawned = None
        try:
            spawned = self._spawn(
                [sys.executable, "-c", "pass"],
                self.workspace,
                None,
                None,
                token=None,
            )
            code = spawned.wait(30.0)
            return code == 0
        except Exception:
            return False
        finally:
            if spawned is not None:
                spawned.close()

    def _downgrade(self, reason: str) -> None:
        token = self._token
        self._token = None
        _close_handle(token)
        if self._ace_sid:
            self._remove_workspace_ace(self._ace_sid)
            self._ace_sid = None
        self.level = "L1-job"
        self.engine = _JOB_ONLY_API
        self.details["restricted_token"] = False
        self.details["integrity"] = "none (job-object only)"
        self.details["downgrade_reason"] = reason
        self.details["process_creation"] = "CreateProcessW"
        self.details.pop("restricting_sids", None)
        self.details.pop("workspace_acl_sid", None)
        self.details.pop("workspace_acl_granted", None)

    def _token_can_write(self, token: Any, target: str) -> bool:
        spawned = None
        try:
            spawned = self._spawn(
                [sys.executable, "-c", _WRITE_PROBE_CODE, target],
                self.workspace,
                None,
                None,
                token=token,
            )
            code = spawned.wait(30.0)
            return code == 0
        except Exception:
            return False
        finally:
            if spawned is not None:
                spawned.close()
            try:
                if os.path.exists(target):
                    os.remove(target)
            except OSError:
                pass

    def _run_icacls(self, args: list[str]) -> bool:
        try:
            completed = subprocess.run(
                ["icacls", *args],
                capture_output=True,
                timeout=180,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            return completed.returncode == 0
        except Exception:
            return False

    def _add_workspace_ace(self, sid: str) -> bool:
        return self._run_icacls(
            [self.workspace, "/grant:r", f"*{sid}:(OI)(CI)M"]
        )

    def _remove_workspace_ace(self, sid: str) -> None:
        self._run_icacls([self.workspace, "/remove", f"*{sid}"])

    # ---------------------------------------------------------------- spawn

    def _ensure_usable(self) -> None:
        if self._closed:
            raise RuntimeError("sandbox is closed")

    def _spawn(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stdin_data: str | None,
        token: Any = -1,
    ) -> _Spawned:
        """Create one process inside a fresh job, with piped stdio."""
        if not argv:
            raise ValueError("argv must not be empty")
        self._ensure_usable()
        effective_token = self._token if token == -1 else token
        command = subprocess.list2cmdline([str(part) for part in argv])
        command_buffer = ctypes.create_unicode_buffer(command)
        environment = _environment_block(env)
        working_dir = cwd or self.workspace

        job = _create_job()
        stdin_read = stdin_write = None
        stdout_read = stdout_write = None
        stderr_read = stderr_write = None
        nul_handle = None
        attribute_buffer = None
        handle_array = None
        process_info = _PROCESS_INFORMATION()
        process_started = False
        try:
            if stdin_data is not None:
                stdin_read, stdin_write = _create_pipe(inherit_read=True)
                child_stdin = stdin_read
            else:
                nul_handle = _create_nul(GENERIC_READ | GENERIC_WRITE)
                child_stdin = nul_handle
            stdout_read, stdout_write = _create_pipe(inherit_read=False)
            stderr_read, stderr_write = _create_pipe(inherit_read=False)

            attribute_buffer, handle_array = _make_handle_list(
                [child_stdin, stdout_write, stderr_write]
            )

            startup = _STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = child_stdin
            startup.StartupInfo.hStdOutput = stdout_write
            startup.StartupInfo.hStdError = stderr_write
            startup.lpAttributeList = ctypes.cast(
                attribute_buffer, ctypes.c_void_p
            )

            flags = (
                CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
            )
            if effective_token is not None:
                ok = _CreateProcessAsUserW(
                    effective_token,
                    None,
                    command_buffer,
                    None,
                    None,
                    True,
                    flags,
                    ctypes.cast(environment, ctypes.c_void_p),
                    working_dir,
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
                what = "CreateProcessAsUserW"
            else:
                ok = _CreateProcessW(
                    None,
                    command_buffer,
                    None,
                    None,
                    True,
                    flags,
                    ctypes.cast(environment, ctypes.c_void_p),
                    working_dir,
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
                what = "CreateProcessW"
            if not ok:
                raise _win_error(what)
            process_started = True

            # Child-side pipe ends are now duplicated into the child.
            _close_handle(stdin_read, stdout_write, stderr_write, nul_handle)
            stdin_read = stdout_write = stderr_write = nul_handle = None

            if not _AssignProcessToJobObject(job, process_info.hProcess):
                raise _win_error("AssignProcessToJobObject")
            if _ResumeThread(process_info.hThread) == 0xFFFFFFFF:
                raise _win_error("ResumeThread")
        except Exception:
            if process_started and process_info.hProcess:
                try:
                    _TerminateProcess(process_info.hProcess, 1)
                    _WaitForSingleObject(process_info.hProcess, 5000)
                except Exception:
                    pass
            _close_handle(
                process_info.hThread,
                process_info.hProcess,
                stdin_read,
                stdin_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                nul_handle,
                job,
            )
            raise
        finally:
            if attribute_buffer is not None:
                _DeleteProcThreadAttributeList(attribute_buffer)
            _close_handle(process_info.hThread)

        spawned = _Spawned(
            pid=int(process_info.dwProcessId),
            hprocess=process_info.hProcess,
            job=job,
            stdin_write=stdin_write,
            stdout_read=stdout_read,
            stderr_read=stderr_read,
            on_job_closed=self._forget_job,
        )
        try:
            self._remember_job(job)
        except Exception:
            spawned.close()
            raise
        if stdin_data is not None and spawned.stdin_write:
            try:
                _write_all(spawned.stdin_write, stdin_data.encode("utf-8"))
            except Exception:
                pass
            finally:
                _close_handle(spawned.stdin_write)
                spawned.stdin_write = None
        return spawned

    def _remember_job(self, job: Any) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("sandbox is closed")
            self._live_jobs[int(job)] = job

    def _forget_job(self, job: Any) -> None:
        if job is None:
            return
        with self._lock:
            self._live_jobs.pop(int(job), None)

    # ------------------------------------------------------------------ run

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
        max_output_chars: int = 200_000,
    ) -> RunResult:
        started = time.monotonic()
        spawned: _Spawned | None = None
        timed_out = False
        try:
            self._ensure_usable()
            spawned = self._spawn(
                argv,
                cwd,
                env,
                stdin_data,
                token=-1,
            )
            milliseconds = (
                INFINITE if timeout is None else max(0, int(timeout * 1000))
            )
            result = _WaitForSingleObject(spawned.hprocess, milliseconds)
            if result == WAIT_FAILED:
                raise _win_error("WaitForSingleObject")
            if result == WAIT_TIMEOUT:
                timed_out = True
            # Reap the whole tree: kills leaked descendants and guarantees the
            # pipe readers see EOF.  The root's exit code is already final.
            spawned.terminate_tree()
            spawned.join_readers(10.0)
            exit_code = spawned.poll()
            raw_out, raw_err = spawned.drain_bytes()
            out = raw_out.decode("utf-8", "replace")
            err = raw_err.decode("utf-8", "replace")
            truncated_out = len(out) > max_output_chars
            truncated_err = len(err) > max_output_chars
            return RunResult(
                exit_code=exit_code,
                stdout=out[:max_output_chars],
                stderr=err[:max_output_chars],
                timed_out=timed_out,
                duration_ms=int((time.monotonic() - started) * 1000),
                engine=self.engine,
                pid=spawned.pid,
                truncated_stdout=truncated_out,
                truncated_stderr=truncated_err,
            )
        except Exception as exc:
            return RunResult(
                exit_code=None,
                timed_out=timed_out,
                duration_ms=int((time.monotonic() - started) * 1000),
                engine=self.engine,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if spawned is not None:
                spawned.close()

    def popen(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        self._ensure_usable()
        spawned = self._spawn(argv, cwd, env, None, token=-1)
        return _PopenHandle(spawned)

    # ----------------------------------------------------------- probe_write

    def probe_write(self, path: str) -> tuple[bool, str]:
        """Really try to write ``path`` the way a sandboxed command would."""
        target = str(path)
        is_directory = os.path.isdir(target)
        if is_directory:
            target = os.path.join(target, f".jinlin_probe_{uuid.uuid4().hex}")
        existed = os.path.exists(target)
        spawned: _Spawned | None = None
        try:
            spawned = self._spawn(
                [sys.executable, "-c", _WRITE_PROBE_CODE, target],
                self.workspace,
                None,
                None,
                token=-1,
            )
            spawned.wait(30.0)
            exit_code = spawned.poll()
            raw_out, raw_err = spawned.drain_bytes()
            allowed = exit_code == 0
            if allowed:
                reason = f"write allowed (probe created {target})"
            else:
                stderr_text = raw_err.decode("utf-8", "replace").strip()
                reason = f"write denied (probe exit code {exit_code})"
                if stderr_text:
                    reason += f": {stderr_text.splitlines()[-1][:200]}"
            return allowed, reason
        except Exception as exc:
            return False, f"probe failed: {type(exc).__name__}: {exc}"
        finally:
            if spawned is not None:
                spawned.close()
            if not existed:
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except OSError:
                    pass
