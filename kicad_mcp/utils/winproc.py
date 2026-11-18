"""Invisible Windows child processes, contained before they execute."""
import ctypes as c
from ctypes import wintypes as w
import subprocess
import threading
import time

k = c.WinDLL('kernel32', use_last_error=True)
nt = c.WinDLL('ntdll')
k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
k.CreateJobObjectW.restype = w.HANDLE
k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
k.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
k.CloseHandle.argtypes = [w.HANDLE]
k.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
k.OpenProcess.restype = w.HANDLE
k.GetProcessTimes.argtypes = [w.HANDLE,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p]
k.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]
k.CreateMutexW.argtypes = [c.c_void_p,w.BOOL,w.LPCWSTR]
k.CreateMutexW.restype = w.HANDLE
k.ReleaseMutex.argtypes = [w.HANDLE]
nt.NtResumeProcess.argtypes = [w.HANDLE]
k.GetConsoleWindow.restype = w.HWND
u = c.WinDLL('user32')
u.IsWindowVisible.argtypes = [w.HWND]

class Basic(c.Structure):
    _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                ('active', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
class IO(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]
class Extended(c.Structure):
    _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]

class Accounting(c.Structure):
    _fields_ = [('user_time', c.c_longlong), ('kernel_time', c.c_longlong),
                ('period_user', c.c_longlong), ('period_kernel', c.c_longlong),
                ('page_faults', w.DWORD), ('total', w.DWORD),
                ('active', w.DWORD), ('terminated', w.DWORD)]

def assert_hidden():
    hwnd = k.GetConsoleWindow()
    if hwnd and u.IsWindowVisible(hwnd):
        raise RuntimeError('Visible console refused. Use the GUI-subsystem entry point behind a verified silent outer launcher.')

def identity(pid):
    handle=k.OpenProcess(0x1000 | 0x100000,False,pid)
    if not handle:
        if c.get_last_error()==87:return None
        raise c.WinError(c.get_last_error())
    try:
        if k.WaitForSingleObject(handle,0)==0:return None
        created,exited,kernel,user=(c.c_ulonglong() for _ in range(4))
        if not k.GetProcessTimes(handle,c.byref(created),c.byref(exited),c.byref(kernel),c.byref(user)):
            raise c.WinError(c.get_last_error())
        return {'pid':pid,'created':created.value}
    finally:k.CloseHandle(handle)

class Mutex:
    def __init__(self,name):
        self.handle=k.CreateMutexW(None,False,name)
        if not self.handle:raise c.WinError(c.get_last_error())
    def __enter__(self):
        result=k.WaitForSingleObject(self.handle,0xffffffff)
        if result not in (0,0x80):raise RuntimeError('Mutex wait failed')
        return self
    def __exit__(self,*args):
        k.ReleaseMutex(self.handle)
        k.CloseHandle(self.handle)

class Tree:
    def __init__(self):
        self._close_lock = threading.Lock()  # close() may run from several threads (stop and reader EOF)
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        # Children inherit containment, including descendants of venv launchers.
        # SILENT_BREAKAWAY_OK let workers escape cancellation and falsely report
        # their tree stopped. Nested Windows Jobs preserve both owners' limits.
        info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            self.close()
            raise c.WinError(c.get_last_error())

    def spawn(self, argv, **kwargs):
        if not argv or str(argv[0]).lower().endswith(('.cmd', '.bat')):
            raise ValueError('Use an executable directly, not a cmd/bat shim.')
        p = subprocess.Popen(argv, creationflags=subprocess.CREATE_NO_WINDOW | 4,
                             shell=False, **kwargs)  # suspended until contained
        try:
            if not k.AssignProcessToJobObject(self.handle, int(p._handle)):
                raise c.WinError(c.get_last_error())
            if nt.NtResumeProcess(int(p._handle)) != 0:
                raise RuntimeError('Could not resume contained process')
        except BaseException:
            p.kill()
            p.wait()
            raise
        return p

    def close(self):
        """Terminate the tree and close the job. Thread-safe and idempotent: a concurrent caller waits for the
        first one and returns once the tree is gone; the handle is never used after it is closed."""
        with self._close_lock:
            self._close_locked()

    def _close_locked(self):
        if self.handle:
            try:
                if not k.TerminateJobObject(self.handle, 1):
                    raise c.WinError(c.get_last_error())
                while True:
                    info = Accounting()
                    if not k.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
                        raise c.WinError(c.get_last_error())
                    if info.active == 0:
                        break
                    time.sleep(.01)
            finally:
                k.CloseHandle(self.handle)
                self.handle = None

    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
