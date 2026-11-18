"""Persistent workflow receipts with client-owned, contained execution.

Long native work is queued without a tool-call lifetime limit. Each request and
result remains on disk; shutdown interrupts owned work without deleting it.
This manager exposes no arbitrary command or executable selection to clients.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

PARAMETERS = {
    "kicad_cli": ({"command", "input_path", "options", "allow_mutation"}, set()),
    "inspect_bundle": ({"project"}, set()),
    "create_contract_draft": ({"project", "expected_digest", "profile", "variant"}, set()),
    "evaluate_contract": ({"project", "expected_digest"}, {"gate_report", "attestation_paths"}),
    "plan_placement": ({"project", "expected_digest", "contract_sha256", "variant",
                        "operation", "moves"}, set()),
    "evaluate_gates": ({"project", "expected_digest", "variant", "rules_path"}, set()),
    "query_scene": ({"project", "expected_digest", "bbox_mm"}, {"margin_mm"}),
    "release_preview": ({"project", "expected_digest", "variant", "fab",
                         "gate_report", "attestation_paths"},
                        {"rules_path", "fab_notes", "assembly"}),
    "release_finalize": ({"project", "expected_digest", "variant", "fab",
                          "preview_dir", "visual_review"}, set()),
}
OPERATIONS = frozenset(PARAMETERS)


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _native_cleanup_receipt(path, *, verified, worker_exit, worker_identity):
    """Parent-side receipt survives termination of the command-running worker."""
    events_path = path / "events.jsonl"
    if not events_path.is_file():
        return
    item = {"event": "parent_owned_tree_stopped" if verified else "parent_cleanup_unverified",
            "time": datetime.now(timezone.utc).isoformat(),
            "owned_tree_stopped": verified, "worker_exit_code": worker_exit,
            "worker_identity": worker_identity,
            "stdout_bytes_retained": (path / "stdout.bin").stat().st_size
            if (path / "stdout.bin").is_file() else None,
            "stderr_bytes_retained": (path / "stderr.bin").stat().st_size
            if (path / "stderr.bin").is_file() else None}
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _runtime():
    """A private instance of the vendored owned-process runtime (Job Objects, exact identities).

    Loaded fresh per WorkflowJobs, as before vendoring, so one instance's state (or a test's
    substitution of it) never leaks into another."""
    path = Path(__file__).with_name("winproc.py")
    spec = importlib.util.spec_from_file_location("workflow_jobs_winproc", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Owned-process runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_job_root() -> Path:
    """KICAD_MCP_JOB_DIR / config "job_dir", else the per-user data directory (never inside the package)."""
    from kicad_mcp.adaptive_editor import kicad_env
    try:
        configured = kicad_env.setting("job_dir", "KICAD_MCP_JOB_DIR")
    except kicad_env.ConfigError:
        configured = os.environ.get("KICAD_MCP_JOB_DIR")  # a bad config file must not stop the jobs runtime
    return Path(configured or kicad_env.user_data_dir() / "workflow-jobs")


class WorkflowJobs:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_job_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.native = _runtime()
        self.owner = self.native.identity(os.getpid())
        # Runtime-derived worker sizing; queued requests are persisted, not refused
        # or dropped when every execution slot is occupied.
        self.pool = ThreadPoolExecutor(max_workers=os.cpu_count() or 1,
                                       thread_name_prefix="kicad-workflow")
        self.lock = threading.RLock()
        self.entries = {}
        self.closed = False

    def _folder(self, job_id):
        if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
            raise ValueError("Invalid workflow job identity")
        folder = self.root / job_id
        if folder.is_symlink() or folder.resolve().parent != self.root:
            raise ValueError("Workflow job path escaped its journal")
        return folder

    def _state_lock(self, folder):
        digest = hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()
        return self.native.Mutex("Local\\KiCadMCPJournal-" + digest)

    def _save_state(self, folder, state):
        with self._state_lock(folder):
            # A Windows reader without FILE_SHARE_DELETE can block os.replace.
            # Commit each complete state to an append-only, fsynced journal first;
            # the familiar state.json is an atomic mirror, never the authority.
            state.pop("state_mirror_error", None)
            journal = folder / "state-history.jsonl"
            with journal.open("a", encoding="utf-8") as stream:
                # The leading newline isolates an interrupted prior append. A
                # reader can then recover the last complete valid transition.
                stream.write("\n" + json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                _write(folder / "state.json", state)
            except OSError as exc:
                # The canonical transition is durable. Keep a precise mirror
                # error, whether sharing or permanent, without failing the job.
                state["state_mirror_error"] = f"{type(exc).__name__}: {exc}"
                with journal.open("a", encoding="utf-8") as stream:
                    stream.write("\n" + json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())

    def _read_state(self, folder):
        journal = folder / "state-history.jsonl"
        if not journal.is_file():
            # Journals created before the append-only format remain readable.
            return json.loads((folder / "state.json").read_text(encoding="utf-8"))
        last = None
        with journal.open("rb") as stream:
            for record in stream:
                if not record.endswith(b"\n") or not record.strip():
                    continue  # A trailing fragment has no commit delimiter.
                try:
                    last = json.loads(record.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue  # Preserve damaged bytes and recover a valid record.
        if last is None:
            raise ValueError("Workflow state journal has no complete transition")
        return last

    def start(self, operation: str, arguments: dict) -> dict:
        if operation not in OPERATIONS or not isinstance(arguments, dict):
            raise ValueError("Unsupported workflow operation or arguments")
        required, optional = PARAMETERS[operation]
        if not required.issubset(arguments) or set(arguments) - required - optional:
            raise ValueError("Workflow arguments do not match the typed operation")
        # Reject unrepresentable request data before creating a job.
        json.dumps(arguments, allow_nan=False)
        with self.lock:
            if self.closed:
                raise RuntimeError("Workflow server is shutting down")
            job_id = str(uuid.uuid4())
            folder = self._folder(job_id)
            folder.mkdir()
            state = {"schema_version": 1, "job_id": job_id, "operation": operation,
                     "status": "queued", "owner": self.owner,
                     "created": datetime.now(timezone.utc).isoformat(),
                     "evidence_directory": str(folder),
                     "request_path": str(folder / "request.json"),
                     "result_path": str(folder / "result.json"),
                     "stdout_path": str(folder / "stdout.txt"),
                     "stderr_path": str(folder / "stderr.txt")}
            _write(folder / "request.json", {"operation": operation, "arguments": arguments})
            self._save_state(folder, state)
            entry = {"cancel": threading.Event(), "folder": folder, "state": state}
            self.entries[job_id] = entry
            try:
                entry["future"] = self.pool.submit(self._execute, entry)
            except BaseException as exc:
                state.update(status="failed", error=str(exc), exit_code=None,
                             owned_tree_stopped=True)
                (folder / "stdout.txt").touch(exist_ok=True)
                (folder / "stderr.txt").touch(exist_ok=True)
                _write(folder / "result.json", {"worker_completed": False,
                                                  "error": str(exc), "result": None})
                self._save_state(folder, state)
                self.entries.pop(job_id, None)
                raise
            return dict(state)

    def _execute(self, entry):
        state, folder, cancel = entry["state"], entry["folder"], entry["cancel"]
        process = None
        tree = None
        cleanup_error = None
        cleanup_verified = False
        try:
            (folder / "stdout.txt").touch(exist_ok=False)
            (folder / "stderr.txt").touch(exist_ok=False)
            if cancel.is_set():
                state.update(status="cancelled", exit_code=None, owned_tree_stopped=True)
                return
            tree = self.native.Tree()
            with (folder / "stdout.txt").open("w", encoding="utf-8") as out, \
                    (folder / "stderr.txt").open("w", encoding="utf-8") as err:
                process = tree.spawn([sys.executable, "-m", "kicad_mcp.workflow_job_worker",
                                      str(folder / "request.json"), str(folder / "result.json")],
                                     cwd=str(Path(__file__).resolve().parents[2]),
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err)
                state.update(status="running", process=self.native.identity(process.pid))
                self._save_state(folder, state)
                while process.poll() is None:
                    if cancel.wait(0.1):
                        break
            try:
                tree.close()
                cleanup_verified = True
            except BaseException as exc:
                cleanup_error = exc
            if process.poll() is None and not cleanup_verified:
                try:
                    process.kill()  # Exact Popen root; descendant closure remains unverified.
                    state["exit_code"] = process.wait()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if process.poll() is not None or cleanup_verified:
                state["exit_code"] = process.wait()
            else:
                state["exit_code"] = None
            state["owned_tree_stopped"] = cleanup_verified
            if cleanup_error is not None:
                state.update(status="cleanup_unverified",
                             cleanup_error=f"{type(cleanup_error).__name__}: {cleanup_error}")
                state["error"] = state["cleanup_error"]
                return
            if cancel.is_set():
                state["status"] = "cancelled"
            elif process.returncode != 0:
                state["status"] = "failed"
                if (folder / "result.json").is_file():
                    state["error"] = json.loads((folder / "result.json").read_text(encoding="utf-8")).get("error")
                else:
                    state["error"] = "Worker exited nonzero without a result receipt"
            elif not (folder / "result.json").is_file():
                state.update(status="failed", error="Worker produced no result receipt")
            else:
                result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                state["status"] = "completed" if result.get("worker_completed") is True else "failed"
                state["result_path"] = str(folder / "result.json")
        except BaseException as exc:
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            state.setdefault("exit_code", None)
        finally:
            if tree is not None and not cleanup_verified and cleanup_error is None:
                try:
                    tree.close()
                    cleanup_verified = True
                except BaseException as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                state.update(status="cleanup_unverified", owned_tree_stopped=False,
                             cleanup_error=f"{type(cleanup_error).__name__}: {cleanup_error}")
                state["error"] = state["cleanup_error"]
            elif tree is None or cleanup_verified:
                state["owned_tree_stopped"] = True
            if process is not None and process.poll() is None and not cleanup_verified:
                try:
                    process.kill()  # Exact owned root; descendant state is still unverified.
                    state["exit_code"] = process.wait()
                except BaseException as exc:
                    state.update(status="cleanup_unverified", owned_tree_stopped=False,
                                 cleanup_error=f"{type(exc).__name__}: {exc}")
                    state["error"] = state["cleanup_error"]
            if process is not None and process.poll() is not None:
                state.setdefault("exit_code", process.wait())
            (folder / "stdout.txt").touch(exist_ok=True)
            (folder / "stderr.txt").touch(exist_ok=True)
            native_evidence = []
            for parent in (folder / "gate-evidence", folder / "release-preview",
                           folder / "release-finalize"):
                candidate = parent / "native-command"
                if candidate.is_dir():
                    native_evidence.append(str(candidate))
            commands = folder / "native-commands"
            if commands.is_dir():
                native_evidence.extend(str(p) for p in sorted(commands.iterdir()) if p.is_dir())
            state["native_evidence"] = native_evidence
            for path in native_evidence:
                try:
                    _native_cleanup_receipt(Path(path), verified=state.get("owned_tree_stopped") is True,
                                            worker_exit=state.get("exit_code"),
                                            worker_identity=state.get("process"))
                except (OSError, ValueError) as exc:
                    state.setdefault("native_evidence_errors", []).append(
                        {"path": path, "error": f"{type(exc).__name__}: {exc}"})
            if not (folder / "result.json").is_file():
                _write(folder / "result.json", {"worker_completed": False,
                                                  "error": state.get("error") or state["status"],
                                                  "result": None,
                                                  "native_evidence": native_evidence})
            state["result_path"] = str(folder / "result.json")
            state["worker_completed"] = bool(
                json.loads((folder / "result.json").read_text(encoding="utf-8")).get("worker_completed"))
            state["finished"] = datetime.now(timezone.utc).isoformat()
            self._save_state(folder, state)
            with self.lock:
                self.entries.pop(state["job_id"], None)

    def status(self, job_id: str) -> dict:
        folder = self._folder(job_id)
        with self._state_lock(folder):
            state = self._read_state(folder)
        if state["job_id"] != job_id:
            raise ValueError("Workflow journal identity mismatch")
        if state["status"] in {"queued", "running"}:
            owner = state.get("owner")
            if not owner or self.native.identity(owner["pid"]) != owner:
                worker = state.get("process")
                alive = bool(worker and self.native.identity(worker["pid"]) == worker)
                state["status"] = "owner_lost_worker_alive" if alive else "interrupted"
        result_path = folder / "result.json"
        if result_path.is_file():
            try:
                receipt = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                state["result_read_error"] = f"{type(exc).__name__}: {exc}"
                state["operation_ok"] = False
                state["design_accepted"] = False
                return state
            state["worker_completed"] = receipt.get("worker_completed") is True
            state["result"] = receipt.get("result")
            operation_ok = (bool(receipt.get("result", {}).get("ok"))
                            if isinstance(receipt.get("result"), dict) and "ok" in receipt["result"]
                            else state["worker_completed"])
            state["operation_ok"] = (state["status"] == "completed" and
                                     state.get("owned_tree_stopped") is True and operation_ok)
            state["design_accepted"] = bool(
                state["operation_ok"] and state["operation"] == "evaluate_contract" and
                isinstance(state["result"], dict) and
                state["result"].get("evaluation", {}).get("accepted") is True)
            if receipt.get("error"):
                state["error"] = receipt["error"]
        return state

    def cancel(self, job_id: str) -> dict:
        self._folder(job_id)
        with self.lock:
            entry = self.entries.get(job_id)
            if entry is None:
                state = self.status(job_id)
                if state["status"] in {"completed", "failed", "cancelled", "interrupted",
                                       "cleanup_unverified"}:
                    return dict(state, cancel_requested=False)
                raise ValueError("Only this server's owned jobs may be cancelled")
            entry["cancel"].set()
        state = self.status(job_id)
        state["cancel_requested"] = True
        return state

    def close(self):
        with self.lock:
            self.closed = True
            for entry in self.entries.values():
                entry["cancel"].set()
        self.pool.shutdown(wait=True, cancel_futures=False)
