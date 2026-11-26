"""L4 broker: ``ui_fallback`` and ``record_identifiers`` (docs/L4-UI-BROKER-SPEC.md).

``ui_fallback(operation, project_dir, params)`` runs ONE registered operation on a task-owned clone
of the project, with pcbnew on a private desktop (never the owner's), driven only through UIA
patterns by the helper process, under the user-desktop monitor. It never acts on the original
project, whose file hashes are compared before and after.

Order of refusals (no GUI is launched before all pass): registry approval by sha256, schema, KiCad
build equals the registry's build (read by one hidden, contained ``kicad-cli version`` run), unknown operation, typed parameter validation, project check.
Then: clone, desktop placement self-check, WTS snapshot, watchdog, launch, helper, steps, post-
condition, cleanup by exact process handle, WTS snapshot, original-unchanged check. The evidence
directory is always kept.

``record_identifiers`` opens the candidate dialogs in three independent launches, admits an
identifier only if it resolves to the same control in all three and writes the registry document.
Its hypotheses about KiCad's UI names (``CANDIDATES``) are starting points: anything that does not
resolve identically three times is dropped and reported, never guessed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import hidden_desktop as hd
from . import monitor
from .registry import (MAIN_DIALOG, Registry, RegistryRefusal, cancel_selector, check_build,
                       choose_menu_path, choose_selector, inspect_registry_file, load_registry,
                       norm_name, sha256_bytes, validate_params, validate_registry)

DEFAULT_EVIDENCE_ROOT = Path(tempfile.gettempdir()) / "kicad-mcp-l4"

# On Windows a file that was just created or renamed can be held open for a moment by another process
# (an anti-malware scanner or the search indexer), so opening or renaming it fails with a sharing violation
# (PermissionError). Channel files are retried for a short window instead of failing the call.
SHARING_RETRY_S = 5.0


def with_sharing_retry(action, window_s: float | None = None):
    end = time.time() + (SHARING_RETRY_S if window_s is None else window_s)
    while True:
        try:
            return action()
        except PermissionError:
            if time.time() >= end:
                raise
            time.sleep(0.05)


REQUIRED_LAUNCHES = 3
STARTUP_TIMEOUT_S = 180.0
HELPER_READY_TIMEOUT_S = 90.0
UIA_CALL_TIMEOUT_S = 60.0
MAX_CLONE_FILES = 5000
MAX_CLONE_BYTES = 512 * 1024 * 1024
IGNORED_WINDOW_CLASSES = {"#32768", "tooltips_class32", "IME", "MSCTFIME UI", "SysShadow"}
SIDE_FILES_NOTE = "KiCad may add lock and .kicad_prl files to the clone; the original is never touched"


class L4Error(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_error(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class UiaError(L4Error):
    pass


def _json_default(value):
    return str(value)


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=_json_default), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---- project, clone, original-unchanged -------------------------------------------------------------

def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return True


def tree_hashes(root: Path) -> dict[str, str]:
    """Relative POSIX path -> sha256 for every file under ``root``."""
    hashes = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            hashes[path.relative_to(root).as_posix()] = _sha256_file(path)
    return hashes


def check_project_dir(project_dir: Any) -> tuple[Path, Path]:
    """Absolute, plain project directory with exactly one top-level ``.kicad_pcb``. Returns (dir, board)."""
    if not isinstance(project_dir, (str, os.PathLike)) or not str(project_dir):
        raise L4Error("project_invalid", "project_dir must be a path")
    path = Path(project_dir)
    if not path.is_absolute():
        raise L4Error("project_invalid", "project_dir must be absolute")
    if not path.is_dir():
        raise L4Error("project_not_found", f"project directory not found: {path}")
    count = size = 0
    for current, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            entry = Path(current) / name
            if _is_link(entry):
                raise L4Error("project_has_links", f"refusing a project containing a link: {entry}")
        for name in files:
            count += 1
            size += (Path(current) / name).stat().st_size
        if count > MAX_CLONE_FILES or size > MAX_CLONE_BYTES:
            raise L4Error("project_too_large", f"project exceeds {MAX_CLONE_FILES} files or {MAX_CLONE_BYTES} bytes")
    boards = sorted(p for p in path.glob("*.kicad_pcb") if p.is_file())
    if len(boards) != 1:
        raise L4Error("board_not_unique", f"expected exactly one top-level .kicad_pcb, found {len(boards)}")
    return path, boards[0]


def make_clone(project: Path, board: Path, clone_dir: Path) -> Path:
    shutil.copytree(project, clone_dir)
    return clone_dir / board.name


def _stat_file(path: Path) -> dict:
    status = path.stat()
    return {"sha256": _sha256_file(path), "mtime_ns": status.st_mtime_ns, "size": status.st_size}


# ---- kicad-cli (hidden, contained) ------------------------------------------------------------------

def _cli_path(app_root: Path) -> Path:
    cli = app_root / "bin" / "kicad-cli.exe"
    if not cli.is_file():
        raise L4Error("kicad_cli_not_found", f"kicad-cli not found: {cli}")
    return cli


def detect_kicad_build(app_root: Path) -> str:
    from ..utils import secure_subprocess

    done = secure_subprocess.run_contained([str(_cli_path(app_root)), "version", "--format", "plain"],
                                           timeout=60, text=True, encoding="utf-8", errors="replace")
    build = (done.stdout or "").strip().splitlines()[0].strip() if done.returncode == 0 and (done.stdout or "").strip() else ""
    if not build:
        raise L4Error("kicad_build_unknown", "kicad-cli version did not report a build", exit_code=done.returncode)
    return build


def kicad_cli_loads_board(app_root: Path, board: Path, out_dir: Path) -> dict:
    """Post-condition helper: the board still loads (kicad-cli can export its edge layer)."""
    from ..utils import secure_subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(_cli_path(app_root)), "pcb", "export", "svg", "--layers", "Edge.Cuts", "--mode-multi",
            "--output", str(out_dir), str(board)]
    done = secure_subprocess.run_contained(argv, cwd=str(out_dir), timeout=120, text=True, encoding="utf-8",
                                           errors="replace")
    svgs = sorted(p.name for p in out_dir.glob("*.svg"))
    return {"loads": done.returncode == 0 and bool(svgs), "exit_code": done.returncode, "svg_files": svgs,
            "stderr_tail": (done.stderr or "")[-400:]}


# ---- UIA client -------------------------------------------------------------------------------------

class UiaClient:
    """File-channel client for the helper process. Replies are plain JSON; the helper never prints."""

    def __init__(self, channel: Path, proc: hd.OwnedProcess):
        self.channel = channel
        self.proc = proc
        self._counter = 0

    def call(self, command: str, args: dict | None = None, timeout_s: float = UIA_CALL_TIMEOUT_S) -> dict:
        self._counter += 1
        number = f"{self._counter:06d}"
        final = self.channel / f"cmd-{number}.json"
        temporary = self.channel / f"cmd-{number}.json.tmp"
        payload = json.dumps({"id": self._counter, "cmd": command, "args": args or {}})
        with_sharing_retry(lambda: temporary.write_text(payload, encoding="utf-8"))
        with_sharing_retry(lambda: os.replace(temporary, final))
        reply_path = self.channel / f"reply-{number}.json"
        end = time.time() + timeout_s
        while time.time() < end:
            if reply_path.exists():
                reply = json.loads(with_sharing_retry(lambda: reply_path.read_text(encoding="utf-8")))
                if reply.get("ok"):
                    return reply["result"]
                error = reply.get("error") or {}
                raise UiaError(error.get("code", "uia_error"), error.get("message", "UIA command failed"),
                               command=command, helper_details=error.get("details"))
            if not self.proc.alive():
                raise UiaError("helper_exited", "the UIA helper exited", command=command)
            time.sleep(0.05)
        raise UiaError("helper_timeout", f"no reply to {command!r} within {timeout_s}s", command=command)


# ---- one hidden-desktop KiCad session ---------------------------------------------------------------

class HiddenSession:
    """Placement check, monitor, KiCad and the UIA helper for one run. ``close()`` is always safe to call."""

    def __init__(self, run_dir: Path, board: Path, app_root: Path, base_env: Mapping[str, str] | None = None,
                 kicad_build: str | None = None):
        self.run_dir = run_dir
        self.settings_version = hd.settings_version_of(kicad_build) if kicad_build else None
        self.board = board
        self.app_root = app_root
        self.base_env = base_env
        self.kicad: hd.HiddenKiCad | None = None
        self.helper_proc: hd.OwnedProcess | None = None
        self.uia: UiaClient | None = None
        self.pids = monitor.TaskPids()
        self.watchdog: monitor.OwnerDesktopWatchdog | None = None
        self.violated = threading.Event()
        self.violations: list[dict] = []
        self.extra_violations: list[dict] = []
        self.cleanup_errors: list[dict] = []  # cleanup problems; never a reason to skip the next cleanup step
        self.placement: dict | None = None
        self.wts_before: dict | None = None
        self.summary: dict = {}
        self._channel = run_dir / "uia-channel"
        self._closed = False

    def open(self) -> "HiddenSession":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.placement = hd.placement_check(workdir=self.run_dir / "placement")
        if not self.placement["placed_correctly"]:
            raise L4Error("placement_failed", "desktop placement self-check failed; no GUI was launched",
                          placement=self.placement)
        self.wts_before = monitor.wts_snapshot()
        self.watchdog = monitor.OwnerDesktopWatchdog(self.pids, self._on_violation)
        self.watchdog.start()
        try:
            self.kicad = hd.HiddenKiCad(self.board, self.run_dir / "profile", app_root=self.app_root,
                                        settings_version=self.settings_version,
                                        base_env=self.base_env)
            self.kicad.start(on_created=self._register_kicad)
            self._start_helper()
        except hd.VisibleWindowError as exc:
            self.extra_violations.append({"kind": "tripwire_task_window_on_input_desktop", "detail": str(exc)})
            self.close()
            raise L4Error("task_window_on_input_desktop", str(exc)) from exc
        except BaseException:
            self.close()
            raise
        return self

    def _on_violation(self, kind: str, details: Any) -> None:
        self.violations.append({"kind": kind, "details": details})
        self.violated.set()
        if self.kicad:
            self.kicad.kill()
        if self.helper_proc:
            self.helper_proc.kill()

    def check(self) -> None:
        if self.violated.is_set():
            raise L4Error("owner_desktop_violation", "a task window or focus change reached the user's desktop; "
                          "the instance was killed", violations=self.violations)

    def _start_helper(self) -> None:
        assert self.kicad is not None
        self._channel.mkdir(parents=True, exist_ok=True)
        helper = Path(__file__).with_name("uia_helper.py")
        sites = " ".join(f'--site "{s}"' for s in hd.site_packages_paths())
        arguments = (f'-I "{helper}" --channel "{self._channel}" --pid {self.kicad.pid} '
                     f'--desktop {self.kicad.desktop_name} {sites}')
        self.helper_proc = hd.spawn_windowless_on_desktop(arguments, self.kicad.desktop_name,
                                                          str(self._channel), "uia-helper")
        self.pids.add(self.helper_proc.pid)
        self.pids.add_owner(self.helper_proc)
        end = time.time() + HELPER_READY_TIMEOUT_S
        ready = self._channel / "ready.json"
        fatal = self._channel / "fatal.json"
        while time.time() < end:
            self.check()
            if ready.exists():
                info = json.loads(with_sharing_retry(lambda: ready.read_text(encoding="utf-8")))
                if info.get("thread_desktop") != self.kicad.desktop_name or info.get("target_pid") != self.kicad.pid:
                    raise L4Error("helper_binding_mismatch", "UIA helper is not on the expected desktop/PID", ready=info)
                self.uia = UiaClient(self._channel, self.helper_proc)
                return
            if fatal.exists():
                raise L4Error("helper_failed_to_start", "UIA helper could not start",
                              fatal=json.loads(with_sharing_retry(lambda: fatal.read_text(encoding="utf-8"))))
            if not self.helper_proc.alive():
                raise L4Error("helper_failed_to_start", "UIA helper exited during startup")
            time.sleep(0.2)
        raise L4Error("helper_failed_to_start", "UIA helper did not become ready in time")

    def _cleanup_step(self, step: str, action):
        """Run one cleanup step; a failure is recorded and the next step still runs."""
        try:
            return action()
        except BaseException as exc:  # including KeyboardInterrupt: KiCad and the watchdog must still stop
            self.cleanup_errors.append({"step": step, "error": repr(exc)})
            return None

    def _register_kicad(self, pid: int) -> None:
        """Called by HiddenKiCad.start as soon as pcbnew exists (before its start-up tripwire): the watchdog
        then watches the root and every member of its Job Object (descendants, even orphaned ones)."""
        self.pids.add(pid)
        if self.kicad is not None and self.kicad.proc is not None:
            self.pids.add_owner(self.kicad.proc)

    def close(self) -> None:
        """Stop the helper and KiCad by exact handle, close the desktop, finish the monitor. Idempotent."""
        if self._closed:
            return
        self._closed = True
        stopped: dict[str, Any] = {}
        if self.helper_proc is not None:
            try:
                with_sharing_retry(lambda: (self._channel / "stop").write_text("stop", encoding="utf-8"))
                self.helper_proc.wait(3000)
            except BaseException as exc:  # the helper is killed below either way; KiCad must still be stopped
                self.cleanup_errors.append({"step": "helper_stop_request", "error": repr(exc)})
            finally:
                self._cleanup_step("helper_kill", self.helper_proc.kill)
                stopped["helper"] = {"pid": self.helper_proc.pid,
                                     "exit_code": self._cleanup_step("helper_exit_code", self.helper_proc.exit_code)}
                self._cleanup_step("helper_close", self.helper_proc.close)
        if self.kicad is not None:
            stopped["kicad"] = self._cleanup_step("kicad_identity", self.kicad.identity)
            self._cleanup_step("kicad_close", self.kicad.close)
        idle = {"input_desktop_samples": 0, "window_hits": [],
                "foreground": {"samples": 0, "changes": 0, "attributed": []}}
        wd = (self._cleanup_step("watchdog_stop", self.watchdog.stop) if self.watchdog else None) or idle
        try:
            self._finish_verdict(stopped, wd)
        except BaseException as exc:  # never an empty summary: the run is reported as failed, with the reason
            self.cleanup_errors.append({"step": "monitor_verdict", "error": repr(exc)})
            self.summary = {"placement": self.placement, "stopped": stopped, "cleanup_errors": self.cleanup_errors,
                            "monitor": {"ok": False, "violations": [{"kind": "monitor_verdict_failed",
                                                                     "error": repr(exc)}]}}

    def _finish_verdict(self, stopped: dict, wd: dict) -> None:
        wts_after = monitor.wts_snapshot()
        verdict = monitor.build_verdict(
            input_desktop_samples=wd["input_desktop_samples"], window_hits=wd["window_hits"],
            foreground=wd["foreground"], wts_diffs=monitor.compare_wts(self.wts_before, wts_after),
            extra_violations=self.extra_violations, input_desktop_unavailable=wd.get("input_desktop_unavailable") or 0)
        self.summary = {"placement": self.placement, "monitor": verdict, "wts_before": self.wts_before,
                        "wts_after": wts_after, "stopped": stopped, "cleanup_errors": self.cleanup_errors,
                        "watchdog_sampling_error": wd.get("sampling_error"),
                        "input_desktop_unavailable_samples": wd.get("input_desktop_unavailable")}


# ---- window helpers ---------------------------------------------------------------------------------

def window_matches(window: dict, match: dict) -> bool:
    actual, wanted = norm_name(window.get("name")), norm_name(match["name"])
    return {"exact": actual == wanted, "prefix": actual.startswith(wanted),
            "contains": wanted in actual}[match.get("match", "exact")]


def list_windows(session: HiddenSession) -> list[dict]:
    assert session.uia is not None
    return session.uia.call("find", {"window": None})["matches"]


def wait_for_main_window(session: HiddenSession, main_match: dict, timeout_s: float = STARTUP_TIMEOUT_S) -> dict:
    assert session.uia is not None and session.kicad is not None
    end = time.time() + timeout_s
    last = None
    while time.time() < end:
        session.check()
        if not session.kicad.alive():
            raise L4Error("kicad_exited", "pcbnew exited before its main window appeared")
        try:
            found = session.uia.call("find", {"window": main_match})
            if found["count"] == 1:
                return found["matches"][0]
            if found["count"] > 1:
                raise L4Error("main_window_ambiguous", f"{found['count']} windows match the main window",
                              matches=found["matches"])
        except UiaError as exc:
            last = exc.as_error()
        time.sleep(0.5)
    raise L4Error("main_window_timeout", "the pcbnew main window did not appear", last_error=last)


def find_unexpected(windows: list[dict], reg: Registry, op: dict) -> list[dict]:
    allowed = [reg.main_window, *reg.ambient_windows, *(d["window"] for d in op["dialogs"].values())]
    unexpected = []
    for window in windows:
        if window.get("control_type") != "Window" or not norm_name(window.get("name")):
            continue
        if window.get("class_name") in IGNORED_WINDOW_CLASSES:
            continue
        if not any(window_matches(window, match) for match in allowed):
            unexpected.append(window)
    return unexpected


def _capture(session: HiddenSession, run_dir: Path, tag: str, match: dict | None = None) -> str | None:
    """Best-effort UIA tree capture of the target process's windows."""
    try:
        assert session.uia is not None
        tree = session.uia.call("dump-tree", {"window": match, "max_depth": 8, "max_nodes": 2500})
        path = run_dir / f"uia-{tag}.json"
        _write_json(path, tree)
        return str(path)
    except Exception:
        return None


def handle_unexpected(session: HiddenSession, reg: Registry, window: dict, run_dir: Path, index: int) -> None:
    """Capture the tree; cancel only if the registry marks the dialog safe-to-cancel, else kill by PID."""
    exact = {"name": window["name"], "match": "exact"}
    dump = _capture(session, run_dir, f"unexpected-{index}", exact)
    known = next((d for d in reg.known_dialogs.values() if window_matches(window, d["window"])), None)
    action = "terminated"
    if known and known["safe_to_cancel"] and known["cancel"]:
        try:
            assert session.uia is not None
            session.uia.call("invoke", {"window": exact, "selector": cancel_selector(known["cancel"])})
            action = "cancelled"
        except Exception:
            action = "terminated"
    if action == "terminated" and session.kicad:
        session.kicad.kill()
    raise L4Error("unexpected_dialog", f"unexpected window {window.get('name')!r}: {action}",
                  window=window, action=action, uia_capture=dump)


def _check_unexpected(session: HiddenSession, reg: Registry, op: dict, run_dir: Path, counter: list[int]) -> None:
    unexpected = find_unexpected(list_windows(session), reg, op)
    if unexpected:
        counter[0] += 1
        handle_unexpected(session, reg, unexpected[0], run_dir, counter[0])


# ---- step execution ---------------------------------------------------------------------------------

def _poll_dialog(session, reg, op, run_dir, counter, match, present: bool, timeout_s: float) -> dict | None:
    end = time.time() + timeout_s
    while True:
        session.check()
        windows = list_windows(session)
        unexpected = find_unexpected(windows, reg, op)
        if unexpected:
            counter[0] += 1
            handle_unexpected(session, reg, unexpected[0], run_dir, counter[0])
        hits = [w for w in windows if window_matches(w, match)]
        if present and len(hits) == 1:
            return hits[0]
        if present and len(hits) > 1:
            raise L4Error("dialog_ambiguous", f"{len(hits)} windows match {match}", windows=hits)
        if not present and not hits:
            return None
        if time.time() >= end:
            raise L4Error("dialog_timeout", f"dialog {match} did not {'appear' if present else 'close'} in {timeout_s}s",
                          capture=_capture(session, run_dir, "dialog-timeout"))
        time.sleep(0.25)


def run_steps(session: HiddenSession, reg: Registry, op: dict, params: dict, run_dir: Path) -> tuple[dict, list[dict]]:
    """Execute the operation's registered steps; returns (outputs, trace)."""
    assert session.uia is not None
    outputs: dict[str, Any] = {}
    trace: list[dict] = []
    counter = [0]
    windows = {name: d["window"] for name, d in op["dialogs"].items()}

    def target(control: str) -> tuple[dict, dict]:
        selector = op["controls"][control]
        window = reg.main_window if selector["dialog"] == MAIN_DIALOG else windows[selector["dialog"]]
        return window, {key: value for key, value in selector.items() if key != "dialog"}

    for index, step in enumerate(op["steps"]):
        session.check()
        action = step["action"]
        record = {"index": index, "action": action}
        try:
            if action == "select_menu":
                record["result"] = session.uia.call("select-menu", {"window": reg.main_window,
                                                                     "path": step["path"], "invoke_leaf": True})
            elif action == "wait_dialog":
                _poll_dialog(session, reg, op, run_dir, counter, windows[step["dialog"]], True, step["timeout_s"])
            elif action == "wait_dialog_closed":
                _poll_dialog(session, reg, op, run_dir, counter, windows[step["dialog"]], False, step["timeout_s"])
            elif action == "set_value":
                window, selector = target(step["control"])
                result = session.uia.call("set-value", {"window": window, "selector": selector,
                                                        "value": str(params[step["param"]])})
                record["result"] = {"value_after": result["value_after"], "matched": result["matched"]}
                if step.get("verify", True) and not result["matched"]:
                    raise L4Error("value_not_applied", "the control did not keep the value that was set",
                                  value_after=result["value_after"])
            elif action == "invoke":
                window, selector = target(step["control"])
                record["result"] = _call_control(session, "invoke", {"window": window, "selector": selector})
            else:  # read_value
                window, selector = target(step["control"])
                result = _call_control(session, "read-value", {"window": window, "selector": selector})
                outputs[step["output"]] = result["value"]
                record["result"] = {"value": result["value"], "source": result["source"]}
            _check_unexpected(session, reg, op, run_dir, counter)
        except UiaError as exc:
            record["error"] = exc.as_error()
            trace.append(record)
            raise L4Error("ui_step_failed", f"step {index} ({action}) failed: {exc}", step=index,
                          trace=trace, uia_error=exc.as_error(),
                          capture=_capture(session, run_dir, f"step-{index}-failure")) from exc
        except L4Error as exc:
            record["error"] = exc.as_error()
            trace.append(record)
            exc.details.setdefault("trace", trace)
            raise
        trace.append(record)
    return outputs, trace


def check_postcondition(post: dict, *, board: Path, before: dict, params: dict, outputs: dict, started_ns: int,
                        app_root: Path, run_dir: Path) -> dict:
    kind = post["type"]
    after = _stat_file(board) if board.is_file() else None
    result: dict[str, Any] = {"type": kind, "passed": False}
    if kind == "file_rewritten":
        result["before"], result["after"] = before, after
        rewritten = bool(after) and (after["mtime_ns"] != before["mtime_ns"] or after["sha256"] != before["sha256"])
        result["rewritten"] = rewritten
        loads = {"loads": True, "skipped": True}
        if rewritten and post["loads_with_kicad_cli"]:
            loads = kicad_cli_loads_board(app_root, board, run_dir / "postcondition-svg")
        result["kicad_cli_load"] = loads
        result["passed"] = rewritten and bool(loads["loads"])
    elif kind == "files_exist":
        directory = Path(params[post["dir_param"]])
        end = time.time() + 30
        found: list[Path] = []
        while True:
            found = [p for p in sorted(directory.glob(post["glob"])) if p.is_file() and p.stat().st_mtime_ns >= started_ns]
            if len(found) >= post["min_count"] or time.time() >= end:
                break
            time.sleep(0.5)
        result["directory"] = str(directory)
        result["files"] = [{"name": p.name, "size": p.stat().st_size, "sha256": _sha256_file(p)} for p in found]
        result["passed"] = len(found) >= post["min_count"] and all(p.stat().st_size > 0 for p in found)
    else:  # file_unchanged
        unchanged = bool(after) and after["sha256"] == before["sha256"]
        have = all(isinstance(outputs.get(name), str) and outputs[name] != "" for name in post["required_outputs"])
        result.update(file_unchanged=unchanged, outputs_present=have, before=before, after=after)
        result["passed"] = unchanged and have
    return result


# ---- ui_fallback ------------------------------------------------------------------------------------

def _new_run_dir(evidence_root: str | os.PathLike | None, prefix: str) -> Path:
    root = Path(evidence_root) if evidence_root else DEFAULT_EVIDENCE_ROOT
    return root / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def _resolved_app_root(app_root) -> Path:
    """The KiCad install root, or an L4Error carrying kicad_env's own code (kicad_not_found,
    configured_path_invalid, config_invalid) so the caller gets a typed refusal, never an exception."""
    from kicad_mcp.adaptive_editor import kicad_env
    try:
        return hd.kicad_app_root(app_root)
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        raise L4Error(getattr(exc, "code", "kicad_not_found"), str(exc)) from exc


def ui_fallback(operation: Any, project_dir: Any, params: Any = None, *,
                registry_path: str | os.PathLike | None = None, evidence_root: str | os.PathLike | None = None,
                app_root: str | os.PathLike | None = None, approval_env: Mapping[str, str] | None = None,
                kicad_build: str | None = None, base_env: Mapping[str, str] | None = None) -> dict:
    """Run one registered L4 operation on a clone of ``project_dir``. Never raises for expected failures."""
    began = time.time()
    result: dict[str, Any] = {"operation": operation, "ok": False, "status": "refused", "launched": False,
                              "error": None, "registry_sha256": None, "kicad_build": None}
    try:
        reg = load_registry(registry_path, approval_env, skip_build_check=True)  # approval + schema first
        result["registry_sha256"] = reg.sha256
        root = _resolved_app_root(app_root)
        build = kicad_build or detect_kicad_build(root)
        result["kicad_build"] = build
        check_build(reg, build)
        op = reg.operation(operation)
        project, board = check_project_dir(project_dir)
        run_dir = _new_run_dir(evidence_root, f"l4-{operation}")
        clone_dir = run_dir / "clone"
        validated = validate_params(op, params, clone_dir)  # prospective clone path; nothing exists yet
    except RegistryRefusal as exc:
        result["error"] = exc.as_error()
        return result
    except L4Error as exc:
        result["error"] = exc.as_error()
        return result
    except Exception as exc:  # any other pre-launch failure (kicad-cli did not answer or could not be contained,
        # the project is unreadable): a typed refusal, never a raw exception; nothing has been launched yet
        result["error"] = L4Error("precondition_failed", f"{type(exc).__name__}: {exc}").as_error()
        return result

    result.update(status="failed", evidence_dir=str(run_dir), clone_dir=str(clone_dir), params=validated,
                  original_project=str(project))
    original_before = {}
    session: HiddenSession | None = None
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_json(run_dir / "request.json", {"operation": operation, "project_dir": str(project),
                                               "params": params, "registry_sha256": reg.sha256})
        original_before = tree_hashes(project)
        clone_board = make_clone(project, board, clone_dir)
        for name, value in validated.items():
            if op["params"][name]["type"] == "clone_path" and op["params"][name]["kind"] == "dir":
                Path(value).mkdir(parents=True, exist_ok=True)
        before = _stat_file(clone_board)
        started_ns = time.time_ns() - 2_000_000_000
        session = HiddenSession(run_dir, clone_board, root, base_env, build)
        session.open()
        result["launched"] = True
        result["kicad"] = session.kicad.identity() if session.kicad else None
        wait_for_main_window(session, reg.main_window)
        outputs, trace = run_steps(session, reg, op, validated, run_dir)
        result["outputs"], result["steps"] = outputs, trace
        post = check_postcondition(op["postcondition"], board=clone_board, before=before, params=validated,
                                   outputs=outputs, started_ns=started_ns, app_root=root, run_dir=run_dir)
        result["postcondition"] = post
        if not post["passed"]:
            result["error"] = {"code": "postcondition_failed", "message": f"{post['type']} postcondition failed",
                               "details": {}}
    except (L4Error, RegistryRefusal) as exc:
        result["error"] = exc.as_error()
    except (hd.LaunchPreconditionError, hd.VisibleWindowError, OSError) as exc:
        result["error"] = {"code": type(exc).__name__, "message": str(exc), "details": {}}
    except Exception as exc:  # a broker bug must still clean up and report
        result["error"] = {"code": "broker_exception", "message": f"{type(exc).__name__}: {exc}", "details": {}}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                result.setdefault("cleanup_error", f"{type(exc).__name__}: {exc}")
            result["session"] = session.summary
            result["monitor"] = session.summary.get("monitor")
        after = _hashes_after_run(project, result) if original_before else {}
        result["original_unchanged"] = bool(original_before) and after == original_before
        result["original_file_count"] = len(original_before)
    monitor_ok = bool(result.get("monitor") and result["monitor"]["ok"])
    if result["error"] is None and not monitor_ok:
        result["error"] = {"code": "owner_desktop_violation", "message": "the owner-desktop monitor reported violations",
                           "details": {"violations": (result.get("monitor") or {}).get("violations")}}
    if result["error"] is None and not result["original_unchanged"]:
        result["error"] = {"code": "original_project_changed", "message": "the original project changed", "details": {}}
    result["ok"] = result["error"] is None
    result["status"] = "ok" if result["ok"] else "failed"
    result["duration_s"] = round(time.time() - began, 2)
    result["note"] = SIDE_FILES_NOTE
    _write_result(run_dir, result)
    return result


def list_operations(registry_path=None, approval_env=None) -> dict:
    """Registry facts and the operations a caller may run (none unless approved and valid)."""
    info = inspect_registry_file(registry_path, approval_env)
    out: dict[str, Any] = {"registry": info, "operations": []}
    if not info.get("readable"):
        out["status"] = "refused"
        out["error"] = {"code": "registry_unreadable", "message": info.get("error")}
    elif not info.get("approved"):
        out["status"] = "refused"
        out["error"] = {"code": "registry_unapproved",
                        "message": f"registry sha256 {info['sha256']} is not approved; "
                                   "add a sha256 you recorded yourself to KICAD_MCP_L4_APPROVED_REGISTRY_SHA256 "
                                   "(comma-separated) in the server environment; the shipped registry is approved "
                                   "by kicad_mcp/l4/registry/APPROVED.sha256; all operations are refused"}
    elif not info.get("schema_valid"):
        out["status"] = "refused"
        out["error"] = {"code": "registry_invalid", "message": info.get("schema_error")}
    else:
        from .registry import describe_operations

        out["status"] = "available_pending_build_check"
        out["operations"] = describe_operations(load_registry(registry_path, approval_env, skip_build_check=True))
    return out


# ---- identifier recording ---------------------------------------------------------------------------

MAIN_CANDIDATE = {"name": "PCB Editor", "match": "contains"}

# Hypotheses about KiCad 10.0.5's UI names. They are only starting points for recording: a name that
# does not resolve to exactly one control, identically, in three launches is dropped and reported.
CANDIDATES: dict[str, dict] = {
    "pcb_open_and_save": {
        "description": "Open the project's board in pcbnew on a hidden desktop and save it (File > Save).",
        "api_alternative": "kicad-cli pcb upgrade rewrites a board without any UI; the pcbnew Python "
                           "SaveBoard() does the same headless.",
        "menu": [["File", "exact"], ["Save", "exact"]],
        "dialog": None, "controls": {}, "cancel": None, "params": {},
        "steps": [{"action": "select_menu", "path": "$menu"}],
        "postcondition": {"type": "file_rewritten", "file": "board", "loads_with_kicad_cli": True},
    },
    "pcb_export_gerbers_dialog": {
        "description": "File > Fabrication Outputs > Gerbers: set the clone-relative output directory and press Plot.",
        "api_alternative": "kicad-cli pcb export gerbers (kicad_cli_run) produces Gerber files without any UI.",
        "menu": [["File", "exact"], ["Fabrication Outputs", "exact"], ["Gerbers", "prefix"]],
        "dialog": {"id": "plot", "window_names": ["Plot"]},
        "controls": {
            "output_dir": {"control_types": ["Edit"], "names": ["Output directory:", "Output directory"]},
            "plot_button": {"control_types": ["Button"], "names": ["Plot"]},
            "close_button": {"control_types": ["Button"], "names": ["Close"], "automation_ids": ["5101"]},
        },
        "cancel": "close_button",
        "params": {"output_dir": {"type": "clone_path", "kind": "dir", "required": True,
                                  "description": "Output directory, relative to the project clone"}},
        "steps": [{"action": "select_menu", "path": "$menu"},
                  {"action": "wait_dialog", "dialog": "plot", "timeout_s": 30},
                  {"action": "set_value", "control": "output_dir", "param": "output_dir", "verify": True},
                  {"action": "invoke", "control": "plot_button"},
                  {"action": "invoke", "control": "close_button"},
                  {"action": "wait_dialog_closed", "dialog": "plot", "timeout_s": 60}],
        "postcondition": {"type": "files_exist", "dir_param": "output_dir", "glob": "*.gbr", "min_count": 1},
    },
    "pcb_board_setup_read": {
        "description": "Open File > Board Setup, go to Board Stackup > Physical Stackup, read the copper "
                       "layer count read-only, close with Cancel.",
        "api_alternative": "kicad-cli and the pcbnew Python API expose board settings (for example "
                           "BOARD.GetCopperLayerCount()) without any UI.",
        "menu": [["File", "exact"], ["Board Setup", "prefix"]],
        "dialog": {"id": "board_setup", "window_names": ["Board Setup"]},
        "controls": {
            # The dialog opens on another page; the field lives on the Physical Stackup page of its page tree.
            "stackup_page": {"control_types": ["TreeItem"], "names": ["Physical Stackup"]},
            "copper_layers": {"control_types": ["ComboBox", "Edit"], "on_page": "stackup_page",
                              "names": ["Copper layers:", "Copper layers", "Copper Layers:", "Copper Layers"]},
            "cancel_button": {"control_types": ["Button"], "names": ["Cancel"], "automation_ids": ["5101"]},
        },
        "cancel": "cancel_button",
        "params": {},
        "steps": [{"action": "select_menu", "path": "$menu"},
                  {"action": "wait_dialog", "dialog": "board_setup", "timeout_s": 30},
                  {"action": "invoke", "control": "stackup_page"},
                  {"action": "read_value", "control": "copper_layers", "output": "copper_layers"},
                  {"action": "invoke", "control": "cancel_button"},
                  {"action": "wait_dialog_closed", "dialog": "board_setup", "timeout_s": 30}],
        "postcondition": {"type": "file_unchanged", "file": "board", "required_outputs": ["copper_layers"]},
    },
}


def _menu_args(cand: dict) -> list[dict]:
    return [{"name": name, "match": match} for name, match in cand["menu"]]


PAGE_SETTLE_S = 5.0  # a freshly selected dialog page builds its controls asynchronously


def _resolve_with_wait(session: HiddenSession, window: dict, spec: dict, wait_s: float) -> dict | None:
    """_resolve_candidate, retried for up to ``wait_s`` (a control on a page that was just selected)."""
    end = time.time() + wait_s
    while True:
        found = _resolve_candidate(session, window, spec)
        if found is not None or time.time() >= end:
            return found
        session.check()
        time.sleep(0.25)


def _resolve_candidate(session: HiddenSession, window: dict, spec: dict) -> dict | None:
    """First control_type x name combination that resolves to exactly one control, as an observation."""
    assert session.uia is not None
    for control_type in spec["control_types"]:
        for name in spec["names"]:
            found = session.uia.call("find", {"window": window, "selector": {
                "by": "path", "name": name, "name_match": "exact", "control_type": control_type, "ancestry": []}})
            if found["count"] == 1:
                match = found["matches"][0]
                return {"name": match["name"], "control_type": match["control_type"],
                        "automation_id": match["automation_id"], "class_name": match["class_name"],
                        "ancestry": match["ancestry"], "match_count": 1, "enabled": match["enabled"],
                        "automation_id_matches": match.get("automation_id_matches")}
    # A name shared by several controls (e.g. two "Close" buttons) is not unique; fall back to a wx
    # stock id (wxID_OK/CANCEL/APPLY... are fixed constants, unlike the dynamic negative ids).
    for control_type in spec["control_types"]:
        for aid in spec.get("automation_ids", []):
            found = session.uia.call("find", {"window": window, "selector": {
                "by": "automation_id", "automation_id": aid, "control_type": control_type}})
            if found["count"] == 1:
                match = found["matches"][0]
                by_name = session.uia.call("find", {"window": window, "selector": {
                    "by": "path", "name": match["name"], "name_match": "exact", "control_type": control_type,
                    "ancestry": []}})["count"] if match["name"] else 0
                return {"name": match["name"], "control_type": match["control_type"],
                        "automation_id": match["automation_id"], "class_name": match["class_name"],
                        "ancestry": match["ancestry"], "match_count": by_name, "enabled": match["enabled"],
                        "automation_id_matches": 1}
    return None


def _selector_for(observation: dict) -> dict:
    """The selector that found ``observation``: the unique name path, else its automation id."""
    if observation.get("match_count") == 1:
        return {"by": "path", "name": observation["name"], "name_match": "exact",
                "control_type": observation["control_type"], "ancestry": []}
    return {"by": "automation_id", "automation_id": observation["automation_id"],
            "control_type": observation["control_type"]}


def _probe_menu_only(session: HiddenSession, cand: dict) -> dict:
    assert session.uia is not None
    found = session.uia.call("select-menu", {"window": MAIN_CANDIDATE, "path": _menu_args(cand), "invoke_leaf": False})
    return {"menu": [step["norm_name"] for step in found["steps"]], "leaf_enabled": found["leaf"]["enabled"]}


def _probe_dialog_op(session: HiddenSession, cand: dict, run_dir: Path, name: str) -> dict:
    assert session.uia is not None
    observed: dict[str, Any] = {"menu": None, "dialog_window": None, "controls": {}, "closed": False}
    found = session.uia.call("select-menu", {"window": MAIN_CANDIDATE, "path": _menu_args(cand), "invoke_leaf": True})
    observed["menu"] = [step["norm_name"] for step in found["steps"]]
    observed["dialog_opened"] = True  # from here on a dialog may be open: failure must abort the launch
    wanted = [{"name": n, "match": "exact"} for n in cand["dialog"]["window_names"]]
    end = time.time() + 30
    hits: list[dict] = []
    while time.time() < end:
        session.check()
        hits = [w for w in list_windows(session) if any(window_matches(w, m) for m in wanted)]
        if len(hits) == 1:
            break
        time.sleep(0.3)
    if len(hits) != 1:
        raise L4Error("dialog_not_found", f"dialog for {name} did not appear exactly once", hits=hits)
    window = {"name": hits[0]["name"], "match": "exact"}
    observed["dialog_window"] = {"name": norm_name(hits[0]["name"]), "control_type": hits[0]["control_type"]}
    _capture(session, run_dir, f"dialog-{name}", window)
    for ref, spec in cand["controls"].items():
        page = spec.get("on_page")
        if page:
            page_obs = observed["controls"].get(page)
            if not page_obs:  # the page itself did not resolve: neither can this control
                observed["controls"][ref] = None
                continue
            session.uia.call("invoke", {"window": window, "selector": _selector_for(page_obs)})
        observed["controls"][ref] = _resolve_with_wait(session, window, spec, PAGE_SETTLE_S if page else 0.0)
    cancel = observed["controls"].get(cand["cancel"])
    if not cancel:
        raise L4Error("cannot_close_dialog", f"{name}: the cancel control was not found; killing the instance")
    session.uia.call("invoke", {"window": window, "selector": _selector_for(cancel)})
    end = time.time() + 30
    while time.time() < end:
        if not any(window_matches(w, window) for w in list_windows(session)):
            observed["closed"] = True
            break
        time.sleep(0.3)
    if not observed["closed"]:
        raise L4Error("dialog_did_not_close", f"{name}: dialog stayed open after the cancel control was invoked")
    return observed


def _probe_launch(run_dir: Path, board: Path, app_root: Path, base_env, kicad_build: str | None = None) -> dict:
    """One independent launch: main window, ambient windows, then every candidate operation."""
    session = HiddenSession(run_dir, board, app_root, base_env, kicad_build)
    probe: dict[str, Any] = {"main_window": None, "baseline_windows": [], "ops": {}, "errors": []}
    try:
        session.open()
        found = wait_for_main_window(session, MAIN_CANDIDATE)
        probe["main_window"] = {"name": found["name"], "count": 1}
        probe["baseline_windows"] = [{"name": norm_name(w["name"]), "class_name": w["class_name"]}
                                     for w in list_windows(session)
                                     if w["control_type"] == "Window" and norm_name(w["name"])
                                     and w["class_name"] not in IGNORED_WINDOW_CLASSES
                                     and not window_matches(w, MAIN_CANDIDATE)]
        _capture(session, run_dir, "main-window", MAIN_CANDIDATE)
        for name, cand in CANDIDATES.items():
            try:
                probe["ops"][name] = (_probe_menu_only(session, cand) if cand["dialog"] is None
                                      else _probe_dialog_op(session, cand, run_dir, name))
            except (L4Error, UiaError) as exc:
                probe["ops"][name] = None
                probe["errors"].append({"operation": name, **exc.as_error()})
                if name != "pcb_open_and_save":  # a dialog may be open: do not continue this launch
                    break
    except (L4Error, hd.LaunchPreconditionError, hd.VisibleWindowError, OSError) as exc:
        probe["errors"].append({"operation": None, "code": type(exc).__name__, "message": str(exc),
                                **(exc.as_error() if isinstance(exc, L4Error) else {})})
    finally:
        session.close()
    probe["session"] = session.summary
    probe["monitor_ok"] = bool(session.summary.get("monitor") and session.summary["monitor"]["ok"])
    return probe


def _assemble(name: str, cand: dict, launches: list[dict], dropped: list[dict]) -> dict | None:
    """Build one registry operation from the per-launch observations, or None (dropped, with reasons)."""
    observations = [launch["ops"].get(name) for launch in launches]
    menu, why = choose_menu_path([o["menu"] if o else None for o in observations], len(launches))
    if menu is None:
        dropped.append({"operation": name, "element": "menu_path", "reason": why})
        return None
    if cand["dialog"] is None:
        if not all(o["leaf_enabled"] for o in observations):
            dropped.append({"operation": name, "element": "menu_leaf", "reason": "menu_item_disabled_on_unmodified_board"})
            return None
        dialogs: dict = {}
        controls: dict = {}
    else:
        names = {o["dialog_window"]["name"] if o and o["dialog_window"] else None for o in observations}
        if len(names) != 1 or None in names or not all(o["closed"] for o in observations):
            dropped.append({"operation": name, "element": "dialog_window", "reason": "dialog_not_stable_or_not_closed"})
            return None
        dialog_id = cand["dialog"]["id"]
        controls, problems = {}, []
        for ref in cand["controls"]:
            selector, reason = choose_selector([(o["controls"].get(ref) if o else None) for o in observations],
                                               len(launches))
            if selector is None:
                problems.append({"operation": name, "element": f"control:{ref}", "reason": reason})
            else:
                controls[ref] = {"dialog": dialog_id, **selector}
        cancel = controls.get(cand["cancel"])
        if problems:
            dropped.extend(problems)
            return None
        cancel_entry = ({"name": cancel["name"], "control_type": cancel["control_type"]} if cancel["by"] == "path"
                        else {"automation_id": cancel["automation_id"], "control_type": cancel["control_type"]})
        dialogs = {dialog_id: {"window": {"name": next(iter(names)), "match": "exact"}, "safe_to_cancel": True,
                               "cancel": cancel_entry}}
    steps = json.loads(json.dumps(cand["steps"]))
    for step in steps:
        if step.get("path") == "$menu":
            step["path"] = menu
    return {"description": cand["description"], "application": "pcbnew", "api_alternative": cand["api_alternative"],
            "params": cand["params"], "dialogs": dialogs, "controls": controls, "steps": steps,
            "postcondition": cand["postcondition"]}


def build_registry_document(kicad_build: str, launches: list[dict]) -> tuple[dict | None, dict]:
    """Pure: registry document from per-launch observations. Returns (document or None, report)."""
    report: dict[str, Any] = {"launches": len(launches), "admitted": [], "dropped": [], "ambient_unstable": []}
    if len(launches) != REQUIRED_LAUNCHES:
        report["fatal"] = f"need exactly {REQUIRED_LAUNCHES} launches, got {len(launches)}"
        return None, report
    if any(not launch.get("main_window") for launch in launches):
        report["fatal"] = "main window did not resolve in every launch"
        return None, report
    sets = [{(w["name"], w["class_name"]) for w in launch["baseline_windows"]} for launch in launches]
    stable = set.intersection(*sets)
    report["ambient_unstable"] = sorted(map(list, set.union(*sets) - stable))
    operations = {}
    for name, cand in CANDIDATES.items():
        op = _assemble(name, cand, launches, report["dropped"])
        if op is not None:
            operations[name] = op
            report["admitted"].append(name)
    if not operations:
        report["fatal"] = "no operation survived the 3-launch stability check"
        return None, report
    document = {"schema_version": 1, "kicad_build": kicad_build, "main_window": dict(MAIN_CANDIDATE),
                "ambient_windows": [{"name": n, "match": "exact"} for n, _c in sorted(stable)],
                "known_dialogs": {}, "operations": operations,
                "provenance": {"recorded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "tool": "kicad_mcp.l4.broker.record_identifiers", "launches": len(launches),
                               "dropped": report["dropped"], "ambient_unstable": report["ambient_unstable"],
                               "note": "known_dialogs is empty: any unexpected dialog terminates the instance"}}
    try:
        validate_registry(document)
    except RegistryRefusal as exc:
        report["fatal"] = f"assembled registry failed schema validation: {exc}"
        return None, report
    return document, report


def registry_bytes(document: dict) -> bytes:
    """Canonical bytes (LF newlines, sorted keys); the sha256 is over exactly these bytes."""
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def record_identifiers(project_dir: Any, *, registry_out: str | os.PathLike | None = None,
                       evidence_root: str | os.PathLike | None = None, app_root: str | os.PathLike | None = None,
                       kicad_build: str | None = None, base_env: Mapping[str, str] | None = None) -> dict:
    """Record candidate identifiers over three independent launches and write the registry document.

    The document goes to ``registry_out`` when given (the owner then approves its sha256), else only
    into the evidence directory as ``candidate-registry.json``. It is never written over the default
    registry path unless ``registry_out`` names it explicitly.
    """
    began = time.time()
    result: dict[str, Any] = {"ok": False, "status": "failed", "error": None, "launches_completed": 0}
    try:
        project, board = check_project_dir(project_dir)
        root = _resolved_app_root(app_root)
        build = kicad_build or detect_kicad_build(root)
        run_dir = _new_run_dir(evidence_root, "l4-record")
        run_dir.mkdir(parents=True, exist_ok=False)
        original_before = tree_hashes(project)
    except L4Error as exc:
        result.update(status="refused", error=exc.as_error())
        return result
    except Exception as exc:  # kicad-cli (incl. a containment failure), the evidence folder or the project
        result.update(status="refused", error=L4Error("precondition_failed", f"{type(exc).__name__}: {exc}").as_error())
        return result
    result.update(evidence_dir=str(run_dir), kicad_build=build)
    launches: list[dict] = []
    try:
        for index in range(1, REQUIRED_LAUNCHES + 1):
            launch_dir = run_dir / f"launch-{index}"
            launch_dir.mkdir()
            clone_board = make_clone(project, board, launch_dir / "clone")
            probe = _probe_launch(launch_dir, clone_board, root, base_env, build)
            _write_json(launch_dir / "probe.json", probe)
            launches.append(probe)
            result["launches_completed"] = index
            if not probe["monitor_ok"]:
                result["error"] = {"code": "owner_desktop_violation",
                                   "message": f"launch {index}: monitor reported violations; recording aborted",
                                   "details": probe["session"].get("monitor")}
                break
            if probe["main_window"] is None:
                result["error"] = {"code": "main_window_not_found", "message": f"launch {index}: no main window",
                                   "details": probe["errors"]}
                break
        if result["error"] is None:
            document, report = build_registry_document(build, launches)
            result["report"] = report
            if document is None:
                result["error"] = {"code": "no_registry", "message": report.get("fatal", "no registry"), "details": report}
            else:
                raw = registry_bytes(document)
                candidate = run_dir / "candidate-registry.json"
                candidate.write_bytes(raw)
                result.update(registry_sha256=sha256_bytes(raw), registry_size=len(raw),
                              candidate_registry=str(candidate))
                if registry_out:
                    target = Path(registry_out)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    result["registry_path"] = str(target)
                result["approval_instruction"] = (f"Set KICAD_MCP_L4_APPROVED_REGISTRY_SHA256 to "
                                                  f"{result['registry_sha256']}")
    except Exception as exc:
        result["error"] = {"code": "broker_exception", "message": f"{type(exc).__name__}: {exc}", "details": {}}
    result["original_unchanged"] = _hashes_after_run(project, result) == original_before
    if result["error"] is None and not result["original_unchanged"]:
        result["error"] = {"code": "original_project_changed", "message": "the original project changed", "details": {}}
    result["ok"] = result["error"] is None
    result["status"] = "ok" if result["ok"] else "failed"
    result["launch_summaries"] = [{"monitor_ok": l["monitor_ok"], "errors": l["errors"],
                                   "operations_observed": sorted(k for k, v in l["ops"].items() if v)}
                                  for l in launches]
    result["duration_s"] = round(time.time() - began, 2)
    _write_result(run_dir, result)
    return result


def _call_control(session: HiddenSession, command: str, payload: dict, wait_s: float = PAGE_SETTLE_S) -> dict:
    """A UIA control command, retried for a short bounded time while the control is not there yet (a dialog
    page that was just selected builds its controls asynchronously). Any other error fails at once."""
    end = time.time() + wait_s
    while True:
        try:
            return session.uia.call(command, payload)
        except UiaError as exc:
            if exc.code != "control_not_found" or time.time() >= end:
                raise
        session.check()
        time.sleep(0.25)


def _hashes_after_run(project: Path, result: dict) -> dict | None:
    """The original project's hashes after a run. If they cannot be read, the run cannot prove the original
    is unchanged: that is recorded and the run fails (None never equals the before-hashes); nothing raises."""
    try:
        return tree_hashes(project)
    except OSError as exc:
        result.setdefault("post_run_errors", []).append({"step": "hash_original", "error": f"{type(exc).__name__}: {exc}"})
        return None


def _write_result(run_dir: Path, result: dict) -> None:
    """Write result.json into the task's evidence folder; a failure is reported in the returned result
    (``result_json_error``), never raised, so the caller always gets the typed result."""
    try:
        _write_json(run_dir / "result.json", result)
    except OSError as exc:
        result["result_json_error"] = f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kicad_mcp.l4.broker")
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="record identifiers over 3 launches on a synthetic project")
    record.add_argument("project_dir")
    record.add_argument("--out", help="write the registry here (default: evidence dir only)")
    record.add_argument("--evidence-root")
    args = parser.parse_args(argv)
    outcome = record_identifiers(args.project_dir, registry_out=args.out, evidence_root=args.evidence_root)
    print(json.dumps(outcome, indent=2, default=_json_default))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
