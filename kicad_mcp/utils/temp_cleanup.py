"""Owned temporary storage, released at call/session boundaries, and conservative stale cleanup.

The 24 hour scratch and 7 day completed-job lifetimes are owner requirements. Active
jobs and resources held by another process are always preserved; no process is stopped.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time

_scope = ContextVar("kicad_owned_temporaries", default=None)
logger = logging.getLogger(__name__)


def _is_reparse(path: Path) -> bool:
    import stat
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@contextmanager
def temporary_workspace():
    """A private allocation stack; nested and concurrent calls cannot clean each other."""
    with ExitStack() as stack:
        token = _scope.set(stack)
        try:
            yield
        finally:
            _scope.reset(token)


def cleanup_after_call(function):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def call(*args, **kwargs):
            with temporary_workspace():
                return await function(*args, **kwargs)
    else:
        @wraps(function)
        def call(*args, **kwargs):
            with temporary_workspace():
                return function(*args, **kwargs)
    return call


def scoped_mkdtemp(*args, **kwargs):
    """Allocate in the active call or owner scope; fail before leaking an unowned dir."""
    stack = _scope.get()
    if stack is None:
        raise RuntimeError("temporary allocation requires a call or session cleanup scope")
    directory = stack.enter_context(tempfile.TemporaryDirectory(*args, **kwargs))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                      w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        # Hold the directory without FILE_SHARE_DELETE. No marker file changes
        # the empty-directory contract or appears in an export's artifact list.
        lease = kernel.CreateFileW(str(directory), 0x1, 0x3, None, 3, 0x02000000, None)
        if lease == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        stack.callback(kernel.CloseHandle, lease)
    return directory


def temporary_owner(*, cleanup="close", allocating=("__init__",)):
    """Keep scratch until an existing session's close/exit method, including failed starts."""
    def decorate(cls):
        key = "_temporary_storage_" + cls.__name__
        for name in allocating:
            original = getattr(cls, name)
            def wrap_allocate(function):
                @wraps(function)
                def allocate(self, *args, **kwargs):
                    stack = getattr(self, key, None)
                    if stack is None:
                        stack = ExitStack()
                        setattr(self, key, stack)
                    token = _scope.set(stack)
                    try:
                        return function(self, *args, **kwargs)
                    except BaseException:
                        # Stop this owner's resources before releasing files on failed starts.
                        try:
                            closer = getattr(self, cleanup)
                            closer(None, None, None) if cleanup == "__exit__" else closer()
                        except BaseException:
                            logger.exception("Owner cleanup after failed %s.%s", cls.__name__, function.__name__)
                        stack.close()
                        raise
                    finally:
                        _scope.reset(token)
                return allocate
            setattr(cls, name, wrap_allocate(original))
        original_close = getattr(cls, cleanup, None)
        @wraps(original_close) if original_close else (lambda f: f)
        def close(self, *args, **kwargs):
            try:
                if original_close is not None:
                    return original_close(self, *args, **kwargs)
            finally:
                stack = getattr(self, key, None)
                if stack is not None:
                    stack.close()
        setattr(cls, cleanup, close)
        return cls
    return decorate


def directory_in_use(directory: Path) -> bool:
    """Windows Restart Manager checks file holders; exclusive handles also check directories.

    Errors mean 'in use'. Never shut down applications or force open handles closed.
    """
    if os.name != "nt":
        return True  # No portable open-handle authority: skip automatic stale deletion.
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    files = []
    try:
        for root, dirs, names in os.walk(directory, onerror=lambda e: (_ for _ in ()).throw(e)):
            current = Path(root)
            if _is_reparse(current) or any(_is_reparse(current / n) for n in dirs + names):
                return True
            handle = kernel.CreateFileW(str(current), 0x10000, 0, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                return True
            kernel.CloseHandle(handle)
            files.extend(str(current / name) for name in names)
    except OSError:
        return True
    if not files:
        return False
    rm = ctypes.WinDLL("rstrtmgr")
    session = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return True
    try:
        resources = (w.LPCWSTR * len(files))(*files)
        if rm.RmRegisterResources(session, len(files), resources, 0, None, 0, None):
            return True
        needed, count, reason = w.UINT(), w.UINT(), w.DWORD()
        result = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                              ctypes.byref(reason))
        return result != 0 or needed.value != 0
    finally:
        rm.RmEndSession(session)


def _job_finished(folder: Path) -> bool:
    import json
    state = None
    try:
        journal = folder / "state-history.jsonl"
        if journal.is_file():
            with journal.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.endswith("\n") and line.strip():
                        state = json.loads(line)
        else:
            state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        if state.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            return False
        # Preserve any job whose recorded owner/worker process is still live.
        from kicad_mcp.utils import winproc
        for field in ("owner", "process"):
            recorded = state.get(field)
            if recorded and winproc.identity(recorded["pid"]) == recorded:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError):
        return False


def sweep_stale(*, temp_root=None, jobs_root=None, now=None):
    """Remove only aged, unused scratch and finished jobs. Return a complete receipt."""
    now = time.time() if now is None else now
    temp_root = Path(temp_root or tempfile.gettempdir())
    if jobs_root is None:
        from kicad_mcp.adaptive_editor.kicad_env import user_data_dir
        jobs_root = user_data_dir() / "workflow-jobs"
    jobs_root = Path(jobs_root)
    receipt = {"removed": [], "skipped": [], "errors": []}

    def entries(base):
        try:
            yield from base.iterdir()
        except OSError as exc:
            receipt["errors"].append({"path": str(base), "error": str(exc)})
            logger.warning("Stale cleanup cannot enumerate %s: %s", base, exc)

    for base, age, jobs in ((temp_root, 24 * 60 * 60, False), (jobs_root, 7 * 24 * 60 * 60, True)):
        if not base.is_dir():
            continue
        for folder in entries(base):
            if not jobs and not folder.name.startswith(("kicad-ai-tools-", "kicad_cli_run_")):
                continue
            try:
                if _is_reparse(folder) or not folder.is_dir():
                    continue
                # A recently touched descendant is activity even if its parent mtime is old.
                latest = folder.stat().st_mtime
                unsafe = False
                for root, dirs, files in os.walk(folder):
                    if any(_is_reparse(Path(root) / n) for n in dirs + files):
                        unsafe = True
                        break
                    latest = max(latest, Path(root).stat().st_mtime,
                                 *( (Path(root) / n).stat().st_mtime for n in dirs + files))
                if unsafe or now - latest <= age:
                    continue
                if (jobs and not _job_finished(folder)) or directory_in_use(folder):
                    receipt["skipped"].append(str(folder))
                    continue
                shutil.rmtree(folder)
                receipt["removed"].append(str(folder))
            except OSError as exc:
                receipt["errors"].append({"path": str(folder), "error": str(exc)})
                logger.warning("Stale temporary cleanup skipped %s: %s", folder, exc)
    return receipt
