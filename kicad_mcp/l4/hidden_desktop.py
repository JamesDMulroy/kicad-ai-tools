"""Run KiCad on a private, never-shown Win32 desktop with a task-owned profile.

Generalised from ``kicad_mcp/adaptive_editor/live/hidden_kicad.py`` to any KiCad
executable under a KICAD_APP_PATH-aware application root.

Every process in this package that must not touch the user's desktop (KiCad, the UIA helper and
the placement self-check child) is created ONLY through :func:`create_process_on_desktop`, which
calls ``CreateProcessW`` with ``STARTUPINFOW.lpDesktop`` set to a private desktop. Python's
``subprocess`` startup-info class has no desktop field, so setting a desktop there silently does
nothing and shows windows on the user's desktop; this module never uses it.

Safety properties:
- the profile is task-owned and pre-seeded (no start wizard, no settings migration prompt);
- KICAD_CONFIG_HOME, KICAD_DOCUMENTS_HOME, KICAD<major>_3RD_PARTY, KICAD_CACHE_HOME, TEMP, TMP,
  LOCALAPPDATA and APPDATA are redirected, inherited KICAD*_3RD_PARTY is stripped, and the exact
  environment block handed to CreateProcessW is parsed back and verified before launch (B4 launch
  precondition). That proves the values were passed, not that KiCad reads them;
- the broker's HiddenSession runs a windowless placement self-check before its GUI launch and refuses
  the launch if a child created this way does not report the private desktop as its own (HiddenKiCad
  used on its own does not run it);
- a tripwire kills the process tree if any of its windows reaches the user's input desktop, and stops
  it when the input desktop cannot be observed (fail closed);
- processes are stopped by exact handle (PID plus creation time), then the desktop is closed.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

from ..adaptive_editor.adaptive_engine.job import CREATE_SUSPENDED, Job

if os.name != "nt":  # pragma: no cover - the broker is Windows-only by design
    raise ImportError("kicad_mcp.l4.hidden_desktop requires Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DESKTOP_ALL = 0x01FF | 0x000F0000
DESKTOP_READOBJECTS = 0x0001
SEM_FLAGS = 0x0001 | 0x0002 | 0x8000  # FAILCRITICALERRORS | NOGPFAULTERRORBOX | NOOPENFILEERRORBOX
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
UOI_NAME = 2

def third_party_variable(settings_version: str) -> str:
    """KiCad's versioned 3rd-party variable, ``KICAD<major>_3RD_PARTY`` (GetVersionedEnvVarName)."""
    return f"KICAD{settings_version.split('.')[0]}_3RD_PARTY"


def required_redirects(settings_version: str) -> tuple[str, ...]:
    return ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", third_party_variable(settings_version),
            "KICAD_CACHE_HOME", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA")
NONCE_VARIABLE = "KICAD_MCP_L4_NONCE"


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD),
                ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD),
                ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


user32.CreateDesktopW.restype = wt.HANDLE
user32.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                  ctypes.c_void_p]
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumDesktopWindows.argtypes = [wt.HANDLE, EnumWindowsProc, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
# InternalGetWindowText reads the title without sending WM_GETTEXT, so a tripwire can never hang on a window
# whose thread is busy or not pumping messages (GetWindowTextW would block on it).
user32.InternalGetWindowText.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
kernel32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
                                    wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                    ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.SetErrorMode.argtypes = [wt.UINT]
kernel32.SetErrorMode.restype = wt.UINT
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.ResumeThread.restype = wt.DWORD
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                                     ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME)]
kernel32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


class LaunchPreconditionError(RuntimeError):
    """A B4 launch precondition failed; nothing was launched (or the launch was refused)."""


class PlacementError(LaunchPreconditionError):
    """CreateProcessW + lpDesktop did not place the child on the private desktop."""


class VisibleWindowError(RuntimeError):
    """A task window reached the user's input desktop; the process was killed."""


class OwnerDesktopUnobservable(VisibleWindowError):
    """The user's input desktop could not be observed (e.g. locked): the tripwire cannot look, so the task
    is stopped. Never treated as "nothing seen"."""


# ---- application root, windowless python ----------------------------------------------------

def kicad_app_root(explicit: str | os.PathLike | None = None, env=None) -> Path:
    """An explicit root, else KICAD_APP_PATH from ``env``, else kicad_env resolution (per-machine config,
    then platform discovery). Never a guessed default."""
    if explicit:
        return Path(explicit)
    env = os.environ if env is None else env
    if env.get("KICAD_APP_PATH"):
        return Path(env["KICAD_APP_PATH"])
    from kicad_mcp.adaptive_editor import kicad_env
    return kicad_env.kicad_app_root()


def settings_version_of(build: str) -> str:
    """``10.0.5`` -> ``10.0`` (the settings/plugins directory name KiCad uses)."""
    return ".".join(str(build).split(".")[:2])


def windowless_python() -> Path:
    """``pythonw.exe`` of the base interpreter, so the PID is the real process, not a venv launcher."""
    candidates = []
    base = getattr(sys, "_base_executable", None)
    if base:
        candidates.append(Path(base).with_name("pythonw.exe"))
    candidates.append(Path(sys.executable).with_name("pythonw.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LaunchPreconditionError("pythonw.exe (windowless Python) was not found next to the interpreter")


def site_packages_paths() -> list[str]:
    """site-packages directories of this interpreter, for the isolated helper process."""
    return [p for p in sys.path if p.lower().endswith("site-packages") and os.path.isdir(p)]


# ---- profile seeding and the environment-block precondition ---------------------------------

def seed_profile(root: Path, app_root: Path, settings_version: str) -> dict[str, str]:
    """Create the task-owned profile tree for ``settings_version`` (``major.minor``) and return the
    redirected variables."""
    cfg, docs, cache = root / "config", root / "documents", root / "cache"
    tmp, local, roaming = root / "temp", root / "localappdata", root / "appdata"
    ver = cfg / settings_version
    kdocs = docs / "KiCad" / settings_version
    third = kdocs / "3rdparty"
    for directory in (ver, kdocs / "plugins", kdocs / "scripting" / "plugins", third, cache, tmp, local,
                      roaming):
        directory.mkdir(parents=True, exist_ok=True)
    common = ver / "kicad_common.json"
    if not common.exists():
        # As in the proven reference profile: no first-run prompts. The API server flag is kept
        # as in that reference although L4 itself never talks to the IPC socket.
        common.write_text(json.dumps({
            "api": {"enable_server": True},
            "do_not_show_again": {"data_collection_prompt": True, "update_check_prompt": True},
            "meta": {"version": 3}}), encoding="utf-8")
    template = app_root / "share" / "kicad" / "template"
    for name in ("fp-lib-table", "sym-lib-table"):
        if not (ver / name).exists():
            source = template / name
            if source.is_file():
                shutil.copyfile(source, ver / name)
            else:  # a lite or nightly install ships no library tables: start with an empty one, as KiCad would
                kind = name.replace("-", "_")
                (ver / name).write_text(f"({kind}\n  (version 7)\n)\n", encoding="utf-8")
    if not (ver / "design-block-lib-table").exists():
        (ver / "design-block-lib-table").write_text("(design_block_lib_table\n  (version 7)\n)\n",
                                                    encoding="utf-8")
    return {"KICAD_CONFIG_HOME": str(cfg), "KICAD_DOCUMENTS_HOME": str(docs),
            third_party_variable(settings_version): str(third), "KICAD_CACHE_HOME": str(cache),
            "TEMP": str(tmp), "TMP": str(tmp), "LOCALAPPDATA": str(local), "APPDATA": str(roaming)}


def build_child_env(root: Path, app_root: Path, base_env=None,
                    settings_version: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(environment, redirected)`` for a KiCad child.

    Environment names are upper-cased (Windows names are case-insensitive), inherited
    KICAD*_3RD_PARTY, KICAD_API_SOCKET, PYTHONHOME and PYTHONPATH are dropped, then the redirected
    profile values and an instance nonce are added.
    """
    base = os.environ if base_env is None else base_env
    env: dict[str, str] = {}
    for key, value in base.items():
        upper = key.upper()
        if upper.startswith("KICAD") and upper.endswith("_3RD_PARTY"):
            continue
        if upper in ("KICAD_API_SOCKET", "PYTHONHOME", "PYTHONPATH"):
            continue
        env[upper] = value
    if settings_version is None:
        from kicad_mcp.adaptive_editor import kicad_env
        settings_version = kicad_env.settings_version()
    redirected = seed_profile(root, app_root, settings_version)
    env.update(redirected)
    env[NONCE_VARIABLE] = secrets.token_hex(8)
    return env, redirected


def build_env_block(env: dict[str, str]) -> str:
    """The Windows environment block text: sorted ``K=V`` entries, each NUL-terminated, plus a final NUL."""
    entries = [f"{key}={value}" for key, value in sorted(env.items(), key=lambda kv: kv[0].upper())]
    return "\0".join(entries) + "\0\0"


def parse_env_block(block: str) -> dict[str, str]:
    """Inverse of :func:`build_env_block` (upper-cased names, drive entries starting with ``=`` kept)."""
    parsed: dict[str, str] = {}
    for entry in block.split("\0"):
        if not entry:
            continue
        offset = 1 if entry.startswith("=") else 0
        key, sep, value = entry[offset:].partition("=")
        if sep:
            parsed[(entry[:offset] + key).upper()] = value
    return parsed


def verify_env_block(block: str, redirected: dict[str, str], root: Path) -> dict[str, str]:
    """B4 launch precondition: the block to be passed carries every redirection, nothing inherited.

    Raises :class:`LaunchPreconditionError` on any missing or differing value, on a redirected
    path outside the task-owned root or not an existing directory, on any KICAD*_3RD_PARTY other
    than the redirected one and on a KICAD_API_SOCKET. Returns the parsed block.
    """
    parsed = parse_env_block(block)
    problems = []
    root_resolved = Path(root).resolve()
    third = next((n for n in redirected if n.startswith("KICAD") and n.endswith("_3RD_PARTY")), None)
    required = [n for n in ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", third, "KICAD_CACHE_HOME", "TEMP",
                            "TMP", "LOCALAPPDATA", "APPDATA") if n] if third else ["KICAD<major>_3RD_PARTY"]
    for name in required:
        want = redirected.get(name)
        got = parsed.get(name)
        if not want:
            problems.append(f"{name}: no redirected value was prepared")
        elif got != want:
            problems.append(f"{name}: block carries {got!r}, expected {want!r}")
        else:
            path = Path(got)
            try:
                inside = path.resolve() == root_resolved or root_resolved in path.resolve().parents
            except OSError:
                inside = False
            if not inside:
                problems.append(f"{name}: {got!r} is outside the task-owned profile root")
            elif not path.is_dir():
                problems.append(f"{name}: {got!r} is not an existing directory")
    for name in parsed:
        if name.startswith("KICAD") and name.endswith("_3RD_PARTY") and name != third:
            problems.append(f"{name}: inherited 3rd-party variable present")
    if "KICAD_API_SOCKET" in parsed:
        problems.append("KICAD_API_SOCKET: inherited socket variable present")
    if not parsed.get(NONCE_VARIABLE):
        problems.append(f"{NONCE_VARIABLE}: instance nonce missing")
    if problems:
        raise LaunchPreconditionError("launch precondition failed: " + "; ".join(problems))
    return parsed


# ---- win32 helpers ----------------------------------------------------------------------------

def windows_on(desktop_handle) -> list[dict]:
    """Top-level windows of a desktop handle: hwnd, pid, title, class, visible."""
    out: list[dict] = []

    def callback(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user32.InternalGetWindowText(hwnd, title, 256)
        klass = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, klass, 256)
        out.append({"hwnd": int(hwnd), "pid": pid.value, "title": title.value,
                    "class": klass.value, "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumDesktopWindows(desktop_handle, EnumWindowsProc(callback), 0)
    return out


def input_desktop_windows() -> list[dict] | None:
    """Windows on the owner's current input desktop, or None when it cannot be opened (locked)."""
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return None
    try:
        return windows_on(handle)
    finally:
        user32.CloseDesktop(handle)


def tree_pids(roots) -> set[int]:
    """The roots and all their descendants: parent links followed only to processes created at or after
    their parent, so a stranger holding a reused parent PID is never included."""
    from .monitor import descendant_pids
    return descendant_pids([int(r) for r in roots], process_table(), pid_creation_time)


def owner_windows_of_tree(roots) -> list[dict] | None:
    """Windows on the user's input desktop owned by any process in the given trees; None when the input
    desktop cannot be opened (e.g. locked), so a caller can tell "nothing seen" from "could not look"."""
    windows = input_desktop_windows()
    if windows is None:
        return None
    pids = tree_pids(roots)
    return [w for w in windows if w["pid"] in pids]


def owner_windows_of(pid: int) -> list[dict]:
    """Owner-desktop windows of ``pid`` and every descendant (a crash reporter or helper it starts).
    Fails closed: raises OwnerDesktopUnobservable when the input desktop cannot be observed."""
    seen = owner_windows_of_tree([pid])
    if seen is None:
        raise OwnerDesktopUnobservable("the user's desktop could not be observed (locked?); the task is stopped")
    return seen


def process_table() -> list[tuple[int, int]]:
    """``(pid, parent pid)`` for every process, from a Toolhelp snapshot."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:  # NULL or INVALID_HANDLE_VALUE
        return []
    rows: list[tuple[int, int]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _filetime_int(value: wt.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def pid_creation_time(pid: int) -> int | None:
    """Creation time of a running process by PID, or None if it cannot be opened."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return process_creation_time(handle)
    finally:
        kernel32.CloseHandle(handle)


def process_creation_time(handle) -> int | None:
    created, exited, kernel_t, user_t = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel_t), ctypes.byref(user_t)):
        return None
    return _filetime_int(created)


def create_process_on_desktop(command_line: str, desktop_name: str, *, env_block: str | None = None,
                              cwd: str | None = None, creation_flags: int = 0,
                              suppress_error_ui: bool = False) -> PROCESS_INFORMATION:
    """The ONLY process-creation path: CreateProcessW with STARTUPINFOW.lpDesktop on a private desktop."""
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    startup.lpDesktop = f"WinSta0\\{desktop_name}"
    info = PROCESS_INFORMATION()
    command = ctypes.create_unicode_buffer(command_line)
    block = ctypes.create_unicode_buffer(env_block) if env_block is not None else None
    # CREATE_NO_WINDOW: no console for console programs (a GUI program is unaffected and stays on the desktop).
    # CREATE_SUSPENDED: OwnedProcess puts the process in its Job Object before it runs any code, then resumes it.
    flags = (creation_flags | CREATE_NO_WINDOW | CREATE_SUSPENDED
             | (CREATE_UNICODE_ENVIRONMENT if env_block is not None else 0))
    previous = kernel32.SetErrorMode(SEM_FLAGS) if suppress_error_ui else None
    try:
        ok = kernel32.CreateProcessW(None, command, None, None, False, flags, block, cwd,
                                     ctypes.byref(startup), ctypes.byref(info))
    finally:
        if previous is not None:
            kernel32.SetErrorMode(previous)
    if not ok:
        raise OSError(ctypes.get_last_error(), f"CreateProcessW failed for {command_line[:120]!r}")
    return info


class PrivateDesktop:
    """A private desktop on the current window station, closed on exit."""

    def __init__(self, name: str | None = None):
        self.name = name or f"l4-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        self.handle = None

    def open(self) -> "PrivateDesktop":
        self.handle = user32.CreateDesktopW(self.name, None, None, 0, DESKTOP_ALL, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateDesktopW failed")
        return self

    def windows(self) -> list[dict]:
        return windows_on(self.handle) if self.handle else []

    def close(self) -> None:
        if self.handle:
            user32.CloseDesktop(self.handle)
            self.handle = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False


class OwnedProcess:
    """A process this task created: held by handle, so PID and creation time are exact, and owned with every
    descendant it ever starts through its own Job Object (a child whose parent exited is still owned)."""

    def __init__(self, info: PROCESS_INFORMATION, label: str):
        self.label = label
        self.pid = int(info.dwProcessId)
        self._process = info.hProcess
        self._thread = info.hThread
        self.job = None
        try:
            self.job = Job()
            self.job.assign(self._process)  # created suspended: nothing has run yet, so nothing escapes
        except BaseException:  # never leave a suspended, unowned process (or its handles) behind
            kernel32.TerminateProcess(self._process, 1)
            kernel32.WaitForSingleObject(self._process, 15000)
            kernel32.CloseHandle(self._thread)
            kernel32.CloseHandle(self._process)
            self._process = self._thread = None
            if self.job is not None:
                self.job.close()
            raise
        kernel32.ResumeThread(self._thread)
        self.creation_time = process_creation_time(self._process)
        session = wt.DWORD()
        self.session_id = int(session.value) if kernel32.ProcessIdToSessionId(self.pid, ctypes.byref(session)) else None

    def alive(self) -> bool:
        if not self._process:
            return False
        code = wt.DWORD()
        return bool(kernel32.GetExitCodeProcess(self._process, ctypes.byref(code))) and code.value == STILL_ACTIVE

    def wait(self, timeout_ms: int) -> bool:
        """True when the process has exited."""
        return bool(self._process) and kernel32.WaitForSingleObject(self._process, timeout_ms) == 0

    def exit_code(self) -> int | None:
        code = wt.DWORD()
        if self._process and kernel32.GetExitCodeProcess(self._process, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return int(code.value)
        return None

    def kill(self, wait_ms: int = 15000) -> None:
        if self._process and self.alive():
            kernel32.TerminateProcess(self._process, 1)
            kernel32.WaitForSingleObject(self._process, wait_ms)

    def pids(self) -> list[int]:
        """Every process this one owns now (root while running, all descendants, orphans included)."""
        return self.job.pids()

    def kill_descendants(self) -> list[int]:
        """Terminate every process this one started: the members of its Job Object (orphans included) plus,
        for safety, creation-time-checked tree descendants; each is checked to be younger than this process."""
        from .monitor import descendant_pids

        killed: list[int] = []
        # creation-time checked at every link: a stranger with a reused parent PID is never followed
        members = set(self.job.pids())
        for pid in sorted((members | descendant_pids([self.pid], process_table(), pid_creation_time)) - {self.pid}):
            handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                          False, pid)
            if not handle:
                continue
            try:
                created = process_creation_time(handle)
                if created is not None and self.creation_time is not None and created >= self.creation_time:
                    if kernel32.TerminateProcess(handle, 1):
                        kernel32.WaitForSingleObject(handle, 15000)  # returned only once it has really ended
                        killed.append(pid)
            finally:
                kernel32.CloseHandle(handle)
        return killed

    def close(self) -> None:
        self.job.close()  # KILL_ON_JOB_CLOSE: nothing this process started outlives it
        if self._thread:
            kernel32.CloseHandle(self._thread)
            self._thread = None
        if self._process:
            kernel32.CloseHandle(self._process)
            self._process = None

    def identity(self) -> dict:
        return {"pid": self.pid, "creation_time_filetime": self.creation_time, "session_id": self.session_id}


def spawn_windowless_on_desktop(arguments: str, desktop_name: str, cwd: str, label: str) -> OwnedProcess:
    """Start ``pythonw.exe <arguments>`` (no console, no window) on a private desktop."""
    command = f'"{windowless_python()}" {arguments}'
    return OwnedProcess(create_process_on_desktop(command, desktop_name, cwd=cwd, suppress_error_ui=True), label)


# ---- placement self-check ---------------------------------------------------------------------

_PLACEMENT_CHILD = r'''
import ctypes, sys
u = ctypes.WinDLL("user32"); k = ctypes.WinDLL("kernel32")
u.GetThreadDesktop.restype = ctypes.c_void_p
u.GetThreadDesktop.argtypes = [ctypes.c_ulong]
u.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.POINTER(ctypes.c_ulong)]
h = u.GetThreadDesktop(k.GetCurrentThreadId())
buf = ctypes.create_unicode_buffer(256)
need = ctypes.c_ulong()
u.GetUserObjectInformationW(h, 2, buf, 512, ctypes.byref(need))
open(sys.argv[1], "w", encoding="utf-8").write(buf.value)
'''


@cleanup_after_call
def placement_check(workdir: Path | None = None, timeout_s: float = 30.0) -> dict:
    """Windowless proof that CreateProcessW + lpDesktop places a child on the private desktop.

    A windowless ``pythonw.exe`` child writes its own thread-desktop name to a file. No window is
    ever created, so nothing can appear even if placement failed. Returns a result dict;
    :func:`require_placement` raises on failure.
    """
    work = Path(workdir) if workdir else Path(scoped_mkdtemp(prefix="l4-placement-"))
    work.mkdir(parents=True, exist_ok=True)
    script = work / "placement_child.py"
    out = work / "desktop-name.txt"
    script.write_text(_PLACEMENT_CHILD, encoding="utf-8")
    if out.exists():
        out.unlink()
    result: dict = {"created": False, "expected": None, "child_desktop": None, "placed_correctly": False,
                    "timed_out": False, "child_exit_code": None}
    with PrivateDesktop(f"l4-verify-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(2)}") as desk:
        result["expected"] = desk.name
        child = spawn_windowless_on_desktop(f'-I "{script}" "{out}"', desk.name, str(work), "placement-child")
        try:
            result["created"] = True
            if not child.wait(int(timeout_s * 1000)):
                result["timed_out"] = True
                child.kill()
            result["child_exit_code"] = child.exit_code()
        finally:
            child.close()
    if out.exists():
        result["child_desktop"] = out.read_text(encoding="utf-8")
    result["placed_correctly"] = result["child_desktop"] == result["expected"]
    return result


def require_placement(result: dict) -> dict:
    if not result.get("placed_correctly"):
        raise PlacementError(f"desktop placement self-check failed, refusing any GUI launch: {result}")
    return result


# ---- the launcher -----------------------------------------------------------------------------

class HiddenKiCad:
    """One KiCad executable on its own private desktop with a task-owned profile.

    The instance is bound by exact process handle (PID plus creation time), session id, private
    desktop name and an instance nonce in its environment; it is never found by name or title.
    """

    def __init__(self, board: Path | str, workdir: Path | str, *, exe: str = "pcbnew.exe",
                 app_root: Path | str | None = None, base_env=None, tripwire_s: float = 20.0,
                 settings_version: str | None = None):
        self.board = Path(board)
        self.settings_version = settings_version
        self.root = Path(workdir)
        self.exe = exe
        self.app_root = kicad_app_root(app_root)
        self.base_env = base_env
        self.tripwire_s = tripwire_s
        self.desktop = PrivateDesktop()
        self.proc: OwnedProcess | None = None
        self.env: dict[str, str] | None = None
        self.env_block_verified: dict[str, str] | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else -1

    @property
    def desktop_name(self) -> str:
        return self.desktop.name

    def alive(self) -> bool:
        return bool(self.proc and self.proc.alive())

    def identity(self) -> dict:
        info = self.proc.identity() if self.proc else {}
        return {**info, "desktop": self.desktop.name, "nonce": (self.env or {}).get(NONCE_VARIABLE),
                "exe": str(self.app_root / "bin" / self.exe)}

    def start(self, on_created=None) -> "HiddenKiCad":
        executable = self.app_root / "bin" / self.exe
        if not executable.is_file():
            raise LaunchPreconditionError(f"KiCad executable not found: {executable}")
        if not self.board.is_file():
            raise LaunchPreconditionError(f"board file not found: {self.board}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.env, redirected = build_child_env(self.root, self.app_root, self.base_env, self.settings_version)
        block = build_env_block(self.env)
        self.env_block_verified = verify_env_block(block, redirected, self.root)  # raises on failure
        self.desktop.open()
        try:
            info = create_process_on_desktop(f'"{executable}" "{self.board}"', self.desktop.name,
                                             env_block=block, cwd=str(self.root), suppress_error_ui=True)
        except BaseException:
            self.desktop.close()
            raise
        self.proc = OwnedProcess(info, self.exe)
        if on_created:
            on_created(self.proc.pid)
        self._tripwire(self.tripwire_s)
        self._start_watch()
        return self

    def _start_watch(self) -> None:
        """Keep the tripwire on for the session's whole life, not only its start: a window any member of the
        tree shows on the user's desktop later (a dialog, another program KiCad starts) ends the session
        within a quarter second, and is logged."""
        import threading

        self.leaked: list = []
        self._watch_stop = threading.Event()

        def watch() -> None:
            while not self._watch_stop.wait(0.25):
                if not self.alive():
                    return
                try:
                    found = owner_windows_of_tree([self.pid, *(self.proc.pids() if self.proc else [])])
                except Exception:  # noqa: BLE001 - observation failure: fail closed
                    found = None
                if found is None or found:
                    self.leaked = found if found else ["owner desktop unobservable"]
                    self.kill()
                    try:
                        log = Path(os.environ.get("TEMP", ".")) / "kicad-ai-tools" / "leaks.log"
                        log.parent.mkdir(parents=True, exist_ok=True)
                        with log.open("a", encoding="utf-8") as fh:
                            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {self.exe} pid {self.pid} "
                                     f"{self.board.name}: {self.leaked[:3]}\n")
                    except OSError:
                        pass
                    return

        threading.Thread(target=watch, name="kicad-ai-tools-tripwire", daemon=True).start()

    def _tripwire(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.alive():
            try:  # the root and every member of its Job Object (descendants, even orphaned ones)
                leaked = owner_windows_of_tree([self.pid, *(self.proc.pids() if self.proc else [])])
                if leaked is None:
                    raise OwnerDesktopUnobservable("the user's desktop could not be observed (locked?); "
                                                   "the task is stopped")
            except OwnerDesktopUnobservable:
                self.kill()  # fail closed
                raise
            if leaked:
                self.kill()
                raise VisibleWindowError(f"task window reached the owner desktop: {leaked[:3]}")
            time.sleep(0.05)

    def hidden_windows(self) -> list[dict]:
        return [w for w in self.desktop.windows() if w["pid"] == self.pid]

    def kill(self) -> None:
        if self.proc:
            self.proc.kill_descendants()
            self.proc.kill()

    def close(self) -> None:
        """Stop KiCad and everything it started, then close its job and desktop. The job and desktop are closed
        even if stopping is interrupted: closing the job ends every member (KILL_ON_JOB_CLOSE)."""
        if getattr(self, "_watch_stop", None):
            self._watch_stop.set()
        try:
            self.kill()
        finally:
            try:
                if self.proc:
                    self.proc.close()
            finally:
                self.desktop.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False
