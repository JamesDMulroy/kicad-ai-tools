"""Typed MCP tools for the kicad-cli 10.0.5 command registry.

Three tools are registered by :func:`register_cli_tools`:

* ``kicad_cli_commands`` - the full typed registry (43 leaf commands) with a run-level status.
* ``kicad_cli_describe`` - one command, its options, and each option's path role.
* ``kicad_cli_run`` - validated generic invocation of one leaf command.

Safety model (every refusal is a structured ``{"ok": False, "status": "refused", "error": {...}}``
result and happens before anything is launched):

* argv is built only from registry-validated options (``cli_registry.validate_options``); there is
  never free-form argv. ``--output`` is reserved and is emitted only from ``output_path``.
* Every path is absolute, has no ``..``, and is resolved (symlinks/junctions) before use. Read
  paths must stay inside the input's project directory. Write paths must stay inside the output
  root and must not already exist. Options whose metavar or name suggests a path and that have no
  explicit role in ``PATH_ROLES`` are refused (default deny).
* Mutating commands (and ``pcb drc --save-board``) need ``allow_mutation=True``. Before such a run a
  byte-exact, sha256-verified backup is made next to the input as ``<name>.bak-<utc timestamp>``
  (a whole sibling directory for directory inputs, plus a sibling backup of every rewritable KiCad
  file in the project directory for file inputs) and a manifest of the copies is written.
* On every run the project's KiCad files are hashed before and after; a change during a run that
  was not authorised is reported as an error (``unexpected_project_change``), never ignored.
* ``jobset run`` is listed but refused (jobsets can name arbitrary outputs and run commands);
  ``pcb export hpgl`` is refused (removed in KiCad 10); ``pcb render`` is callable but unverified
  (it may open an offscreen GL context; no window-free guarantee has been proven).
* The process runs only through ``secure_subprocess.run_contained`` (hidden window, owned job
  object, explicit timeout) with stdout/stderr going to an evidence directory and only a bounded
  head/tail returned, plus total byte counts and sha256.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from kicad_mcp.utils import cli_registry, secure_subprocess
from kicad_mcp.utils.cli_registry import CliRegistryError, Command, Option
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path

KICAD_CLI_RELEASE = "10.0.5"

MAX_STREAM_BYTES = 64 * 1024  # per stream returned to the caller
DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_OUTPUT_FILES = 200  # listed output / changed files; the total is always reported
MAX_WALK_ENTRIES = 20000  # output-root snapshot cap
MAX_PROJECT_FILES = 5000  # files visited while hashing the project directory

# Run-level status per command (everything not listed is "supported": validated generic invocation).
RUN_STATUS_OVERRIDES: dict[str, tuple[str, str]] = {
    "jobset run": (
        "blocked",
        "Jobsets can name arbitrary output destinations and contain 'execute command' or copy "
        "jobs, which would give a caller free command execution and writes outside the allowed "
        "roots. Listed in the registry, refused by kicad_cli_run.",
    ),
    "pcb export hpgl": (
        "deprecated",
        "kicad-cli 10.0.5 reports 'No longer supported as of KiCad 10.0.' Refused at run level. "
        "sch export hpgl is a different command and is not affected.",
    ),
    "pcb render": (
        "callable_unverified",
        "Renders through an offscreen 3D context. It is callable, but no proof exists that it "
        "never shows a window, so it is not claimed as supported and is not integration-tested.",
    ),
}
RUN_REFUSED = {"blocked": "blocked_command", "deprecated": "deprecated_command"}

# Explicit role for every value option that looks like a path (metavar or name mentions
# FILE/DIR/PATH/SHEET/OUTPUT). Anything path-like and absent here is refused at run time and
# reported by unclassified_path_options() (a unit test asserts that list is empty).
PATH_ROLES: dict[str, str] = {
    "--output": "reserved",  # only ever emitted from output_path
    "--drawing-sheet": "read_in_project",  # must resolve inside the input's project directory
    "--report-path": "write_file",  # pcb export drill; must be new, inside the output root
    "--report-file": "write_file",  # pcb import; must be new, inside the output root
    "--models-dir": "name_only",  # pcb export vrml; one folder name created under the output root
    "--file": "blocked",  # jobset run; the command itself is blocked
}
_PATHISH_METAVAR = re.compile(r"FILE|DIR|PATH|SHEET", re.IGNORECASE)
_PATHISH_NAME_TOKENS = frozenset({"file", "dir", "path", "sheet", "folder"})

# Files hashed in the project directory. BACKUP_SUFFIXES are the ones a command can rewrite.
BACKUP_SUFFIXES = frozenset(
    {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_sym", ".kicad_mod"}
)
_MANIFEST_SUFFIXES = BACKUP_SUFFIXES | {".kicad_dru", ".kicad_wks", ".kicad_jobset"}

_VARIANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_FOLDER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")


class _Refusal(Exception):
    """A refusal raised before launch; becomes a structured result."""

    def __init__(self, code: str, message: str, option: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.option = option


def _refused(command: Any, code: str, message: str, option: str | None = None,
             source: str = "kicad_cli_run") -> dict[str, Any]:
    return {
        "ok": False,
        "status": "refused",
        "command": command if isinstance(command, str) else (
            command.name if isinstance(command, Command) else None),
        "error": {"code": code, "message": message, "option": option, "source": source},
    }


# --- registry-facing helpers -------------------------------------------------------------------


def command_run_status(name: str) -> tuple[str, str | None]:
    override = RUN_STATUS_OVERRIDES.get(name)
    if override is not None:
        return override
    return "supported", None


def command_coverage() -> dict[str, Any]:
    """Counts that reconcile: supported + callable_unverified + blocked + deprecated = registered."""
    counts = {"supported": 0, "callable_unverified": 0, "blocked": 0, "deprecated": 0}
    commands = cli_registry.list_commands()
    for cmd in commands:
        counts[command_run_status(cmd.name)[0]] += 1
    return {
        "kicad_release": KICAD_CLI_RELEASE,
        "registered": len(commands),
        **counts,
        "runnable": counts["supported"] + counts["callable_unverified"],
    }


def option_path_role(option: Option) -> str | None:
    return PATH_ROLES.get(option.long)


def _looks_like_path(option: Option) -> bool:
    if not option.takes_value:
        return False
    if option.metavar and _PATHISH_METAVAR.search(option.metavar):
        return True
    return any(token in _PATHISH_NAME_TOKENS for token in option.long.lstrip("-").split("-"))


def unclassified_path_options() -> list[tuple[str, str]]:
    """Path-like value options with no explicit role (must stay empty)."""
    missing = []
    for cmd in cli_registry.list_commands():
        for option in cmd.options:
            if _looks_like_path(option) and option.long not in PATH_ROLES:
                missing.append((cmd.name, option.long))
    return missing


def _command_dict(cmd: Command) -> dict[str, Any]:
    data = cmd.to_dict()
    status, reason = command_run_status(cmd.name)
    data["run_status"] = status
    data["run_status_reason"] = reason
    data["may_write_input"] = cmd.may_write_input
    for option in data["options"]:
        option["path_role"] = PATH_ROLES.get(option["long"])
    return data


# --- path and text checks ----------------------------------------------------------------------


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_within(child: Any, parent: Any) -> bool:
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def _check_text(value: Any, what: str, *, allow_variant: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise _Refusal("unsafe_path", f"{what} must be a non-empty string")
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise _Refusal("unsafe_path", f"{what} contains NUL, CR or LF")
    if len(value) > 4096:
        raise _Refusal("unsafe_path", f"{what} is longer than 4096 characters")
    probe = value.replace("${VARIANT}", "") if allow_variant else value
    if "${" in probe:
        raise _Refusal("unsafe_path", f"{what} contains '${{' (only a literal ${{VARIANT}} is "
                       "allowed, and only in output paths)")
    return value


def _absolute_clean(raw: Any, what: str, *, allow_variant: bool = False) -> Path:
    path = Path(_check_text(raw, what, allow_variant=allow_variant))
    if not path.is_absolute():
        raise _Refusal("path_not_absolute", f"{what} must be an absolute path")
    if ".." in path.parts:
        raise _Refusal("path_traversal", f"{what} must not contain '..'")
    return path


def _resolve_lenient(path: Path) -> Path:
    """Resolve symlinks/junctions of the deepest existing ancestor and re-append the rest."""
    tail: list[str] = []
    current = path
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            break
        tail.append(current.name)
        current = parent
    resolved = current.resolve(strict=False)
    for part in reversed(tail):
        resolved = resolved / part
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- project manifest and output snapshots -----------------------------------------------------


def _is_project_file(name: str) -> bool:
    lower = name.lower()
    return (Path(lower).suffix in _MANIFEST_SUFFIXES or lower.endswith("-lib-table")
            or (lower.startswith("~") and lower.endswith(".lck")))


def _manifest(root: Path, *, all_files: bool, exclude: tuple[Path, ...] = (),
              skip_backups: bool = True) -> dict[str, dict[str, Any]]:
    """sha256 + size of the project files under root. Backup folders are skipped."""
    entries: dict[str, dict[str, Any]] = {}
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not (skip_backups and ".bak-" in d)
            and not any(_is_within(Path(dirpath) / d, ex) for ex in exclude))
        for name in sorted(filenames):
            visited += 1
            if visited > MAX_PROJECT_FILES:
                raise _Refusal(
                    "project_dir_too_large",
                    f"The project directory {root} holds more than {MAX_PROJECT_FILES} files, so "
                    "it cannot be hashed before and after the run. Keep each project in its own "
                    "directory.")
            path = Path(dirpath) / name
            if any(_is_within(path, ex) for ex in exclude):
                continue
            if not all_files and not _is_project_file(name):
                continue
            try:
                entries[str(path)] = {"size": path.stat().st_size, "sha256": _sha256_file(path)}
            except OSError as exc:  # for example a locked file: record, never skip silently
                entries[str(path)] = {"size": None, "sha256": None, "error": f"{type(exc).__name__}"}
    return entries


def _diff_manifests(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
                    ) -> list[dict[str, Any]]:
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        changes.append({
            "path": path,
            "change": "added" if old is None else "removed" if new is None else "modified",
            "sha256_before": old.get("sha256") if old else None,
            "sha256_after": new.get("sha256") if new else None,
        })
    return changes


def _snapshot_tree(root: Path) -> tuple[dict[str, tuple[int, int]], bool]:
    snapshot: dict[str, tuple[int, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if len(snapshot) >= MAX_WALK_ENTRIES:
                return snapshot, True
            path = os.path.join(dirpath, name)
            try:
                stat = os.lstat(path)
            except OSError:
                continue
            snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot, False


def _diff_snapshots(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]],
                    first: Path | None) -> dict[str, Any]:
    changed = sorted(p for p, stat in after.items() if before.get(p) != stat)
    if first is not None:
        lead = [p for p in changed if _norm(p) == _norm(first)]
        changed = lead + [p for p in changed if p not in lead]
    files = []
    for path in changed[:MAX_OUTPUT_FILES]:
        try:
            files.append({"path": path, "size": after[path][0],
                          "sha256": _sha256_file(Path(path)),
                          "change": "new" if path not in before else "changed"})
        except OSError as exc:
            files.append({"path": path, "size": after[path][0], "sha256": None,
                          "change": "new" if path not in before else "changed",
                          "error": type(exc).__name__})
    removed = sorted(p for p in before if p not in after)
    return {"files": files, "total": len(changed), "truncated": len(changed) > MAX_OUTPUT_FILES,
            "removed": removed[:MAX_OUTPUT_FILES], "removed_total": len(removed)}


# --- backup ------------------------------------------------------------------------------------


def _make_backup(command: Command, input_path: Path, is_dir: bool,
                 manifest_before: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    items: list[dict[str, Any]] = []
    try:
        if is_dir:
            dest = input_path.with_name(f"{input_path.name}.bak-{stamp}")
            if os.path.lexists(dest):
                raise _Refusal("backup_failed", f"backup target {dest} already exists")
            shutil.copytree(input_path, dest, symlinks=True)
            copied = _manifest(dest, all_files=True, skip_backups=False)
            source_files = {os.path.relpath(k, input_path): v for k, v in manifest_before.items()}
            backup_files = {os.path.relpath(k, dest): v for k, v in copied.items()}
            expected = {k: v for k, v in source_files.items()}
            # The source manifest skips nested backup folders; skip them on the copy side too.
            backup_files = {k: v for k, v in backup_files.items()
                            if ".bak-" not in os.path.dirname(k)}
            if expected != backup_files:
                raise _Refusal("backup_verification_failed",
                               f"backup copy {dest} does not match the source file hashes")
            items.append({"source": str(input_path), "backup": str(dest),
                          "files": len(expected),
                          "bytes": sum(v["size"] or 0 for v in expected.values())})
            manifest_rows = [{"relative_path": k, **v} for k, v in sorted(expected.items())]
            manifest_path = input_path.with_name(f"{input_path.name}.bak-{stamp}.manifest.json")
        else:
            others = sorted(Path(p) for p in manifest_before
                            if Path(p) != input_path and not Path(p).name.startswith("~")
                            and not manifest_before[p].get("error"))
            manifest_rows = []
            for source in [input_path, *others]:
                dest = source.with_name(f"{source.name}.bak-{stamp}")
                if os.path.lexists(dest):
                    raise _Refusal("backup_failed", f"backup target {dest} already exists")
                known = manifest_before.get(str(source))
                expected_hash = known["sha256"] if known and known.get("sha256") else _sha256_file(source)
                shutil.copy2(source, dest)
                if _sha256_file(dest) != expected_hash or _sha256_file(source) != expected_hash:
                    raise _Refusal("backup_verification_failed",
                                   f"backup copy {dest} does not match {source}")
                row = {"source": str(source), "backup": str(dest),
                       "size": dest.stat().st_size, "sha256": expected_hash}
                items.append(row)
                manifest_rows.append(row)
            manifest_path = input_path.with_name(f"{input_path.name}.bak-{stamp}.manifest.json")
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump({"created_utc": stamp, "command": command.name, "input": str(input_path),
                       "kind": "directory" if is_dir else "file", "entries": manifest_rows},
                      stream, indent=2)
    except _Refusal:
        raise
    except OSError as exc:
        raise _Refusal("backup_failed", f"could not create the backup: {type(exc).__name__}: {exc}")
    return {"created": True, "verified": True, "stamp": stamp, "location": "next to the input",
            "items": items[:MAX_OUTPUT_FILES], "item_count": len(items),
            "manifest_path": str(manifest_path)}


# --- option preparation ------------------------------------------------------------------------


def _classify_options(cmd: Command, options: Any) -> dict[str, Any]:
    """Resolve keys to canonical long names and apply the option-level refusals (no filesystem)."""
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise _Refusal("wrong_type", "options must be a mapping of option name to value")
    chosen: dict[str, Any] = {}
    for key, value in options.items():
        option = cmd.find_option(key)
        if option is None:
            raise _Refusal("unknown_option", f"{cmd.name}: unknown option {str(key)[:80]!r}")
        if value is None:
            continue
        if option.long in chosen:
            raise _Refusal("duplicate_option",
                           f"{option.long} was given more than once (aliases count as the same)",
                           option.long)
        role = PATH_ROLES.get(option.long)
        if role == "reserved":
            raise _Refusal("reserved_option",
                           "--output is reserved: pass output_path instead, it is the only way an "
                           "output location reaches argv", option.long)
        if role == "blocked":
            raise _Refusal("blocked_option", f"{option.long} is not accepted", option.long)
        if role is None and _looks_like_path(option):
            raise _Refusal("unclassified_path_option",
                           f"{option.long} looks like a path option but has no declared role; "
                           "refused by default", option.long)
        if option.long == "--force" and not cmd.mutating:
            raise _Refusal("force_refused",
                           "--force overwrites output files on export commands and is refused; "
                           "choose a new output_path", option.long)
        chosen[option.long] = value
    return chosen


def _resolve_path_option(option: Option, value: Any, project_root: Path | None,
                         out_root: Path | None) -> str:
    role = PATH_ROLES[option.long]
    if not isinstance(value, str):
        raise _Refusal("wrong_type", f"{option.long} expects a string", option.long)
    if role == "name_only":
        if (not _FOLDER_NAME_RE.fullmatch(value) or ".." in value):
            raise _Refusal("invalid_value",
                           f"{option.long} must be a single folder name (letters, digits, "
                           "'.', '_', '-', space), not a path", option.long)
        if out_root is None:
            raise _Refusal("output_root_required",
                           f"{option.long} creates a folder and needs an output_path", option.long)
        return value
    _check_text(value, option.long)
    path = Path(value)
    if ".." in path.parts:
        raise _Refusal("path_traversal", f"{option.long} must not contain '..'", option.long)
    if role == "read_in_project":
        if project_root is None:
            raise _Refusal("path_outside_project", f"{option.long} needs an input project",
                           option.long)
        if not path.is_absolute():
            path = project_root / path
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise _Refusal("path_not_found", f"{option.long} file does not exist", option.long)
        if not resolved.is_file() or not _is_within(resolved, project_root):
            raise _Refusal("path_outside_project",
                           f"{option.long} must be an existing file inside the input's project "
                           f"directory {project_root}", option.long)
        return str(resolved)
    if role == "write_file":
        if out_root is None:
            raise _Refusal("output_root_required",
                           f"{option.long} writes a file and needs an output_path", option.long)
        if not path.is_absolute():
            path = out_root / path
        target = _resolve_lenient(path)
        if not path.name or not _is_within(target, out_root) or _norm(target) == _norm(out_root):
            raise _Refusal("path_outside_output_root",
                           f"{option.long} must name a new file inside the output root {out_root}",
                           option.long)
        if os.path.lexists(target):
            raise _Refusal("output_exists", f"{option.long} target already exists: {target}",
                           option.long)
        return str(target)
    raise _Refusal("unclassified_path_option", f"{option.long} has no usable path role", option.long)


_NAME_ONLY_OPTIONS = ("--theme", "--preset", "--format-preset", "--default-font")


def _check_names(chosen: dict[str, Any]) -> None:
    """Theme/preset/font are names, not paths; --define-var must not smuggle ${...} expansions."""
    for long in _NAME_ONLY_OPTIONS:
        value = chosen.get(long)
        if value is not None and (not isinstance(value, str) or any(
                bad in value for bad in ("/", "\\", "..", "${"))):
            raise _Refusal("invalid_value",
                           f"{long} must be a plain name (no '/', '\\', '..' or '${{')", long)
    defs = chosen.get("--define-var")
    if defs is not None:
        for item in defs if isinstance(defs, (list, tuple)) else [defs]:
            if isinstance(item, str) and "${" in item:
                raise _Refusal("invalid_value", "--define-var values must not contain '${'",
                               "--define-var")


def _check_variants(chosen: dict[str, Any]) -> None:
    value = chosen.get("--variant")
    if value is None:
        return
    for item in value if isinstance(value, (list, tuple)) else [value]:
        if not isinstance(item, str) or not _VARIANT_RE.fullmatch(item) or ".." in item:
            raise _Refusal("invalid_value",
                           "--variant names may only use letters, digits, '.', '_' and '-' "
                           "(no separators, no '..')", "--variant")


# --- input and output resolution ---------------------------------------------------------------


def _resolve_input(cmd: Command, raw: Any) -> tuple[Path | None, Path | None, bool]:
    kind = cmd.positional.kind
    if kind == "none":
        if raw not in (None, ""):
            raise _Refusal("input_not_expected", f"{cmd.name} takes no input path")
        return None, None, False
    if raw in (None, ""):
        raise _Refusal("input_required", f"{cmd.name} needs an input_path ({cmd.positional.name})")
    path = _absolute_clean(raw, "input_path")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise _Refusal("input_not_found", f"input_path does not exist: {path}")
    is_dir = resolved.is_dir()
    if kind == "file" and not resolved.is_file():
        raise _Refusal("input_wrong_kind", f"{cmd.name} needs a file, got a directory")
    if kind == "dir" and not is_dir:
        raise _Refusal("input_wrong_kind", f"{cmd.name} needs a directory, got a file")
    if kind == "file_or_dir" and not (is_dir or resolved.is_file()):
        raise _Refusal("input_wrong_kind", "input_path must be a file or a directory")
    return resolved, (resolved if is_dir else resolved.parent), is_dir


def _resolve_output(cmd: Command, raw: Any, input_path: Path | None
                    ) -> tuple[Path | None, Path | None, str | None]:
    kind = cmd.output_kind
    if kind is None:
        if raw not in (None, ""):
            raise _Refusal("output_not_supported", f"{cmd.name} has no output location")
        return None, None, None
    if raw in (None, ""):
        if cmd.mutating and cmd.name != "pcb import":
            return None, None, None  # fp/sym upgrade: --output is optional, in-place without it
        raise _Refusal("output_path_required",
                       f"{cmd.name} needs an absolute output_path; without one kicad-cli would "
                       "write next to the input, inside the project tree")
    path = _absolute_clean(raw, "output_path", allow_variant=True)
    if not path.name:
        raise _Refusal("unsafe_path", "output_path must name a file or directory")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise _Refusal("output_parent_missing", f"the parent of output_path must exist: {path.parent}")
    if not parent.is_dir():
        raise _Refusal("output_parent_missing", "the parent of output_path is not a directory")
    target = parent / path.name
    effective = "dir" if kind == "dir" else "file"
    if os.path.lexists(target):
        empty_dir = effective == "dir" and target.is_dir() and not any(target.iterdir())
        if not empty_dir:
            raise _Refusal("output_exists",
                           f"output_path already exists: {target}; choose a new path"
                           + (" (an existing directory must be empty)" if effective == "dir" else ""))
    if input_path is not None and (_is_within(target, input_path) or _is_within(input_path, target)):
        raise _Refusal("output_overlaps_input",
                       "output_path must not be, contain or sit inside the input")
    return target, (target if effective == "dir" else parent), effective


# --- result streams ----------------------------------------------------------------------------


def _read_stream(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError:
        return {"text": "", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                "truncated": False, "available": False}
    digest = _sha256_file(path)
    with path.open("rb") as stream:
        if size <= MAX_STREAM_BYTES:
            text = stream.read().decode("utf-8", "replace")
            truncated = False
        else:
            half = MAX_STREAM_BYTES // 2
            head = stream.read(half)
            stream.seek(size - half)
            tail = stream.read(half)
            text = (head.decode("utf-8", "replace")
                    + f"\n...[{size - 2 * half} bytes omitted]...\n"
                    + tail.decode("utf-8", "replace"))
            truncated = True
    return {"text": text, "bytes": size, "sha256": digest, "truncated": truncated,
            "available": True}


# --- the run -----------------------------------------------------------------------------------


def run_cli(command: str, input_path: str | None = None, options: Mapping[str, Any] | None = None,
            output_path: str | None = None, allow_mutation: bool = False, *,
            timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
            evidence_base: str | Path | None = None) -> dict[str, Any]:
    """Validate and run one kicad-cli leaf command. Never raises for a refusal."""
    try:
        return _run_cli(command, input_path, options, output_path, bool(allow_mutation),
                        timeout_seconds, evidence_base)
    except _Refusal as exc:
        return _refused(command, exc.code, exc.message, exc.option)
    except CliRegistryError as exc:
        return _refused(command, exc.code, str(exc), exc.option, source="cli_registry")


def _run_cli(command: str, input_raw: str | None, options: Any, output_raw: str | None,
             allow_mutation: bool, timeout_seconds: float,
             evidence_base: str | Path | None) -> dict[str, Any]:
    cmd = cli_registry.get_command(command)
    status, status_reason = command_run_status(cmd.name)
    if status in RUN_REFUSED:
        raise _Refusal(RUN_REFUSED[status], f"{cmd.name}: {status_reason}")

    chosen = _classify_options(cmd, options)
    _check_variants(chosen)
    _check_names(chosen)
    writes_input = [long for long in chosen
                    if chosen[long] is True and cmd.find_option(long).writes_input]
    needs_mutation = cmd.mutating or bool(writes_input)
    if needs_mutation and not allow_mutation:
        why = "it rewrites KiCad files" if cmd.mutating else (
            f"{', '.join(writes_input)} writes the input board")
        raise _Refusal("mutation_refused",
                       f"{cmd.name} needs allow_mutation=True ({why}). Nothing was changed.")
    mutation_authorised = needs_mutation and allow_mutation

    input_path, project_root, is_dir = _resolve_input(cmd, input_raw)
    output_target, out_root, out_kind = _resolve_output(cmd, output_raw, input_path)

    for long in list(chosen):
        option = cmd.find_option(long)
        if option.long in PATH_ROLES:
            chosen[long] = _resolve_path_option(option, chosen[long], project_root, out_root)
    tokens = cli_registry.validate_options(cmd, chosen)

    try:
        cli_path = get_kicad_cli_path(required=True)
    except Exception as exc:
        raise _Refusal("kicad_cli_unavailable", f"kicad-cli could not be located: {exc}")

    try:  # before any backup or output directory exists, so a failure here changes nothing
        from kicad_mcp.adaptive_editor.kicad_env import user_data_dir
        from uuid import uuid4
        evidence_parent = Path(evidence_base) if evidence_base else user_data_dir() / "cli-evidence"
        evidence_parent.mkdir(parents=True, exist_ok=True)
        evidence_root = evidence_parent / ("kicad_cli_run_" + uuid4().hex)
        evidence_root.mkdir()
    except OSError as exc:
        raise _Refusal("evidence_dir_failed",
                       f"could not create the evidence directory: {type(exc).__name__}: {exc}")

    keep_evidence = True  # unexpected exceptions retain durable diagnostics
    try:
        exclude = (output_target,) if output_target is not None else ()
        manifest_before: dict[str, dict[str, Any]] = {}
        if project_root is not None:
            manifest_before = _manifest(project_root, all_files=is_dir, exclude=exclude)

        backup: dict[str, Any] = {"created": False, "reason": "no mutation authorised for this run"}
        if mutation_authorised:
            backup = _make_backup(cmd, input_path, is_dir, manifest_before)

        created_output_dir = False
        if out_kind == "dir" and not os.path.lexists(output_target):
            output_target.mkdir(exist_ok=False)
            created_output_dir = True
        snapshot_before, snapshot_truncated = ({}, False)
        if out_root is not None:
            snapshot_before, snapshot_truncated = _snapshot_tree(out_root)

        argv = [str(cli_path), *cmd.argv_prefix, *tokens]
        if output_target is not None:
            argv += ["--output", str(output_target)]
        if input_path is not None:
            argv.append(str(input_path))
        cwd = str(out_root) if out_root is not None else tempfile.gettempdir()
        timeout = None if timeout_seconds is None else min(max(float(timeout_seconds), 1.0), MAX_TIMEOUT_SECONDS)

        evidence = evidence_root / "evidence"
        exit_code: int | None = None
        timed_out = False
        runner_error: str | None = None
        try:
            completed = secure_subprocess.run_contained(
                argv, cwd=cwd, timeout=timeout, text=True, encoding="utf-8", errors="replace",
                evidence_dir=evidence, evidence_context={"tool": "kicad_cli_run", "command": cmd.name})
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except Exception as exc:  # launch or cleanup failure: report, and still diff the files below
            runner_error = f"{type(exc).__name__}: {str(exc)[:300]}"

        stdout = _read_stream(evidence / "stdout.bin")
        stderr = _read_stream(evidence / "stderr.bin")

        changed_project: list[dict[str, Any]] = []
        kicad_side_effects: list[dict[str, Any]] = []
        if project_root is not None:
            manifest_after = _manifest(project_root, all_files=is_dir, exclude=exclude)
            changed_project = _diff_manifests(manifest_before, manifest_after)
            if not mutation_authorised and not is_dir:
                # KiCad 10.0.5 creates <stem>.kicad_prl (project-local UI state) when it is absent.
                # Only that exact added file, named after the input's own stem and sitting directly in
                # the input's directory, is reported separately; it is never deleted. A .kicad_prl
                # for any other stem (even beside a matching .kicad_pro) stays a failure.
                allowed_name = f"{input_path.stem.lower()}.kicad_prl"
                remaining = []
                for change in changed_project:
                    entry = manifest_after.get(change["path"], {})
                    if (change["change"] == "added"
                            and _norm(Path(change["path"]).parent) == _norm(project_root)
                            and Path(change["path"]).name.lower() == allowed_name
                            and entry.get("sha256") and not entry.get("error")):
                        kicad_side_effects.append({
                            "path": change["path"], "sha256_after": entry["sha256"],
                            "reason": "KiCad 10.0.5 creates project-local settings on load"})
                    else:
                        remaining.append(change)
                changed_project = remaining
        outputs: dict[str, Any] = {"files": [], "total": 0, "truncated": False,
                                   "removed": [], "removed_total": 0}
        if out_root is not None:
            snapshot_after, after_truncated = _snapshot_tree(out_root)
            snapshot_truncated = snapshot_truncated or after_truncated
            outputs = _diff_snapshots(snapshot_before, snapshot_after, output_target)

        backed_up = {i["source"] for i in backup.get("items", [])}
        unbacked = [c["path"] for c in changed_project
                    if mutation_authorised and not is_dir and c["change"] in ("modified", "removed")
                    and c["path"] not in backed_up and not Path(c["path"]).name.startswith("~")]

        error: dict[str, Any] | None = None
        if runner_error is not None:
            error = {"code": "runner_error", "message": runner_error}
        elif timed_out:
            error = {"code": "timeout", "message": f"kicad-cli did not finish within {timeout:g}s and "
                     "its owned process tree was stopped"}
        elif changed_project and not mutation_authorised:
            error = {"code": "unexpected_project_change",
                     "message": f"{len(changed_project)} project file(s) changed during a run that was "
                     "not authorised to modify them. No backup exists for this run; the before/after "
                     "hashes are in changed_project_files."}
        elif unbacked:
            error = {"code": "changed_without_backup",
                     "message": f"{len(unbacked)} file(s) changed during an authorised mutation with "
                     f"no backup: {unbacked[:5]}"}
        elif exit_code != 0:
            error = {"code": "nonzero_exit",
                     "message": f"kicad-cli exited with code {exit_code}; stderr: "
                     f"{stderr['text'][-1000:]!r}; stdout: {stdout['text'][-500:]!r}"}

        keep_evidence = bool(error) or stdout["truncated"] or stderr["truncated"]

        return {
            "ok": error is None,
            "status": "completed" if error is None else (
                "timed_out" if timed_out else "failed"),
            "command": cmd.name,
            "run_status": status,
            "run_status_reason": status_reason,
            "kicad_release": KICAD_CLI_RELEASE,
            "argv": argv,
            "cwd": cwd,
            "timeout_seconds": timeout,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "error": error,
            "stdout": stdout["text"],
            "stdout_bytes": stdout["bytes"],
            "stdout_sha256": stdout["sha256"],
            "stdout_truncated": stdout["truncated"],
            "stderr": stderr["text"],
            "stderr_bytes": stderr["bytes"],
            "stderr_sha256": stderr["sha256"],
            "stderr_truncated": stderr["truncated"],
            "evidence_dir": str(evidence) if keep_evidence else None,
            "mutation_authorised": mutation_authorised,
            "backup": backup,
            "input_path": str(input_path) if input_path is not None else None,
            "project_root": str(project_root) if project_root is not None else None,
            "project_files_hashed": len(manifest_before),
            "changed_project_files": changed_project[:MAX_OUTPUT_FILES],
            "changed_project_files_total": len(changed_project),
            "kicad_side_effects": kicad_side_effects,
            "output_path": str(output_target) if output_target is not None else None,
            "output_root": str(out_root) if out_root is not None else None,
            "output_dir_created": created_output_dir,
            "output_files": outputs["files"],
            "output_files_total": outputs["total"],
            "output_files_truncated": outputs["truncated"],
            "output_files_removed": outputs["removed"],
            "output_files_removed_total": outputs["removed_total"],
            "output_snapshot_truncated": snapshot_truncated,
        }


    # --- MCP registration --------------------------------------------------------------------------
    finally:
        if not keep_evidence:
            shutil.rmtree(evidence_root)


def register_cli_tools(mcp: FastMCP) -> None:
    # covers: L1-1
    @mcp.tool()
    def kicad_cli_commands() -> dict[str, Any]:
        """The full typed kicad-cli 10.0.5 registry: 43 leaf commands, options, choices, run status."""
        commands = [_command_dict(cmd) for cmd in cli_registry.list_commands()]
        return {**command_coverage(), "commands": commands}

    @mcp.tool()
    def kicad_cli_describe(command: str) -> dict[str, Any]:
        """Describe one kicad-cli leaf command (for example "pcb export svg") with option roles."""
        try:
            return {"ok": True, **_command_dict(cli_registry.get_command(command))}
        except CliRegistryError as exc:
            return _refused(command, exc.code, str(exc), exc.option, source="cli_registry")

    @mcp.tool()
    def kicad_cli_run(command: str, input_path: str | None = None,
                      options: dict[str, str | bool | int | float | list] | None = None,
                      output_path: str | None = None, allow_mutation: bool = False
                      ) -> dict[str, Any]:
        """Run one validated kicad-cli leaf command; never free argv.

        ``options`` are validated against the registry (type, choices, unknown/duplicate refused);
        ``--output`` is reserved and comes only from the absolute, not-yet-existing ``output_path``.
        Mutating commands (upgrades, pcb import, pcb drc --save-board) need ``allow_mutation=True``
        and first make a sha256-verified ``<name>.bak-<utc>`` backup next to the input. Output is
        bounded; produced files are listed with sha256. ``jobset run`` and ``pcb export hpgl`` are
        refused; ``pcb render`` is callable but unverified.
        """
        return run_cli(command, input_path, options, output_path, allow_mutation)
