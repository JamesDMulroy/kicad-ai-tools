"""
Secure subprocess execution utilities for KiCad MCP.

Provides safe subprocess execution with input validation,
timeout enforcement, and security controls.
"""

import asyncio
import atexit
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
import locale
import logging
import os
from pathlib import Path
import subprocess  # nosec B404 - subprocess usage is secured with validation
import threading
import time

_owned_jobs = set()
_owned_jobs_lock = threading.RLock()


def _event(folder, name, **details):
    if folder is None:
        return
    item = {"event": name, "time": datetime.now(timezone.utc).isoformat(), **details}
    with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _windows_job():
    """Create a kill-on-close job before any child is started."""
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    ntdll = c.WinDLL("ntdll")
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.TerminateJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    ntdll.NtResumeProcess.argtypes = [w.HANDLE]
    ntdll.NtResumeProcess.restype = c.c_long

    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                    ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                    ("active", w.DWORD), ("affinity", c.c_size_t),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in
                    ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                    ("job_memory", c.c_size_t), ("peak_process", c.c_size_t),
                    ("peak_job", c.c_size_t)]

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise c.WinError(c.get_last_error())
    info = Extended()
    # KILL_ON_JOB_CLOSE, and DIE_ON_UNHANDLED_EXCEPTION: a crashing child ends instead of showing a
    # Windows Error Reporting dialog.
    info.basic.flags = 0x2000 | 0x0400
    if not kernel.SetInformationJobObject(handle, 9, c.byref(info), c.sizeof(info)):
        error = c.WinError(c.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    with _owned_jobs_lock:
        _owned_jobs.add(handle)
    _quiet_error_mode(kernel)
    return kernel, ntdll, handle


SEM_FLAGS = 0x0001 | 0x0002 | 0x8000  # FAILCRITICALERRORS | NOGPFAULTERRORBOX | NOOPENFILEERRORBOX


def _quiet_error_mode(kernel) -> None:
    """Children inherit the error mode of this process (CREATE_DEFAULT_ERROR_MODE is never passed), so this
    headless server turns off critical-error, GP-fault and open-file dialogs for itself once; every contained
    child then starts with them off. Together with CREATE_NO_WINDOW and DIE_ON_UNHANDLED_EXCEPTION this is
    why these launches need no private desktop: they are console tools (kicad-cli, KiCad's python running
    pcbnew without a GUI, the engine) that create no windows, and the system crash, critical-error and
    open-file dialogs are suppressed for them (a program that deliberately opens its own window is not
    something these tools do). Anything that opens a window (the PCB Editor) is started only through the private-desktop
    paths (adaptive_editor.live.hidden_kicad, l4.hidden_desktop), which add a window tripwire."""
    try:
        kernel.SetErrorMode(kernel.GetErrorMode() | SEM_FLAGS)
    except (AttributeError, OSError):
        pass


def _close_job(kernel, handle):
    """Terminate only descendants assigned to our exact owned job."""
    with _owned_jobs_lock:
        if not handle or handle not in _owned_jobs:
            return
        _owned_jobs.remove(handle)
    try:
        if not kernel.TerminateJobObject(handle, 1):
            import ctypes as c
            raise c.WinError(c.get_last_error())
        _wait_job_empty(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def _wait_job_empty(kernel, handle):
    """Termination is asynchronous; return only after owned descendants exit."""
    import ctypes as c
    from ctypes import wintypes as w

    class Accounting(c.Structure):
        _fields_ = [("user_time", c.c_longlong), ("kernel_time", c.c_longlong),
                    ("period_user", c.c_longlong), ("period_kernel", c.c_longlong),
                    ("page_faults", w.DWORD), ("total", w.DWORD),
                    ("active", w.DWORD), ("terminated", w.DWORD)]

    kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p,
                                                   w.DWORD, c.c_void_p]
    kernel.QueryInformationJobObject.restype = w.BOOL
    while True:
        accounting = Accounting()
        if not kernel.QueryInformationJobObject(
                handle, 1, c.byref(accounting), c.sizeof(accounting), None):
            raise c.WinError(c.get_last_error())
        if accounting.active == 0:
            return
        time.sleep(0.01)


def close_owned_processes():
    """Server shutdown path; job handles represent only children we launched."""
    if os.name != "nt":
        return
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    with _owned_jobs_lock:
        handles = tuple(_owned_jobs)
        _owned_jobs.clear()
    failures = []
    for handle in handles:
        try:
            if not kernel.TerminateJobObject(handle, 1):
                raise c.WinError(c.get_last_error())
            _wait_job_empty(kernel, handle)
        except BaseException as exc:
            failures.append(exc)
        finally:
            kernel.CloseHandle(handle)
    if failures:
        raise RuntimeError("Could not verify all owned jobs stopped") from failures[0]


atexit.register(close_owned_processes)


def run_contained(argv, *, cwd=None, timeout=None, text=True, encoding=None,
                  errors=None, capture_output=True, check=False,
                  evidence_dir=None, evidence_context=None):
    """Run a hidden process with owned descendant cleanup and complete output."""
    if not argv or str(argv[0]).lower().endswith((".cmd", ".bat")):
        raise ValueError("An executable argv is required; shell shims are unsupported")
    if not capture_output:
        raise ValueError("Contained execution requires captured stdout and stderr")
    evidence = Path(evidence_dir).resolve() if evidence_dir is not None else None
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / "command.json").write_text(json.dumps({
            "argv": list(map(str, argv)), "cwd": str(cwd) if cwd else None,
            "context": evidence_context or {},
            "started": datetime.now(timezone.utc).isoformat()},
            indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        _event(evidence, "launching")
    if os.name == "nt":
        try:
            kernel, ntdll, job = _windows_job()
        except BaseException as exc:
            _event(evidence, "containment_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        process = None
        try:
            with ExitStack() as sinks:
                out = sinks.enter_context((evidence / "stdout.bin").open("wb", buffering=0)) if evidence else subprocess.PIPE
                err = sinks.enter_context((evidence / "stderr.bin").open("wb", buffering=0)) if evidence else subprocess.PIPE
                process = subprocess.Popen(
                    argv, cwd=cwd, stdout=out, stderr=err,
                    text=False if evidence else text,
                    encoding=None if evidence else encoding,
                    errors=None if evidence else errors, shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004,
                )
                _event(evidence, "spawned_suspended", pid=process.pid)
                try:
                    if not kernel.AssignProcessToJobObject(job, int(process._handle)):
                        import ctypes as c
                        raise c.WinError(c.get_last_error())
                    if ntdll.NtResumeProcess(int(process._handle)) != 0:
                        raise RuntimeError("Could not resume contained process")
                    _event(evidence, "running", pid=process.pid)
                except BaseException:
                    process.kill()  # This exact suspended process was launched above.
                    process.wait()
                    _event(evidence, "launch_failed", pid=process.pid,
                           exit_code=process.returncode)
                    raise
                if evidence:
                    return _wait_file_output(process, argv, lambda: _close_job(kernel, job),
                                             timeout=timeout, check=check, evidence=evidence,
                                             text=text, encoding=encoding, errors=errors)
                return _wait_and_collect(process, argv, lambda: _close_job(kernel, job),
                                         timeout=timeout, check=check, evidence=evidence)
        except BaseException as exc:
            _event(evidence, "command_error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                _close_job(kernel, job)
            finally:
                if process is not None and process.poll() is None:
                    process.kill()  # Exact root Popen identity; descendant state remains separately recorded.
                    process.wait()
    # POSIX portability: a new session isolates the exact launched process group.
    with ExitStack() as sinks:
        out = sinks.enter_context((evidence / "stdout.bin").open("wb", buffering=0)) if evidence else subprocess.PIPE
        err = sinks.enter_context((evidence / "stderr.bin").open("wb", buffering=0)) if evidence else subprocess.PIPE
        process = subprocess.Popen(
            argv, cwd=cwd, stdout=out, stderr=err,
            text=False if evidence else text,
            encoding=None if evidence else encoding,
            errors=None if evidence else errors, shell=False,
            start_new_session=True,
        )
        _event(evidence, "running", pid=process.pid)
        def close_group():
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if evidence:
            return _wait_file_output(process, argv, close_group, timeout=timeout,
                                     check=check, evidence=evidence, text=text,
                                     encoding=encoding, errors=errors)
        return _wait_and_collect(process, argv, close_group, timeout=timeout,
                                 check=check, evidence=evidence)


def _wait_file_output(process, argv, close_tree, *, timeout, check, evidence,
                      text, encoding, errors):
    """Read durable native output only after owned descendants are stopped."""
    interruption = None
    try:
        process.wait(timeout=timeout)
        _event(evidence, "root_exit", exit_code=process.returncode)
    except BaseException as exc:
        interruption = exc
        _event(evidence, "root_interrupted", error=f"{type(exc).__name__}: {exc}")
    cleanup_error = None
    try:
        close_tree()
        _event(evidence, "owned_tree_stopped", exit_code=process.poll())
    except BaseException as exc:
        cleanup_error = exc
        _event(evidence, "cleanup_unverified", error=f"{type(exc).__name__}: {exc}")
    if cleanup_error is None:
        process.wait()
    stdout_bytes = (evidence / "stdout.bin").read_bytes()
    stderr_bytes = (evidence / "stderr.bin").read_bytes()
    if text:
        codec = encoding or locale.getencoding()
        stdout = stdout_bytes.decode(codec, errors=errors or "strict")
        stderr = stderr_bytes.decode(codec, errors=errors or "strict")
    else:
        stdout, stderr = stdout_bytes, stderr_bytes
    if cleanup_error is not None:
        cleanup_error.stdout = stdout
        cleanup_error.stderr = stderr
        cleanup_error.returncode = process.poll()
        raise cleanup_error
    if interruption is not None:
        if isinstance(interruption, subprocess.TimeoutExpired):
            interruption.output, interruption.stderr = stdout, stderr
        else:
            interruption.stdout, interruption.stderr = stdout, stderr
            interruption.returncode = process.returncode
        raise interruption
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def _wait_and_collect(process, argv, close_tree, *, timeout, check, evidence=None):
    """Persist output as it arrives, then close descendants before waiting for EOF."""
    output, errors = [], []
    reader_errors = []
    text_mode = isinstance(process.stdout, io.TextIOBase)
    stdout_encoding = process.stdout.encoding if text_mode else None
    stderr_encoding = process.stderr.encoding if text_mode else None
    stdout_errors = process.stdout.errors if text_mode else None
    stderr_errors = process.stderr.errors if text_mode else None

    def drain(stream, chunks, filename):
        sink = None
        try:
            if evidence is not None:
                sink = (evidence / filename).open("wb", buffering=0)
            raw = stream.buffer if isinstance(stream, io.TextIOBase) else stream
            while True:
                block = raw.read1(65536)
                if not block:
                    break
                chunks.append(block)
                if sink is not None:
                    sink.write(block)
                    os.fsync(sink.fileno())
        except BaseException as exc:
            reader_errors.append(exc)
        finally:
            if sink is not None:
                sink.close()
            stream.close()

    readers = (
        threading.Thread(target=drain, args=(process.stdout, output, "stdout.bin"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, errors, "stderr.bin"), daemon=True),
    )
    for reader in readers:
        reader.start()
    interruption = None
    try:
        process.wait(timeout=timeout)
        _event(evidence, "root_exit", exit_code=process.returncode)
    except BaseException as exc:
        interruption = exc
        _event(evidence, "root_interrupted", error=f"{type(exc).__name__}: {exc}")
    cleanup_error = None
    try:
        # A descendant may hold an inherited pipe. Verified tree closure ends it
        # before joining readers; cleanup failure must never wait forever for EOF.
        close_tree()
        _event(evidence, "owned_tree_stopped", exit_code=process.poll())
    except BaseException as exc:
        cleanup_error = exc
        _event(evidence, "cleanup_unverified", error=f"{type(exc).__name__}: {exc}")
    if cleanup_error is None:
        process.wait()
        for reader in readers:
            reader.join()
    stdout_bytes = b"".join(output)
    stderr_bytes = b"".join(errors)
    if text_mode:
        stdout = stdout_bytes.decode(stdout_encoding, errors=stdout_errors or "strict")
        stderr = stderr_bytes.decode(stderr_encoding, errors=stderr_errors or "strict")
    else:
        stdout, stderr = stdout_bytes, stderr_bytes
    if cleanup_error is not None:
        cleanup_error.stdout = stdout
        cleanup_error.stderr = stderr
        cleanup_error.returncode = process.poll()
        raise cleanup_error
    if reader_errors:
        exc = RuntimeError("Could not retain complete child output")
        exc.stdout, exc.stderr, exc.returncode = stdout, stderr, process.returncode
        raise exc from reader_errors[0]
    if interruption is not None:
        if isinstance(interruption, subprocess.TimeoutExpired):
            interruption.output = stdout
            interruption.stderr = stderr
        else:
            interruption.stdout = stdout
            interruption.stderr = stderr
            interruption.returncode = process.returncode
        raise interruption
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result

from ..config import TIMEOUT_CONSTANTS
from .path_validator import PathValidator, get_default_validator

logger = logging.getLogger(__name__)


class SecureSubprocessError(Exception):
    """Raised when secure subprocess operations fail."""

    pass


class SecureSubprocessRunner:
    """
    Secure subprocess runner with validation and safety controls.

    Provides methods for safely executing KiCad CLI commands and other
    subprocess operations with proper input validation and security controls.
    """

    def __init__(self, path_validator: PathValidator | None = None):
        """
        Initialize secure subprocess runner.

        Args:
            path_validator: Path validator to use (defaults to global instance)
        """
        self.path_validator = path_validator or get_default_validator()
        self.default_timeout = TIMEOUT_CONSTANTS["subprocess_default"]

    def run_kicad_command(
        self,
        command_args: list[str],
        input_files: list[str] | None = None,
        output_files: list[str] | None = None,
        working_dir: str | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Run a KiCad CLI command with security validation.

        Args:
            command_args: Command arguments (excluding the kicad-cli executable)
            input_files: List of input file paths to validate
            output_files: List of output file paths to validate
            working_dir: Working directory for command execution
            timeout: Command timeout in seconds
            capture_output: Whether to capture stdout/stderr

        Returns:
            CompletedProcess result

        Raises:
            SecureSubprocessError: If validation fails or command fails
            KiCadCLIError: If KiCad CLI not found
            PathValidationError: If path validation fails
        """
        # Get and validate KiCad CLI path
        from .kicad_cli import get_kicad_cli_path
        kicad_cli = get_kicad_cli_path(required=True)

        # Validate input files
        if input_files:
            for file_path in input_files:
                self.path_validator.validate_path(file_path, must_exist=True)

        # Validate output file directories
        if output_files:
            for file_path in output_files:
                output_dir = os.path.dirname(file_path)
                if output_dir:  # Only validate if there's a directory component
                    self.path_validator.validate_directory(output_dir, must_exist=True)

        # Validate working directory
        if working_dir:
            working_dir = self.path_validator.validate_directory(working_dir, must_exist=True)

        # Construct full command
        full_command = [kicad_cli] + command_args

        # Log command for debugging (sanitized)
        logger.debug(
            "Executing KiCad command: %s (%d arguments)",
            os.path.basename(kicad_cli),
            len(full_command) - 1,
        )

        try:
            return self._run_subprocess(
                full_command,
                working_dir=working_dir,
                timeout=timeout or self.default_timeout,
                capture_output=capture_output,
            )
        except subprocess.SubprocessError as e:
            raise SecureSubprocessError(f"KiCad command failed: {e}") from e

    async def run_kicad_command_async(
        self,
        command_args: list[str],
        input_files: list[str] | None = None,
        output_files: list[str] | None = None,
        working_dir: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Async version of run_kicad_command.

        Args:
            command_args: Command arguments (excluding the kicad-cli executable)
            input_files: List of input file paths to validate
            output_files: List of output file paths to validate
            working_dir: Working directory for command execution
            timeout: Command timeout in seconds

        Returns:
            CompletedProcess result
        """
        # Run in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.run_kicad_command,
            command_args,
            input_files,
            output_files,
            working_dir,
            timeout,
            True,  # capture_output
        )

    def run_safe_command(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: float | None = None,
        *,
        allowed_commands: list[str],
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Run a general command with security validation.

        Args:
            command: Full command list including executable
            working_dir: Working directory for command execution
            timeout: Command timeout in seconds
            allowed_commands: List of allowed executables (whitelist)
            capture_output: Whether to capture stdout/stderr

        Returns:
            CompletedProcess result

        Raises:
            SecureSubprocessError: If validation fails or command fails
        """
        if not command:
            raise SecureSubprocessError("Command cannot be empty")

        executable = command[0]

        if not allowed_commands:
            raise SecureSubprocessError("run_safe_command requires a non-empty allowed_commands list")

        # Validate executable against whitelist
        if executable not in allowed_commands:
            raise SecureSubprocessError(f"Command '{executable}' not in allowed list")

        # Validate working directory
        if working_dir:
            working_dir = self.path_validator.validate_directory(working_dir, must_exist=True)

        # Log only the executable name and argument count to avoid exposing arguments.
        logger.debug(
            "Executing safe command: %s (%d arguments)",
            os.path.basename(executable),
            len(command) - 1,
        )

        try:
            return self._run_subprocess(
                command,
                working_dir=working_dir,
                timeout=timeout or self.default_timeout,
                capture_output=capture_output,
            )
        except subprocess.SubprocessError as e:
            raise SecureSubprocessError(f"Command failed: {e}") from e

    def create_temp_file(
        self, suffix: str = "", prefix: str = "kicad_mcp_", content: str | None = None
    ) -> str:
        """
        Create a temporary file within validated directories.

        Args:
            suffix: File suffix/extension
            prefix: File prefix
            content: Optional content to write to file

        Returns:
            Path to created temporary file
        """
        temp_path = self.path_validator.create_safe_temp_path(prefix.rstrip("_"), suffix)

        if content is not None:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)

        return temp_path

    def _run_subprocess(
        self,
        command: list[str],
        working_dir: str | None = None,
        timeout: float = TIMEOUT_CONSTANTS["subprocess_default"],
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Internal subprocess runner with consistent settings.

        Args:
            command: Command to execute
            working_dir: Working directory
            timeout: Timeout in seconds
            capture_output: Whether to capture output

        Returns:
            CompletedProcess result

        Raises:
            subprocess.SubprocessError: If command fails
        """
        kwargs = {
            "timeout": timeout,
            "cwd": working_dir,
            "text": True,
        }

        if capture_output:
            kwargs.update(
                {
                    "capture_output": True,
                    "check": False,  # Don't raise on non-zero exit code
                }
            )

        return run_contained(command, **kwargs)  # nosec B603 - input is validated


# Global secure subprocess runner instance
_subprocess_runner = None


def get_subprocess_runner() -> SecureSubprocessRunner:
    """Get the global secure subprocess runner instance."""
    global _subprocess_runner
    if _subprocess_runner is None:
        _subprocess_runner = SecureSubprocessRunner()
    return _subprocess_runner


def run_kicad_command(
    command_args: list[str],
    input_files: list[str] | None = None,
    output_files: list[str] | None = None,
    working_dir: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Convenience function to run KiCad command."""
    return get_subprocess_runner().run_kicad_command(
        command_args, input_files, output_files, working_dir, timeout
    )


async def run_kicad_command_async(
    command_args: list[str],
    input_files: list[str] | None = None,
    output_files: list[str] | None = None,
    working_dir: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Convenience function to run KiCad command asynchronously."""
    return await get_subprocess_runner().run_kicad_command_async(
        command_args, input_files, output_files, working_dir, timeout
    )


def create_temp_file(
    suffix: str = "", prefix: str = "kicad_mcp_", content: str | None = None
) -> str:
    """Convenience function to create temporary file."""
    return get_subprocess_runner().create_temp_file(suffix, prefix, content)
