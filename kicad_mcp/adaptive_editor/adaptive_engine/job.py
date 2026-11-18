"""Windows Job Objects: a process and every descendant it ever starts, owned as one unit.

Ownership by Job Object, not by parent-PID walks: a child stays in the job when its parent exits, and a
stranger that happens to carry a reused parent PID is never in it. ``terminate()`` ends every member,
``pids()`` lists them for a window tripwire, and closing the job kills whatever is left
(KILL_ON_JOB_CLOSE). A member that crashes ends instead of showing a Windows Error Reporting dialog
(DIE_ON_UNHANDLED_EXCEPTION).

Standalone (ctypes only): it runs under KiCad's python, the plugin venv and the MCP server alike.
"""
from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes as wt

CREATE_SUSPENDED = 0x00000004
_SILENT_BREAKAWAY_OK = 0x1000
_KILL_ON_JOB_CLOSE = 0x2000
_DIE_ON_UNHANDLED_EXCEPTION = 0x0400


class _Basic(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wt.DWORD),
                ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", wt.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _Extended(ctypes.Structure):
    _fields_ = [("basic", _Basic), ("io", _IO), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong), ("pu", ctypes.c_longlong),
                ("pk", ctypes.c_longlong), ("faults", wt.DWORD), ("total", wt.DWORD), ("active", wt.DWORD),
                ("terminated", wt.DWORD)]


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wt.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                            ctypes.POINTER(wt.DWORD)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.ResumeThread.argtypes = [wt.HANDLE]
    k.ResumeThread.restype = wt.DWORD
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k.WaitForSingleObject.restype = wt.DWORD
    return k


class Job:
    """One owned process tree. Thread-safe; ``close()`` is idempotent."""

    def __init__(self) -> None:
        self._k = _kernel()
        self._lock = threading.Lock()
        self.handle = self._k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _Extended()
        info.basic.flags = _SILENT_BREAKAWAY_OK | _KILL_ON_JOB_CLOSE | _DIE_ON_UNHANDLED_EXCEPTION
        if not self._k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self._k.CloseHandle(self.handle)
            self.handle = None
            raise error

    def assign(self, process_handle) -> None:
        if not self._k.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def pids(self) -> list[int]:
        """Every process currently in the job (the root and all descendants, orphans included)."""
        with self._lock:
            if not self.handle:
                return []
            for size in (64, 1024, 16384):
                class _List(ctypes.Structure):
                    _fields_ = [("assigned", wt.DWORD), ("listed", wt.DWORD), ("ids", ctypes.c_size_t * size)]
                data = _List()
                if self._k.QueryInformationJobObject(self.handle, 3, ctypes.byref(data), ctypes.sizeof(data), None):
                    return [int(data.ids[i]) for i in range(data.listed)]
            return []

    def active(self) -> int:
        info = _Accounting()
        if not self._k.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.active)

    def terminate(self, wait_s: float = 15.0) -> list[int]:
        """End every member and wait until none is left. Returns the PIDs that were members."""
        members = self.pids()
        # Handles opened while they are members (so the PIDs are exactly ours); waited on after the job
        # ends, because a process object is signalled a moment after the job's active count drops.
        waits = [h for h in (self._k.OpenProcess(0x00100000, False, pid) for pid in members) if h]  # SYNCHRONIZE
        try:
            with self._lock:
                if not self.handle:
                    return members
                self._k.TerminateJobObject(self.handle, 1)
                end = time.time() + wait_s
                while self.active() and time.time() < end:
                    time.sleep(0.01)
            for handle in waits:
                self._k.WaitForSingleObject(handle, max(0, int((end - time.time()) * 1000)))
        finally:
            for handle in waits:
                self._k.CloseHandle(handle)
        return members

    def close(self) -> None:
        with self._lock:
            handle, self.handle = self.handle, None
        if handle:
            try:
                self._k.TerminateJobObject(handle, 1)
            finally:
                self._k.CloseHandle(handle)  # KILL_ON_JOB_CLOSE: nothing owned outlives the job


def popen_contained(cmd, **kwargs) -> tuple[subprocess.Popen, Job]:
    """``subprocess.Popen`` whose process is in a new Job before it runs any code (started suspended,
    assigned, then resumed), so every descendant it starts is owned too."""
    job = Job()
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except BaseException:
        job.close()
        raise
    try:
        job.assign(int(proc._handle))
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
        if ntdll.NtResumeProcess(int(proc._handle)) != 0:
            raise OSError("could not resume the contained process")
    except BaseException:
        proc.kill()
        proc.wait()
        job.close()
        raise
    return proc, job
