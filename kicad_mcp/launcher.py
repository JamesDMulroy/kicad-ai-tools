"""On-demand stdio launcher: the MCP server process runs only while a client is actually using it.

An MCP client starts this launcher for every session. The launcher itself is small: it answers the
session handshake (``initialize``) and the catalogue requests (``tools/list``, ``prompts/list``,
``resources/list``, ``resources/templates/list``) from a manifest cache, and starts the real server
(``workflow_server.py``) only when a request needs it (a tool call, a resource read, a paged list).

The server is stopped again after ``server_idle_seconds`` without a request in flight, unless a
workflow job started through this session is still queued or running (job ids come from the
``start_*`` tool results; status from the job's journal). Its next request starts it again
transparently, replaying the client's ``initialize``. When the client ends the session, the launcher
waits for such jobs to finish before stopping the server. A server that crashes takes its jobs with it.

The manifest cache lives in the per-user data directory. It is rebuilt (one short server start) when
the package source or the server interpreter changes, so it never serves a stale tool list.

Messages are newline-delimited JSON-RPC (the MCP stdio transport). Requests the launcher makes to the
server use ids prefixed ``kicad-ai-tools-launcher:`` and are never forwarded to the client.
"""
from __future__ import annotations

import hashlib
import json
import math
import weakref
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from kicad_mcp.adaptive_editor import kicad_env

PACKAGE = Path(__file__).resolve().parent
SERVER_MODULE = "kicad_mcp.workflow_server"
CATALOGUE = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")
OWN_ID = "kicad-ai-tools-launcher:"
# Owner direction 2026-10-11: run the server only while something is using it. The idle period is a
# per-machine setting; the default keeps a burst of related calls on one server process.
DEFAULT_IDLE_SECONDS = 600


def _log(message: str) -> None:
    try:
        sys.stderr.write(f"[kicad-ai-tools launcher] {message}\n")
        sys.stderr.flush()
    except Exception:  # stderr may be closed by the client; logging must never break the session
        pass


def server_python() -> str:
    """The interpreter that runs the server: configured, else this interpreter (console variant)."""
    # KICAD_MCP_PYTHON is the older name still set by existing client configs; honour it as a fallback.
    configured = kicad_env.setting("server_python", "KICAD_AI_TOOLS_SERVER_PYTHON") or os.environ.get("KICAD_MCP_PYTHON")
    if configured:
        return str(configured)
    exe = Path(sys.executable)
    console = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    return str(console if console.is_file() else exe)


def source_digest() -> str:
    """Changes whenever any packaged file (code or data) or the server interpreter changes. Contents are
    hashed, not sizes and timestamps, so a same-size edit within one timestamp tick is still seen."""
    interpreter = server_python()
    h = hashlib.sha256(interpreter.encode())
    h.update(interpreter_fingerprint(Path(interpreter)).encode())
    for f in sorted(PACKAGE.rglob("*")):  # code AND packaged data (registry, plugin manifest, data files)
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        h.update(f.relative_to(PACKAGE).as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def interpreter_fingerprint(interpreter: Path) -> str:
    """What the server's handshake and tool schemas depend on outside this package, cheaply: the interpreter
    binary, the base interpreter behind a venv redirector (pyvenv.cfg ``home``/``version`` and the base
    binary), and the installed distributions (``*.dist-info`` names carry versions, so ``pip install -U`` of
    mcp, fastmcp, pydantic or anything else changes it). Nothing is executed."""
    parts = []

    def stat_of(path: Path) -> None:
        try:
            st = path.stat()
            parts.append(f"{path}|{st.st_size}|{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{path}|missing")

    stat_of(interpreter)
    roots = [interpreter.parent, interpreter.parent.parent]  # <venv>/Scripts/python.exe or <base>/python.exe
    for root in roots:
        cfg = root / "pyvenv.cfg"
        if cfg.is_file():
            try:
                text = cfg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            parts.append(text)
            for line in text.splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    stat_of(Path(value.strip()) / interpreter.name)
    for root in roots:
        site = root / "Lib" / "site-packages" if os.name == "nt" else None
        candidates = [site] if site else list(root.glob("lib/python*/site-packages"))
        for folder in candidates:
            if folder and folder.is_dir():
                try:
                    parts.extend(sorted(p.name for p in folder.iterdir() if p.name.endswith(".dist-info")))
                except OSError:
                    pass
    return "\n".join(parts)


def manifest_path() -> Path:
    return kicad_env.user_data_dir() / "mcp-manifest.json"


def idle_seconds() -> float:
    """The idle period; the default when the per-machine config cannot be read (logged), so a config
    that breaks mid-session never stops the idle reaper."""
    try:
        value = kicad_env.setting("server_idle_seconds", "KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)
    except kicad_env.ConfigError as exc:
        _log(f"idle period unreadable ({exc}); using {DEFAULT_IDLE_SECONDS}s")
        return float(DEFAULT_IDLE_SECONDS)
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(DEFAULT_IDLE_SECONDS)
    return max(5.0, seconds) if math.isfinite(seconds) else float(DEFAULT_IDLE_SECONDS)


def jobs_root() -> Path:
    from kicad_mcp.utils.workflow_jobs import default_job_root
    return default_job_root()


class Backend:
    """One server process (contained in a Job Object on Windows) with a reader thread."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.tree = None
        self.init_result = None  # the server's own initialize result, for a client on another protocol version
        argv = [server_python(), "-m", SERVER_MODULE]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE.parent), env.get("PYTHONPATH")]))
        if os.name == "nt":
            from kicad_mcp.utils import winproc
            self.tree = winproc.Tree()
            self.proc = self.tree.spawn(argv, cwd=str(PACKAGE.parent), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=sys.stderr if _has_fileno(sys.stderr) else subprocess.DEVNULL)
        else:
            self.proc = subprocess.Popen(argv, cwd=str(PACKAGE.parent), env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE)
        self.pid = self.proc.pid
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                try:
                    message = json.loads(line)
                except ValueError:
                    _log(f"server wrote a non-JSON line: {line[:200]}")
                    continue
                if not isinstance(message, dict):
                    _log(f"server wrote a non-object JSON message: {line[:200]}")
                    continue
                try:
                    self.on_message(self, message)
                except Exception as exc:  # one bad message must never kill the reader (EOF handling below)
                    _log(f"could not handle a server message: {exc!r}")
        self.on_message(self, None)  # EOF: the server exited

    def send(self, message: dict) -> None:
        with self.lock:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            self.proc.stdin.close()  # a polite end of the stdio session first
            self.proc.wait(10)
        except Exception:  # still running or the pipe is gone: the job object ends it
            pass
        if self.tree is not None:
            self.tree.close()
        elif self.proc.poll() is None:
            self.proc.kill()


def _has_fileno(stream) -> bool:
    try:
        stream.fileno()
        return True
    except Exception:  # pythonw / redirected streams without a descriptor
        return False


class Launcher:
    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.out_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.backend: Backend | None = None
        self.init_params: dict | None = None
        self.inflight: dict = {}   # client request id -> (method, tool name)
        self.jobs: set[str] = set()  # workflow job ids started through this session
        self.retired: "weakref.WeakSet[Backend]" = weakref.WeakSet()  # servers already retired
        self.job_owner: dict[str, "Backend"] = {}  # the server each job runs in
        self.last_activity = time.monotonic()
        self.pending: dict[str, tuple] = {}  # own request id -> (backend, event)
        self.replies: dict[str, dict] = {}
        self.manifest: dict | None = None
        self.counter = 0
        self.client_gone = False
        self.stop = threading.Event()

    # ------------------------------------------------------------ output
    def emit(self, message: dict) -> None:
        """Write to the client. Once the client is gone this is a no-op, so the server's reader thread
        keeps processing replies (job registration, in-flight cleanup) and its EOF."""
        with self.out_lock:
            if self.client_gone:
                return
            try:
                self.stdout.write(json.dumps(message) + "\n")
                self.stdout.flush()
            except (OSError, ValueError):
                self.client_gone = True

    # ------------------------------------------------------------ backend
    def _from_backend(self, source: "Backend", message):
        if message is None:
            for owner, event in list(self.pending.values()):
                if owner is source:
                    event.set()  # only this server's waiters see "exited before answering"
            with self.state_lock:
                if source is self.backend:
                    self.backend = None
            self._retire(source)  # also when a newer server already replaced it (EOF seen late)
            return
        mid = message.get("id")
        if isinstance(mid, str) and mid.startswith(OWN_ID):
            entry = self.pending.get(mid)
            if entry:  # a reply nobody waits for any more (timed out) is dropped, not kept
                self.replies[mid] = message
                entry[1].set()
            return
        if source is not self.backend:
            return  # only the session's current server talks to the client
        if "id" in message and ("result" in message or "error" in message):
            job_id = _job_id_from(message)
            with self.state_lock:  # one step: session end never sees the call gone but its job not yet known
                if source is not self.backend or self.inflight.get(mid, (None, None, None))[2] is not source:
                    return  # retired meanwhile: the client already got its error; never answer an id twice
                request = self.inflight.pop(mid)
                self.last_activity = time.monotonic()
                if job_id and request[0] == "tools/call" and str(request[1]).startswith("start_"):
                    self.jobs.add(job_id)
                    self.job_owner[job_id] = source
        self.emit(message)

    def _retire(self, dead: "Backend") -> None:
        """A server has exited: fail the calls it was answering and forget the jobs it ran (their threads and
        contained workers died with it). Calls not yet sent, and other servers' calls and jobs, are untouched.
        Safe to call more than once for the same server."""
        with self.state_lock:
            if dead in self.retired:
                return
            self.retired.add(dead)
            lost = [mid for mid, entry in self.inflight.items() if entry[2] is dead]
            for mid in lost:
                del self.inflight[mid]
            for job_id in [j for j, owner in self.job_owner.items() if owner is dead]:
                del self.job_owner[job_id]
                self.jobs.discard(job_id)
        for mid in lost:  # the client must not wait forever on a request the server never answered
            self.emit({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32603, "message": "kicad-ai-tools server exited; retry the call"}})
        if dead.tree is not None:
            dead.tree.close()

    def _own_request(self, backend: "Backend", method: str, params: dict | None, timeout: float = 120.0):
        self.counter += 1
        mid = f"{OWN_ID}{self.counter}"
        event = threading.Event()
        self.pending[mid] = (backend, event)
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            backend.send(msg)
            if not event.wait(timeout):
                raise TimeoutError(f"server did not answer {method}")
        finally:
            self.pending.pop(mid, None)
            late = self.replies.pop(mid, None)  # a reply landing between the timeout and this pop
        if late is not None:
            return late
        raise RuntimeError(f"server exited before answering {method}")

    def ensure_backend(self) -> Backend:
        with self.state_lock:
            if self.backend is not None and self.backend.alive():
                return self.backend
            dead, self.backend = self.backend, None
        if dead is not None:  # exited, but its reader has not reported EOF yet: retire it now
            self._retire(dead)
        with self.state_lock:
            if self.backend is not None and self.backend.alive():
                return self.backend
            _log("starting the server")
            backend = Backend(self._from_backend)
            try:
                reply = self._own_request(backend, "initialize", self.init_params or _default_init_params())
                if "error" in reply:
                    raise RuntimeError(f"server initialize failed: {reply['error']}")
                backend.init_result = reply.get("result")
                backend.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except BaseException:
                backend.stop()  # never leave a half-started server (or its job object) behind
                raise
            self.backend = backend
            return backend

    def stop_backend_if_idle(self) -> None:
        # covers: MCP-1
        with self.state_lock:
            b = self.backend
            if b is None or self.inflight or time.monotonic() - self.last_activity < idle_seconds():
                return
            self.jobs = {j for j in self.jobs if job_active(j)}
            if self.jobs:
                return  # a job started through this session is queued or running: keep its server
            self.backend = None
        _log("stopping the idle server")
        b.stop()

    # ------------------------------------------------------------ manifest
    def load_manifest(self) -> dict:
        digest = source_digest()  # rechecked on every use: a long session never serves a stale tool list
        if self.manifest is not None and self.manifest.get("digest") == digest:
            return self.manifest
        path = manifest_path()
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("digest") == digest:
                self.manifest = cached
                return cached
        except (OSError, ValueError):
            pass
        _log("building the tool manifest (one short server start)")
        backend = Backend(self._from_backend)
        try:
            init = self._own_request(backend, "initialize", self.init_params or _default_init_params())
            if "error" in init:
                raise RuntimeError(f"server initialize failed: {init['error']}")
            backend.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            manifest = {"digest": digest, "initialize": init.get("result"), "lists": {}}
            for method in CATALOGUE:
                reply = self._own_request(backend, method, {})
                manifest["lists"][method] = {k: reply[k] for k in ("result", "error") if k in reply}
        finally:
            backend.stop()
        self.manifest = manifest
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # unique per writer
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(manifest), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # another launcher writing at once, or an unwritable data folder
            _log(f"tool manifest not cached ({exc}); serving it from memory")
            try:
                tmp.unlink()
            except OSError:
                pass
        return manifest

    # ------------------------------------------------------------ client side
    def handle(self, message: dict) -> None:
        if not isinstance(message, dict):  # e.g. 5, "x", null or [1]: not a JSON-RPC message
            self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            return
        method, mid = message.get("method"), message.get("id")
        if method is None:  # a response to a server-initiated request
            self._send_quietly(message)
            return
        with self.state_lock:
            self.last_activity = time.monotonic()
        if method == "initialize":
            self.init_params = message.get("params") or {}
            try:
                manifest = self.load_manifest()
                result = manifest["initialize"] or {}
                wanted = self.init_params.get("protocolVersion")
                if wanted and wanted != result.get("protocolVersion"):
                    # The cache was negotiated for another version: let the real server negotiate with this
                    # client's own parameters, which ensure_backend also replays on every later start.
                    result = self.ensure_backend().init_result
                self.emit({"jsonrpc": "2.0", "id": mid, "result": result})
            except Exception as exc:
                self.emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}})
            return
        if method == "notifications/initialized":
            return  # replayed to every server start by ensure_backend
        if method == "ping":
            self.emit({"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        if method in CATALOGUE and not (message.get("params") or {}).get("cursor"):
            try:
                cached = self.load_manifest()["lists"].get(method, {})
            except Exception as exc:  # a manifest build failure answers this request, never ends the session
                self.emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}})
                return
            if "result" in cached:
                self.emit({"jsonrpc": "2.0", "id": mid, "result": cached["result"]})
            else:
                self.emit({"jsonrpc": "2.0", "id": mid, "error": cached.get("error") or
                           {"code": -32601, "message": f"{method} is not supported"}})
            return
        if mid is None:  # other notifications (e.g. cancelled) matter only to a running server
            self._send_quietly(message)
            return
        if isinstance(mid, str) and mid.startswith(OWN_ID):  # reserved for the launcher's own requests
            self.emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32600,
                       "message": f"request ids starting with {OWN_ID!r} are reserved by the launcher"}})
            return
        params = message.get("params") or {}
        with self.state_lock:  # registered BEFORE the server starts, so the idle check sees it
            self.inflight[mid] = (method, params.get("name") if isinstance(params, dict) else None, None)
        try:
            backend = self.ensure_backend()
            with self.state_lock:
                if mid in self.inflight:
                    self.inflight[mid] = self.inflight[mid][:2] + (backend,)
            backend.send(message)
        except Exception as exc:
            with self.state_lock:
                self.inflight.pop(mid, None)
            self.emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603,
                                                              "message": f"kicad-ai-tools server unavailable: {exc}"}})

    def _send_quietly(self, message: dict) -> None:
        """Forward to a running server; a server that just exited is reported by its EOF, not here."""
        backend = self.backend
        if backend is None:
            return
        try:
            backend.send(message)
        except (OSError, ValueError) as exc:
            _log(f"could not forward to the server: {exc!r}")

    def _idle_loop(self):
        while True:
            try:
                interval = min(15.0, idle_seconds() / 3)
            except Exception as exc:  # the reaper must never end the session, whatever the settings hold
                _log(f"idle period unreadable: {exc!r}")
                interval = 15.0
            if self.stop.wait(interval):
                return
            try:
                self.stop_backend_if_idle()
            except Exception as exc:  # the reaper must never end the session
                _log(f"idle check failed: {exc!r}")

    def run(self) -> int:
        threading.Thread(target=self._idle_loop, daemon=True).start()
        try:
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                    continue
                for item in (message if isinstance(message, list) and message else [message]):
                    try:
                        self.handle(item)
                    except Exception as exc:  # one bad request must never end the session
                        _log(f"could not handle a client message: {exc!r}")
                        mid = item.get("id") if isinstance(item, dict) else None
                        self.emit({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}})
        finally:
            self.stop.set()
            self.client_gone = True
            # The client has gone. Jobs started through this session - including start_* calls whose reply
            # (with the job id) has not arrived yet - run to completion first: stopping the server now
            # would cancel them. Jobs have no lifetime limit of their own, so this waits as long as they run.
            while self.backend is not None and self.backend.alive():
                with self.state_lock:
                    self.jobs = {j for j in self.jobs if job_active(j)}
                    starting = [m for m, (meth, name, *_owner) in self.inflight.items()
                                if meth == "tools/call" and str(name).startswith("start_")]
                    if not self.jobs and not starting:
                        break
                _log(f"session ended; waiting for {len(self.jobs)} job(s) and {len(starting)} job start(s)")
                time.sleep(2)
            backend, self.backend = self.backend, None
            if backend is not None:
                backend.stop()
        return 0


def _default_init_params() -> dict:
    return {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "kicad-ai-tools-launcher", "version": "1"}}


def _job_id_from(message: dict) -> str | None:
    """The job id in a start_* tool result (structured content, or JSON text content)."""
    result = message.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    candidates = [structured.get("result", structured)] if isinstance(structured, dict) else []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                candidates.append(json.loads(item.get("text") or ""))
            except ValueError:
                pass
    for c in candidates:
        if isinstance(c, dict) and isinstance(c.get("job_id"), str):
            return c["job_id"]
    return None


def job_active(job_id: str) -> bool:
    """Queued or running, read from the job's authoritative append-only journal (state-history.jsonl),
    falling back to the state.json mirror only for journals written before that format."""
    folder = jobs_root() / job_id
    last = None
    try:
        with (folder / "state-history.jsonl").open("rb") as stream:
            for raw in stream:
                try:
                    last = json.loads(raw)
                except ValueError:
                    continue  # an interrupted append; the previous complete transition stands
    except OSError:
        try:
            last = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
    if not (isinstance(last, dict) and last.get("status") in ("queued", "running")):
        return False
    return _owner_alive(last.get("owner"))


def _owner_alive(owner) -> bool:
    """The journal's owner (the server that queued the job) still runs: same pid AND same creation time,
    so a reused pid is not mistaken for it. Unknown (no owner recorded, or no way to check) counts as alive."""
    if not isinstance(owner, dict) or os.name != "nt":
        return True
    try:
        from kicad_mcp.utils import winproc
        return winproc.identity(int(owner["pid"])) == {"pid": int(owner["pid"]), "created": owner["created"]}
    except (KeyError, TypeError, ValueError):
        return True
    except OSError:  # e.g. access denied: cannot tell, so keep the job
        return True


def main(stdin=None, stdout=None) -> int:
    # covers: MCP-1
    return Launcher(stdin or sys.stdin, stdout or sys.stdout).run()


def _pipe_stream(number: int, *, writing: bool):
    """A text stream over an inherited standard handle (pythonw leaves sys.std* as None)."""
    import ctypes
    import io
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
    kind = kernel.GetFileType(handle)
    # stdin/stdout must be pipes (the MCP transport); diagnostics may also go to a disk file.
    if kind not in ({1, 3} if number == -12 else {3}):
        raise RuntimeError("MCP launcher requires redirected stdin/stdout/stderr")
    duplicate = wintypes.HANDLE()
    process = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(duplicate.value, os.O_BINARY | (os.O_WRONLY if writing else os.O_RDONLY))
    raw = os.fdopen(fd, "wb" if writing else "rb", buffering=0)
    return io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=writing, line_buffering=writing)


def pythonw_main() -> int:
    # covers: MCP-1
    """Entry point under pythonw.exe (no console window ever exists): restore the pipes, then run."""
    try:
        sys.stderr = sys.__stderr__ = _pipe_stream(-12, writing=True)
    except (RuntimeError, OSError):  # diagnostics only: a client may send stderr to NUL or not at all
        sys.stderr = sys.__stderr__ = open(os.devnull, "w", encoding="utf-8")
    sys.stdin = sys.__stdin__ = _pipe_stream(-10, writing=False)
    sys.stdout = sys.__stdout__ = _pipe_stream(-11, writing=True)
    return main(sys.stdin, sys.stdout)


if __name__ == "__main__":
    if os.name == "nt" and Path(sys.executable).name.lower() == "pythonw.exe":
        sys.exit(pythonw_main())
    sys.exit(main())
