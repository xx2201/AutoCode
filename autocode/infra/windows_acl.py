"""Windows WRITE_RESTRICTED token and ACL primitives.

The implementation follows Microsoft's restricted-token access model and the
DeepSeek Harness Windows ACL design: a deterministic workspace capability SID,
a private temporary-directory SID, and fail-closed restricted process creation.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import msvcrt
import os
import shutil
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path


if os.name != "nt":
    raise ImportError("Windows ACL sandbox is only available on Windows")


LPVOID = ctypes.c_void_p
PSID = LPVOID
PACL = LPVOID
HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
SE_GROUP_LOGON_ID = 0xC0000000
DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
TOKEN_GROUPS_CLASS = 2
TOKEN_USER_CLASS = 1
TOKEN_DEFAULT_DACL_CLASS = 6
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
GRANT_ACCESS = 1
REVOKE_ACCESS = 4
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
ACCESS_ALLOWED_ACE_TYPE = 0
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_EXECUTE = 0x001200A0
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x0040
READ_CONTROL = 0x00020000
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~READ_CONTROL
FILE_ALL_ACCESS = 0x001F01FF
OWNER_READ_EXECUTE_MASK = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
OWNER_WORKSPACE_MASK = OWNER_READ_EXECUTE_MASK | GRANT_MASK
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x1
CREATE_SUSPENDED = 0x00000004
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class TOKEN_GROUPS_HEADER(ctypes.Structure):
    _fields_ = [("GroupCount", DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", LPVOID),
        ("MultipleTrusteeOperation", DWORD),
        ("TrusteeForm", DWORD),
        ("TrusteeType", DWORD),
        ("ptstrName", LPVOID),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", DWORD),
        ("grfAccessMode", DWORD),
        ("grfInheritance", DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class ACL_HEADER(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class STARTUPINFO_W(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


def _bind(dll, name: str, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


GetCurrentProcess = _bind(kernel32, "GetCurrentProcess", HANDLE)
CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, HANDLE)
LocalFree = _bind(kernel32, "LocalFree", LPVOID, LPVOID)
GetLengthSid = _bind(advapi32, "GetLengthSid", DWORD, PSID)
CopySid = _bind(advapi32, "CopySid", wintypes.BOOL, DWORD, PSID, PSID)
EqualSid = _bind(advapi32, "EqualSid", wintypes.BOOL, PSID, PSID)
OpenProcessToken = _bind(advapi32, "OpenProcessToken", wintypes.BOOL, HANDLE, DWORD, ctypes.POINTER(HANDLE))
GetTokenInformation = _bind(advapi32, "GetTokenInformation", wintypes.BOOL, HANDLE, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD))
SetTokenInformation = _bind(advapi32, "SetTokenInformation", wintypes.BOOL, HANDLE, DWORD, LPVOID, DWORD)
CreateRestrictedToken = _bind(advapi32, "CreateRestrictedToken", wintypes.BOOL, HANDLE, DWORD, DWORD, LPVOID, DWORD, LPVOID, DWORD, LPVOID, ctypes.POINTER(HANDLE))
ConvertStringSidToSidW = _bind(advapi32, "ConvertStringSidToSidW", wintypes.BOOL, wintypes.LPCWSTR, ctypes.POINTER(PSID))
GetNamedSecurityInfoW = _bind(advapi32, "GetNamedSecurityInfoW", DWORD, wintypes.LPWSTR, DWORD, DWORD, ctypes.POINTER(PSID), ctypes.POINTER(PSID), ctypes.POINTER(PACL), ctypes.POINTER(PACL), ctypes.POINTER(LPVOID))
SetNamedSecurityInfoW = _bind(advapi32, "SetNamedSecurityInfoW", DWORD, wintypes.LPWSTR, DWORD, DWORD, PSID, PSID, PACL, PACL)
SetEntriesInAclW = _bind(advapi32, "SetEntriesInAclW", DWORD, DWORD, ctypes.POINTER(EXPLICIT_ACCESS_W), PACL, ctypes.POINTER(PACL))
GetAce = _bind(advapi32, "GetAce", wintypes.BOOL, PACL, DWORD, ctypes.POINTER(LPVOID))
GetStdHandle = _bind(kernel32, "GetStdHandle", HANDLE, DWORD)
SetHandleInformation = _bind(kernel32, "SetHandleInformation", wintypes.BOOL, HANDLE, DWORD, DWORD)
CreateProcessAsUserW = _bind(advapi32, "CreateProcessAsUserW", wintypes.BOOL, HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, wintypes.BOOL, DWORD, LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO_W), ctypes.POINTER(PROCESS_INFORMATION))
CreateJobObjectW = _bind(kernel32, "CreateJobObjectW", HANDLE, LPVOID, wintypes.LPCWSTR)
SetInformationJobObject = _bind(kernel32, "SetInformationJobObject", wintypes.BOOL, HANDLE, DWORD, LPVOID, DWORD)
AssignProcessToJobObject = _bind(kernel32, "AssignProcessToJobObject", wintypes.BOOL, HANDLE, HANDLE)
ResumeThread = _bind(kernel32, "ResumeThread", DWORD, HANDLE)
TerminateProcess = _bind(kernel32, "TerminateProcess", wintypes.BOOL, HANDLE, wintypes.UINT)
WaitForSingleObject = _bind(kernel32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
GetExitCodeProcess = _bind(kernel32, "GetExitCodeProcess", wintypes.BOOL, HANDLE, ctypes.POINTER(DWORD))


def _winerror(api: str, detail: str = "") -> OSError:
    code = ctypes.get_last_error()
    suffix = f" ({detail})" if detail else ""
    return OSError(code, f"{api} failed{suffix}: {ctypes.FormatError(code).strip()}")


def _check_bool(ok, api: str, detail: str = "") -> None:
    if not ok:
        raise _winerror(api, detail)


@contextlib.contextmanager
def _handle(handle: HANDLE):
    try:
        yield handle
    finally:
        if handle:
            CloseHandle(handle)


class OwnedSid:
    def __init__(self, sddl: str):
        pointer = PSID()
        _check_bool(ConvertStringSidToSidW(sddl, ctypes.byref(pointer)), "ConvertStringSidToSidW", sddl)
        self.pointer = pointer

    def close(self) -> None:
        if self.pointer:
            LocalFree(self.pointer)
            self.pointer = PSID()

    def __enter__(self):
        return self.pointer

    def __exit__(self, exc_type, exc, tb):
        self.close()


def workspace_write_sid(workspace_root: Path) -> str:
    canonical = os.path.normcase(os.path.normpath(str(workspace_root.resolve())))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    first = int.from_bytes(digest[0:4], "little") % (2**30 - 1) + 1
    second = int.from_bytes(digest[4:8], "little") % (2**30 - 1) + 1
    return f"S-1-4-{first}-{second}"


def temp_write_sid(temp_dir: Path) -> str:
    canonical = os.path.normcase(os.path.normpath(str(temp_dir.resolve())))
    digest = hashlib.sha256(b"temp\0" + canonical.encode("utf-8")).digest()
    first = int.from_bytes(digest[0:4], "little") % (2**30 - 1) + 1
    second = int.from_bytes(digest[4:8], "little") % (2**30 - 1) + 1
    return f"S-1-4-{first}-{second}-1"


@contextlib.contextmanager
def _path_lock(path: Path):
    lock_root = Path(tempfile.gettempdir()) / "autocode-acl-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()[:16]
    with (lock_root / f"{digest}.lock").open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _entry(sid: PSID, mode: int, permissions: int) -> EXPLICIT_ACCESS_W:
    return EXPLICIT_ACCESS_W(
        permissions,
        mode,
        SUB_CONTAINERS_AND_OBJECTS_INHERIT,
        TRUSTEE_W(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, sid),
    )


def _has_exact_grant(acl: PACL, sid: PSID, permissions: int) -> bool:
    if not acl:
        return False
    header = ctypes.cast(acl, ctypes.POINTER(ACL_HEADER)).contents
    for index in range(header.AceCount):
        ace = LPVOID()
        _check_bool(GetAce(acl, index, ctypes.byref(ace)), "GetAce")
        ace_header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
        if ace_header.AceType != ACCESS_ALLOWED_ACE_TYPE:
            continue
        flags = ace_header.AceFlags
        mask = DWORD.from_address(ace.value + 4).value
        ace_sid = PSID(ace.value + 8)
        if flags == SUB_CONTAINERS_AND_OBJECTS_INHERIT and mask == permissions and EqualSid(ace_sid, sid):
            return True
    return False


def _edit_acl(path: Path, sid: PSID, mode: int, permissions: int) -> None:
    with _path_lock(path):
        dacl = PACL()
        descriptor = LPVOID()
        result = GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if result != 0:
            raise OSError(result, f"GetNamedSecurityInfoW failed for {path}: {ctypes.FormatError(result).strip()}")
        try:
            if mode == GRANT_ACCESS and dacl and _has_exact_grant(dacl, sid, permissions):
                return
            new_acl = PACL()
            entry = _entry(sid, mode, permissions)
            result = SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new_acl))
            if result != 0:
                raise OSError(result, f"SetEntriesInAclW failed for {path}: {ctypes.FormatError(result).strip()}")
            try:
                result = SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new_acl, None)
                if result != 0:
                    raise OSError(result, f"SetNamedSecurityInfoW failed for {path}: {ctypes.FormatError(result).strip()}")
            finally:
                if new_acl:
                    LocalFree(new_acl)
        finally:
            if descriptor:
                LocalFree(descriptor)


def grant_write(path: Path, sid: PSID) -> None:
    _edit_acl(path.resolve(), sid, GRANT_ACCESS, GRANT_MASK)


def revoke_write(path: Path, sid: PSID) -> None:
    _edit_acl(path.resolve(), sid, REVOKE_ACCESS, 0)


def grant_owner_workspace_access(path: Path, sid: PSID, mode: str) -> None:
    mask = OWNER_WORKSPACE_MASK if mode == "workspace-write" else OWNER_READ_EXECUTE_MASK
    _edit_acl(path.resolve(), sid, GRANT_ACCESS, mask)


def _open_current_token() -> HANDLE:
    token = HANDLE()
    access = TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ADJUST_DEFAULT | TOKEN_ASSIGN_PRIMARY
    _check_bool(OpenProcessToken(GetCurrentProcess(), access, ctypes.byref(token)), "OpenProcessToken")
    return token


def _find_logon_sid(token: HANDLE):
    needed = DWORD()
    GetTokenInformation(token, TOKEN_GROUPS_CLASS, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise _winerror("GetTokenInformation", "TokenGroups size")
    buffer = ctypes.create_string_buffer(needed.value)
    _check_bool(GetTokenInformation(token, TOKEN_GROUPS_CLASS, buffer, needed, ctypes.byref(needed)), "GetTokenInformation", "TokenGroups")
    base = ctypes.addressof(buffer) + TOKEN_GROUPS_HEADER.Groups.offset
    count = DWORD.from_address(ctypes.addressof(buffer)).value
    for index in range(count):
        item = SID_AND_ATTRIBUTES.from_address(base + index * ctypes.sizeof(SID_AND_ATTRIBUTES))
        if item.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            length = GetLengthSid(item.Sid)
            if not length:
                raise _winerror("GetLengthSid", "logon SID")
            copy = ctypes.create_string_buffer(length)
            _check_bool(CopySid(length, copy, item.Sid), "CopySid", "logon SID")
            return copy, ctypes.cast(copy, PSID)
    raise RuntimeError("CreateRestrictedToken prerequisite failed: current token has no logon SID")


def _copy_token_user_sid(token: HANDLE):
    needed = DWORD()
    GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise _winerror("GetTokenInformation", "TokenUser size")
    buffer = ctypes.create_string_buffer(needed.value)
    _check_bool(GetTokenInformation(token, TOKEN_USER_CLASS, buffer, needed, ctypes.byref(needed)), "GetTokenInformation", "TokenUser")
    source = SID_AND_ATTRIBUTES.from_buffer(buffer).Sid
    length = GetLengthSid(source)
    if not length:
        raise _winerror("GetLengthSid", "token user SID")
    copy = ctypes.create_string_buffer(length)
    _check_bool(CopySid(length, copy, source), "CopySid", "token user SID")
    return copy, ctypes.cast(copy, PSID)


def _require_owned_workspace(path: Path, user_sid: PSID) -> None:
    owner = PSID()
    descriptor = LPVOID()
    result = GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, f"GetNamedSecurityInfoW failed for {path}: {ctypes.FormatError(result).strip()}")
    try:
        if not owner or not EqualSid(owner, user_sid):
            raise PermissionError(f"Windows sandbox workspace must be owned by the current user: {path}")
    finally:
        if descriptor:
            LocalFree(descriptor)


def _create_restricted_token(current: HANDLE, logon: PSID, world: PSID, write_sids: list[PSID], mode: str) -> HANDLE:
    selected = [logon, world] if mode == "read-only" else [logon, world, *write_sids]
    if mode == "workspace-write" and not write_sids:
        raise RuntimeError("workspace-write requires at least one capability SID")
    array = (SID_AND_ATTRIBUTES * len(selected))(*(SID_AND_ATTRIBUTES(sid, 0) for sid in selected))
    restricted = HANDLE()
    flags = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED
    _check_bool(CreateRestrictedToken(current, flags, 0, None, 0, None, len(selected), array, ctypes.byref(restricted)), "CreateRestrictedToken")
    return restricted


def _set_default_dacl(token: HANDLE, sid: PSID) -> None:
    needed = DWORD()
    GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise _winerror("GetTokenInformation", "TokenDefaultDacl size")
    buffer = ctypes.create_string_buffer(needed.value)
    _check_bool(GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, buffer, needed, ctypes.byref(needed)), "GetTokenInformation", "TokenDefaultDacl")
    current_acl = PACL.from_address(ctypes.addressof(buffer)).value
    if not current_acl:
        raise RuntimeError("restricted token has no default DACL")
    new_acl = PACL()
    entry = _entry(sid, GRANT_ACCESS, FILE_ALL_ACCESS)
    result = SetEntriesInAclW(1, ctypes.byref(entry), current_acl, ctypes.byref(new_acl))
    if result != 0:
        raise OSError(result, f"SetEntriesInAclW failed for token default DACL: {ctypes.FormatError(result).strip()}")
    try:
        default_acl_pointer = PACL(new_acl.value)
        _check_bool(SetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, ctypes.byref(default_acl_pointer), ctypes.sizeof(default_acl_pointer)), "SetTokenInformation", "TokenDefaultDacl")
    finally:
        LocalFree(new_acl)


def _kill_on_close_job() -> HANDLE:
    job = CreateJobObjectW(None, None)
    if not job:
        raise _winerror("CreateJobObjectW")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        _check_bool(SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)), "SetInformationJobObject")
    except Exception:
        CloseHandle(job)
        raise
    return job


def _std_handle(kind: int, label: str) -> HANDLE:
    handle = GetStdHandle(kind)
    if not handle or handle == HANDLE(-1).value:
        raise _winerror("GetStdHandle", label)
    _check_bool(SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT), "SetHandleInformation", label)
    return handle


def _spawn_restricted(token: HANDLE, argv: list[str], cwd: Path) -> tuple[HANDLE, HANDLE, int]:
    stdin = _std_handle(STD_INPUT_HANDLE, "stdin")
    stdout = _std_handle(STD_OUTPUT_HANDLE, "stdout")
    stderr = _std_handle(STD_ERROR_HANDLE, "stderr")
    startup = STARTUPINFO_W()
    startup.cb = ctypes.sizeof(startup)
    startup.dwFlags = STARTF_USESTDHANDLES
    startup.hStdInput = stdin
    startup.hStdOutput = stdout
    startup.hStdError = stderr
    info = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    created = CreateProcessAsUserW(token, None, command_line, None, None, True, CREATE_SUSPENDED, None, str(cwd), ctypes.byref(startup), ctypes.byref(info))
    SetHandleInformation(stdin, HANDLE_FLAG_INHERIT, 0)
    SetHandleInformation(stdout, HANDLE_FLAG_INHERIT, 0)
    SetHandleInformation(stderr, HANDLE_FLAG_INHERIT, 0)
    _check_bool(created, "CreateProcessAsUserW", f"cwd={cwd}; argv0={argv[0]}")
    return info.hProcess, info.hThread, info.dwProcessId


def run_restricted(argv: list[str], cwd: Path, workspace_root: Path, mode: str, temp_root: Path) -> int:
    if mode not in {"read-only", "workspace-write"}:
        raise ValueError(f"restricted runner does not support mode {mode}")
    workspace = workspace_root.resolve()
    temp_parent = temp_root.resolve()
    try:
        temp_parent.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise ValueError(f"Windows ACL temp root must be outside workspace: {temp_parent}")

    private_temp = None
    workspace_sid = temp_sid = world_sid = None
    current = restricted = process = thread = job = None
    temp_granted = False
    exit_code_value = None
    primary_error = None
    cleanup_errors: list[Exception] = []
    try:
        private_temp = Path(tempfile.mkdtemp(prefix="autocode-", dir=temp_parent))
        workspace_sid = OwnedSid(workspace_write_sid(workspace))
        temp_sid = OwnedSid(temp_write_sid(private_temp))
        world_sid = OwnedSid("S-1-1-0")
        current = _open_current_token()
        user_buffer, user_sid = _copy_token_user_sid(current)
        _require_owned_workspace(workspace, user_sid)
        # Some nested Windows hosts expose the workspace through an ambient
        # group that LUA_TOKEN disables. Materialize only the owner's ordinary
        # access (never WRITE_DAC/WRITE_OWNER); WRITE_RESTRICTED still requires
        # the capability SID below as the independent pass for every write.
        grant_owner_workspace_access(workspace, user_sid, mode)
        if mode == "workspace-write":
            grant_write(workspace, workspace_sid.pointer)
            grant_write(private_temp, temp_sid.pointer)
            temp_granted = True
        logon_buffer, logon_sid = _find_logon_sid(current)
        write_sids = [workspace_sid.pointer, temp_sid.pointer] if mode == "workspace-write" else []
        restricted = _create_restricted_token(current, logon_sid, world_sid.pointer, write_sids, mode)
        _set_default_dacl(restricted, temp_sid.pointer if mode == "workspace-write" else world_sid.pointer)
        os.environ["TMP"] = str(private_temp)
        os.environ["TEMP"] = str(private_temp)
        job = _kill_on_close_job()
        process, thread, _ = _spawn_restricted(restricted, argv, cwd)
        if not AssignProcessToJobObject(job, process):
            TerminateProcess(process, 1)
            raise _winerror("AssignProcessToJobObject")
        if ResumeThread(thread) == 0xFFFFFFFF:
            raise _winerror("ResumeThread")
        CloseHandle(thread)
        thread = None
        if WaitForSingleObject(process, INFINITE) != WAIT_OBJECT_0:
            raise _winerror("WaitForSingleObject")
        exit_code = DWORD()
        _check_bool(GetExitCodeProcess(process, ctypes.byref(exit_code)), "GetExitCodeProcess")
        exit_code_value = int(exit_code.value)
    except BaseException as exc:
        primary_error = exc
    finally:
        for handle in (thread, process, job, restricted, current):
            if handle:
                CloseHandle(handle)
        if temp_granted and private_temp is not None and temp_sid is not None:
            try:
                revoke_write(private_temp, temp_sid.pointer)
            except OSError as exc:
                cleanup_errors.append(exc)
        for owned_sid in (workspace_sid, temp_sid, world_sid):
            if owned_sid is not None:
                owned_sid.close()
        if private_temp is not None:
            try:
                shutil.rmtree(private_temp)
            except OSError as exc:
                cleanup_errors.append(exc)

    if primary_error is not None:
        for cleanup_error in cleanup_errors:
            primary_error.add_note(f"Windows sandbox cleanup also failed: {cleanup_error}")
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_errors:
        details = "; ".join(str(error) for error in cleanup_errors)
        raise RuntimeError(f"Windows sandbox cleanup failed: {details}")
    if exit_code_value is None:
        raise RuntimeError("Windows sandbox process completed without an exit code")
    return exit_code_value
