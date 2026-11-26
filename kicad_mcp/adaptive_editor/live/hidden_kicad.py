"""Run processes (KiCad's pcbnew, helper pythons, GUI self-tests) on a private, never-shown Win32 desktop.

Every process is created with CreateProcessW and ``STARTUPINFOW.lpDesktop`` set to a private desktop.
Python's subprocess startup-info class has no ``lpDesktop`` field; setting it there silently does nothing.
Each process also gets ``CREATE_NO_WINDOW`` and the no-crash-dialog error mode, which its children
inherit.

A tripwire watches the user's input desktop for any window owned by the whole process tree (the
root process and all its descendants, by exact PID). If one appears, the tree is killed.

pcbnew runs with a task-owned profile, pre-seeded so KiCad's start wizard has nothing to ask:
- settings: ``kicad_common.json`` exists (settings_manager.cpp:637-650);
- libraries: global fp, sym and design-block tables exist (startwizard_provider_libraries.cpp:230-233);
- privacy: both do_not_show_again prompts are true (startwizard_provider_privacy.cpp:81-87).

KICAD_CONFIG_HOME, KICAD_DOCUMENTS_HOME, KICAD<major>_3RD_PARTY, KICAD_CACHE_HOME, TEMP, TMP and
LOCALAPPDATA are redirected, and any inherited KICAD*_3RD_PARTY is stripped. KiCad paths come from
``kicad_env``.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import ctypes
import ctypes.wintypes as wt
import io
import json
import msvcrt
import os
import shutil
import sys
import threading
import time
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adaptive_editor/ (kicad_env)
import kicad_env  # noqa: E402
from adaptive_engine.job import CREATE_SUSPENDED, Job  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DESKTOP_ALL = 0x01FF | 0x000F0000
SEM_FLAGS = 0x0001 | 0x0002 | 0x8000   # no critical-error, GP-fault (WER) or open-file dialogs
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
STILL_ACTIVE = 259
MAX_PIPE_PATH = 120  # KiCad's IPC pipe is named after <TEMP>/kicad/api.sock; ~150 chars failed, ~65 worked


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


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wt.BOOL)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


user32.CreateDesktopW.restype = wt.HANDLE
user32.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.OpenInputDesktop.restype = wt.HANDLE
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumDesktopWindows.argtypes = [wt.HANDLE, EnumWindowsProc, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
# InternalGetWindowText reads the title without sending WM_GETTEXT, so a tripwire can never hang on a window
# whose thread is busy or not pumping messages (GetWindowTextW would block on it).
user32.InternalGetWindowText.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
kernel32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
                                    wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                    ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreatePipe.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
                                ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD]
kernel32.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
kernel32.GetProcessTimes.restype = wt.BOOL
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.ResumeThread.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


# ---------------------------------------------------------------- windows and process trees

def _windows_on(desktop_handle) -> list[dict]:
    out = []

    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        user32.InternalGetWindowText(hwnd, buf, 256)
        out.append({"hwnd": int(hwnd), "pid": pid.value, "title": buf.value,
                    "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumDesktopWindows(desktop_handle, EnumWindowsProc(cb), 0)
    return out


def process_table() -> list[tuple[int, int]]:
    """(pid, parent pid) for every process."""
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == wt.HANDLE(-1).value:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W(); entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def pid_creation_time(pid: int) -> int | None:
    """Creation time (FILETIME ticks) of a running process, or None if it cannot be opened."""
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel_t, user_t = (wt.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def tree_pids(roots, created=pid_creation_time) -> set[int]:
    """The roots and all their descendants. A child is followed only when it was created at or after its
    parent: a long-lived process whose real parent died keeps that parent's PID, and if Windows reuses the
    PID for one of ours, that stranger must never be killed or blamed for a window. Unknown creation times
    are not followed."""
    children: dict[int, list[int]] = {}
    for pid, parent in process_table():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    seen, stack = set(), [int(r) for r in roots]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        parent_created = created(pid)
        for child in children.get(pid, ()):
            child_created = created(child)
            if parent_created is not None and child_created is not None and child_created >= parent_created:
                stack.append(child)
    return seen


def owner_desktop_scan(roots) -> list[dict] | None:
    """Windows on the user's input desktop owned by any process in the given trees, or None when the
    input desktop cannot be opened (e.g. locked): "could not look" is never reported as "nothing seen"."""
    pids = tree_pids(roots) | owned_job_pids()
    inp = user32.OpenInputDesktop(0, False, 0x0001)
    if not inp:
        return None
    try:
        return [w for w in _windows_on(inp) if w["pid"] in pids]
    finally:
        user32.CloseDesktop(inp)


_LIVE_PROCESSES: "weakref.WeakSet[HiddenProcess]" = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()  # tripwire threads read the set while the main thread adds to it


def owned_job_pids() -> set[int]:
    """Members of every live HiddenProcess job: processes this harness owns even when their parent exited."""
    pids: set[int] = set()
    with _LIVE_LOCK:
        live = list(_LIVE_PROCESSES)
    for proc in live:
        try:
            pids.update(proc.pids())
        except OSError:
            pass
    return pids


def owner_windows_of_tree(roots) -> list[dict]:
    """Windows on the user's input desktop owned by any process in the given trees. Fails closed: when the
    input desktop cannot be observed (e.g. locked) it raises OwnerDesktopUnobservable, never returns []."""
    seen = owner_desktop_scan(roots)
    if seen is None:
        raise OwnerDesktopUnobservable("the user's desktop could not be observed (locked?); the run is stopped")
    return seen


def owner_windows_of(pid: int) -> list[dict]:
    return owner_windows_of_tree([pid])


def kill_tree(root_pid: int) -> list[int]:
    """Terminate the root's descendants, then the root, by exact PID. Returns the PIDs terminated."""
    killed = []
    for pid in sorted(tree_pids([root_pid]) - {root_pid}) + [root_pid]:
        handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)  # TERMINATE | SYNCHRONIZE
        if handle:
            try:
                if kernel32.TerminateProcess(handle, 1):
                    kernel32.WaitForSingleObject(handle, 15000)
                    killed.append(pid)
            finally:
                kernel32.CloseHandle(handle)
    return killed


class VisibleWindowError(RuntimeError):
    pass


class OwnerDesktopUnobservable(VisibleWindowError):
    """The tripwire could not look: treated like a leak (the task is stopped), never as "nothing seen"."""


# ---------------------------------------------------------------- private desktop + spawning

class HiddenDesktop:
    def __init__(self, prefix: str = "hidden"):
        self.name = f"{prefix}-{os.getpid()}-{int(time.time() * 1000)}"
        self.handle = user32.CreateDesktopW(self.name, None, None, 0, DESKTOP_ALL, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateDesktopW failed")

    def windows(self) -> list[dict]:
        return _windows_on(self.handle)

    def close(self) -> None:
        if self.handle:
            user32.CloseDesktop(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _inheritable_pipe():
    """(parent_end_handle, child_end_handle); only the child end is inheritable."""
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    read, write = wt.HANDLE(), wt.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(sa), 0):
        raise OSError(ctypes.get_last_error(), "CreatePipe failed")
    return read, write


class HiddenProcess:
    """A process on a private desktop. ``stdin``/``stdout`` are text streams when requested as 'pipe'."""

    def __init__(self, argv, desktop: HiddenDesktop, *, cwd=None, env=None, stdin=None, stdout=None,
                 stderr=None, encoding="utf-8"):
        self.desktop = desktop
        self.stdin = self.stdout = None
        close_after = []

        def child_end(spec, for_reading_by_child: bool):
            if spec == "pipe":
                read, write = _inheritable_pipe()
                parent, child = (write, read) if for_reading_by_child else (read, write)
                kernel32.SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0)
                close_after.append(child)
                fd = msvcrt.open_osfhandle(parent.value, os.O_WRONLY if for_reading_by_child else os.O_RDONLY)
                stream = io.TextIOWrapper(os.fdopen(fd, "wb" if for_reading_by_child else "rb", buffering=0),
                                          encoding=encoding, newline="\n", write_through=True)
                return child, stream
            path = os.devnull if spec is None else str(spec)
            f = open(path, "rb" if for_reading_by_child else "ab", buffering=0)
            handle = wt.HANDLE(msvcrt.get_osfhandle(f.fileno()))
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
            close_after.append(f)
            return handle, None

        h_in, self.stdin = child_end(stdin, True)
        h_out, self.stdout = child_end(stdout, False)
        h_err, _ = child_end(stderr, False)
        si = STARTUPINFOW(); si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = f"WinSta0\\{desktop.name}"
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = h_in, h_out, h_err
        block = None
        if env is not None:
            block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
        cmd = ctypes.create_unicode_buffer(" ".join(_quote(a) for a in argv) if isinstance(argv, (list, tuple))
                                           else argv)
        self.pi = PROCESS_INFORMATION()
        self.job = Job()  # owns the process and everything it ever starts, even after the root exits
        old = kernel32.SetErrorMode(SEM_FLAGS)  # inherited by the child: no crash/error dialogs
        try:
            ok = kernel32.CreateProcessW(None, cmd, None, None, True,
                                         CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | CREATE_SUSPENDED, block,
                                         None if cwd is None else str(cwd), ctypes.byref(si), ctypes.byref(self.pi))
        finally:
            kernel32.SetErrorMode(old)
            for item in close_after:
                kernel32.CloseHandle(item) if isinstance(item, wt.HANDLE) else item.close()
        if not ok:
            self.job.close()
            raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
        try:
            self.job.assign(self.pi.hProcess)  # before it runs any code: nothing escapes the job
        except BaseException:  # never leave a suspended, unowned process (or its handles) behind
            kernel32.TerminateProcess(self.pi.hProcess, 1)
            kernel32.WaitForSingleObject(self.pi.hProcess, 15000)
            kernel32.CloseHandle(self.pi.hThread); kernel32.CloseHandle(self.pi.hProcess)
            self.pi.hProcess = None
            self.job.close()
            raise
        kernel32.ResumeThread(self.pi.hThread)
        self.pid = self.pi.dwProcessId
        with _LIVE_LOCK:
            _LIVE_PROCESSES.add(self)

    def pids(self) -> list[int]:
        """Every process this one owns now: the root (while running) and all descendants, orphans included."""
        return self.job.pids()

    def poll(self):
        """The exit code once the process has fully exited, else None. GetExitCodeProcess alone reports the
        pending code while the process is still running its exit sequence (DLL teardown); ending the job
        then would overwrite it, so only a signalled process counts as exited."""
        if kernel32.WaitForSingleObject(self.pi.hProcess, 0) != 0:  # not signalled: still running
            return None
        code = wt.DWORD()
        kernel32.GetExitCodeProcess(self.pi.hProcess, ctypes.byref(code))
        return None if code.value == STILL_ACTIVE else code.value

    def wait(self, timeout: float | None = None):
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(self.pi.hProcess, ms) != 0:
            raise TimeoutError(f"process {self.pid} did not exit within {timeout}s")
        return self.poll()

    def kill(self) -> list[int]:
        """End the whole job: the root if still running and every descendant, even after the root exited."""
        return self.job.terminate()

    def close(self) -> None:
        self.job.close()  # anything still running in the job ends with it
        for stream in (self.stdin, self.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        if self.pi.hProcess:
            kernel32.CloseHandle(self.pi.hThread); kernel32.CloseHandle(self.pi.hProcess)
            self.pi.hProcess = None


def _quote(arg) -> str:
    s = str(arg)
    if s and not any(c in s for c in ' \t"'):
        return s
    out, backslashes = ['"'], 0
    for ch in s:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes * 2 + 1) + '"')
        else:
            out.append("\\" * backslashes + ch)
        backslashes = 0
    out.append("\\" * (backslashes * 2) + '"')
    return "".join(out)


@cleanup_after_call
def run_hidden(argv, cwd=None, timeout_s: float = 300.0, env: dict | None = None) -> dict:
    """Run a command to completion on a fresh private desktop with output captured; the tripwire watches
    the whole tree for the entire run and kills it if any of its windows reaches the user's desktop."""
    work = Path(scoped_mkdtemp(prefix="hidden-run-"))
    out_path, err_path = work / "stdout.txt", work / "stderr.txt"
    with HiddenDesktop("run") as desk:
        proc = HiddenProcess(argv, desk, cwd=cwd, env=env, stdout=out_path, stderr=err_path)
        try:
            end, leaked, timed_out, blind = time.time() + timeout_s, [], False, None
            while True:
                try:
                    leaked = owner_windows_of_tree([proc.pid, *proc.pids()])
                except OwnerDesktopUnobservable as exc:  # fail closed: stop the task
                    blind = exc
                    break
                if leaked or proc.poll() is not None:
                    break
                if time.time() >= end:
                    timed_out = True
                    break
                time.sleep(0.05)
            killed = proc.kill()
            code = None if (timed_out or leaked or blind) else proc.poll()
        finally:
            proc.close()
    result = {"exit_code": code, "timed_out": timed_out, "pid": proc.pid, "desktop": desk.name,
              "killed": killed, "stdout": out_path.read_text(encoding="utf-8", errors="replace"),
              "stderr": err_path.read_text(encoding="utf-8", errors="replace")}
    shutil.rmtree(work, ignore_errors=True)
    if blind is not None:
        raise blind
    if leaked:
        raise VisibleWindowError(f"task window reached the owner desktop: {leaked[:3]}")
    return result


# ---------------------------------------------------------------- KiCad (pcbnew) on a hidden desktop

def seed_profile(root: Path) -> dict[str, str]:
    ver_name = kicad_env.settings_version()
    major = ver_name.split(".")[0]
    cfg = root / "config"; docs = root / "documents"; cache = root / "cache"; tmp = root / "temp"
    local = root / "localappdata"
    ver = cfg / ver_name
    kdocs = docs / "KiCad" / ver_name
    third = kdocs / "3rdparty"
    for d in (ver, kdocs / "plugins", kdocs / "scripting" / "plugins", third, cache, tmp, local):
        d.mkdir(parents=True, exist_ok=True)
    common = ver / "kicad_common.json"
    if not common.exists():
        common.write_text(json.dumps({
            "api": {"enable_server": True},
            "do_not_show_again": {"data_collection_prompt": True, "update_check_prompt": True},
            "meta": {"version": 3}}), encoding="utf-8")
    tmpl = kicad_env.kicad_app_root() / "share" / "kicad" / "template"
    for name in ("fp-lib-table", "sym-lib-table"):
        if not (ver / name).exists():
            shutil.copyfile(tmpl / name, ver / name)
    if not (ver / "design-block-lib-table").exists():
        (ver / "design-block-lib-table").write_text("(design_block_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    return {"KICAD_CONFIG_HOME": str(cfg), "KICAD_DOCUMENTS_HOME": str(docs),
            f"KICAD{major}_3RD_PARTY": str(third), "KICAD_CACHE_HOME": str(cache),
            "TEMP": str(tmp), "TMP": str(tmp), "LOCALAPPDATA": str(local)}


@temporary_owner(cleanup='__exit__', allocating=('__init__',))
class HiddenKiCad:
    def __init__(self, board: Path, workdir: Path | None = None, tripwire_s: float = 20.0):
        self.board = Path(board)
        self.root = Path(workdir or scoped_mkdtemp(prefix="hk-"))
        self.tripwire_s = tripwire_s
        self.desktop: HiddenDesktop | None = None
        self.proc: HiddenProcess | None = None
        self.env: dict | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else -1

    @property
    def desktop_name(self) -> str:
        return self.desktop.name if self.desktop else ""

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def __enter__(self):
        pipe = self.root / "temp" / "kicad" / "api.sock"
        if len(str(pipe)) > MAX_PIPE_PATH:
            # KiCad's IPC server is an nng ipc:// named pipe named after <TEMP>/kicad/api.sock; a long
            # profile path makes the server fail silently (no socket, no error). Refuse up front.
            raise ValueError(f"profile path too long for the IPC pipe name ({len(str(pipe))} > {MAX_PIPE_PATH}): {pipe}")
        pcbnew = kicad_env.pcbnew_exe()
        if pcbnew is None:
            raise kicad_env.KiCadNotFound("pcbnew executable not found for this platform")
        try:
            self.desktop = HiddenDesktop("kicad")
            env = {k: v for k, v in os.environ.items()
                   if not (k.upper().startswith("KICAD") and k.upper().endswith("_3RD_PARTY"))}
            env.pop("KICAD_API_SOCKET", None)
            env.update(seed_profile(self.root))
            self.env = env
            self.proc = HiddenProcess([str(pcbnew), str(self.board)], self.desktop, cwd=self.root, env=env)
            self._tripwire(self.tripwire_s)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def check_tree(self) -> None:
        try:
            leaked = owner_windows_of_tree([self.pid, *(self.proc.pids() if self.proc else [])])
        except OwnerDesktopUnobservable:
            self.kill()  # fail closed
            raise
        if leaked:
            self.kill()
            raise VisibleWindowError(f"task window reached the owner desktop: {leaked[:3]}")

    def _tripwire(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.alive():
            self.check_tree()
            time.sleep(0.05)

    def socket_dir(self) -> Path:
        return Path(self.env["TEMP"]) / "kicad"

    def wait_socket(self, timeout=120.0) -> Path:
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                raise RuntimeError("pcbnew exited early")
            self.check_tree()
            # On Windows nng ipc:// is a named pipe, so no file appears; KiCad 10.0.5 uses
            # <TEMP>/kicad/api.sock (api_server.cpp:79-128). Readiness = a successful ping.
            if self.socket_dir().is_dir():
                path = self.socket_dir() / "api.sock"
                try:
                    from kipy import KiCad
                    KiCad(socket_path=self.socket_url(path), timeout_ms=3000).ping()
                    return path
                except Exception:
                    pass
            time.sleep(0.5)
        raise TimeoutError("IPC socket did not appear; hidden-desktop windows: "
                           + str([w["title"] for w in self.hidden_windows() if w["title"]][:10]))

    @staticmethod
    def socket_url(path: Path) -> str:
        return "ipc://" + str(path)

    def hidden_windows(self) -> list[dict]:
        pids = tree_pids([self.pid])
        return [w for w in self.desktop.windows() if w["pid"] in pids] if self.desktop else []

    def kill(self) -> list[int]:
        return self.proc.kill() if self.proc else []

    def __exit__(self, *exc):
        if self.proc:
            self.proc.kill()
            self.proc.close()
        if self.desktop:
            self.desktop.close()
        return False
