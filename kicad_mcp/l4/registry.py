"""Closed, versioned L4 UI registry: schema, load, sha256 approval gate and typed value validation.

Pure Python with no Windows dependency. Callers of the broker supply only an operation name and typed
values; every dialog, menu and control identifier comes from the registry file, which is accepted
only if its sha256 is in the owner-approved list (env ``KICAD_MCP_L4_APPROVED_REGISTRY_SHA256``,
comma-separated) and its ``kicad_build`` equals the running KiCad build. Anything unknown (operation,
parameter, parameter type, step action, schema key) is rejected before any input is sent.

Registry document shape (schema_version 1)::

    {"schema_version": 1, "kicad_build": "10.0.5",
     "main_window": {"name": "PCB Editor", "match": "contains"},
     "ambient_windows": [{"name": ..., "match": "exact"}],           # windows that may stay open
     "known_dialogs": {id: {"window": {...}, "safe_to_cancel": bool, "cancel": {...}|null}},
     "operations": {name: {"description", "application", "api_alternative",
                           "params": {pname: {"type": "enum"|"number"|"clone_path", ...}},
                           "dialogs": {id: {"window", "safe_to_cancel", "cancel"}},
                           "controls": {ref: {"dialog": id|"__main__", "by": "path"|"automation_id", ...}},
                           "steps": [{"action": ...}], "postcondition": {"type": ...}}},
     "provenance": {...}}                                              # free-form, never interpreted
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("registry") / "ui-registry-10.0.5.json"
APPROVAL_ENV = "KICAD_MCP_L4_APPROVED_REGISTRY_SHA256"
SCHEMA_VERSION = 1
MAIN_DIALOG = "__main__"

CONTROL_TYPES = ("AppBar", "Button", "Calendar", "CheckBox", "ComboBox", "Custom", "DataGrid", "DataItem",
                 "Document", "Edit", "Group", "Header", "HeaderItem", "Hyperlink", "Image", "List",
                 "ListItem", "Menu", "MenuBar", "MenuItem", "Pane", "ProgressBar", "RadioButton",
                 "ScrollBar", "SemanticZoom", "Separator", "Slider", "Spinner", "SplitButton",
                 "StatusBar", "Tab", "TabItem", "Table", "Text", "Thumb", "TitleBar", "ToolBar",
                 "ToolTip", "Tree", "TreeItem", "Window")
MATCH_MODES = ("exact", "prefix", "contains")
STEP_ACTIONS = ("select_menu", "wait_dialog", "set_value", "invoke", "read_value", "wait_dialog_closed")
POSTCONDITION_TYPES = ("file_rewritten", "files_exist", "file_unchanged")
PARAM_TYPES = ("enum", "number", "clone_path")
APPLICATIONS = ("pcbnew",)

OP_NAME = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SAFE_GLOB = re.compile(r"^[A-Za-z0-9_.*?-]{1,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}


class RegistryRefusal(Exception):
    """A registry, operation or parameter was refused; nothing was sent to any UI."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_error(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


# ---- approval and loading ---------------------------------------------------------------------

def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


RELEASE_APPROVALS = "APPROVED.sha256"  # sha256sum format, next to the registry files


def release_approved_sha256s(registry_path: str | os.PathLike | None) -> set[str]:
    """Hashes the package release approves for exactly this registry file name (``<sha>  <file name>``
    lines in APPROVED.sha256 in the same directory). A byte change to the registry no longer matches."""
    if registry_path is None:
        return set()
    target = Path(registry_path)
    try:
        lines = (target.parent / RELEASE_APPROVALS).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and SHA256.match(parts[0].lower()) and parts[1].lstrip("*") == target.name:
            out.add(parts[0].lower())
    return out


def approved_sha256s(env: Mapping[str, str] | None = None, registry_path=None) -> set[str]:
    """Approved registry hashes: the release approvals shipped beside ``registry_path`` plus any in the
    environment (comma-separated; malformed tokens approve nothing)."""
    env = os.environ if env is None else env
    tokens = (token.strip().lower() for token in (env.get(APPROVAL_ENV) or "").split(","))
    return {token for token in tokens if SHA256.match(token)} | release_approved_sha256s(registry_path)


def norm_name(text: str | None) -> str:
    """Same normalisation as the UIA helper: ellipsis, ampersands and a tab-shortcut are dropped."""
    value = (text or "").replace("…", "...").split("\t", 1)[0]
    return value.replace("&", "").strip()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryRefusal("registry_invalid", f"duplicate key {key!r} in registry JSON")
        result[key] = value
    return result


def parse_registry(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except RegistryRefusal:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryRefusal("registry_invalid", f"registry is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryRefusal("registry_invalid", "registry root must be an object")
    return data


@dataclass(frozen=True)
class Registry:
    data: dict
    sha256: str
    path: str | None = None

    @property
    def kicad_build(self) -> str:
        return self.data["kicad_build"]

    @property
    def main_window(self) -> dict:
        return self.data["main_window"]

    @property
    def ambient_windows(self) -> list[dict]:
        return self.data["ambient_windows"]

    @property
    def known_dialogs(self) -> dict:
        return self.data["known_dialogs"]

    def operation_names(self) -> list[str]:
        return sorted(self.data["operations"])

    def operation(self, name: Any) -> dict:
        operations = self.data["operations"]
        if not isinstance(name, str) or name not in operations:
            raise RegistryRefusal("unknown_operation", f"operation {name!r} is not in the registry",
                                  available=sorted(operations))
        return operations[name]


def load_registry(path: str | os.PathLike | None = None, env: Mapping[str, str] | None = None, *,
                  kicad_build: str | None = None, skip_build_check: bool = False) -> Registry:
    """Read, approve, validate and version-check a registry. Any failure is a :class:`RegistryRefusal`.

    The sha256 is computed over the exact file bytes. Approval is checked first, so an unapproved
    file is refused as a whole whatever it contains.
    """
    target = Path(path) if path else DEFAULT_REGISTRY_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise RegistryRefusal("registry_unreadable", f"cannot read registry {target}: {exc}") from exc
    digest = sha256_bytes(raw)
    if digest not in approved_sha256s(env, target):
        raise RegistryRefusal("registry_unapproved",
                              f"registry sha256 {digest} is approved neither by {RELEASE_APPROVALS} nor by "
                              f"{APPROVAL_ENV}; all operations are refused",
                              sha256=digest)
    data = parse_registry(raw)
    validate_registry(data)
    registry = Registry(data=data, sha256=digest, path=str(target))
    if not skip_build_check:
        check_build(registry, kicad_build)
    return registry


def check_build(registry: Registry, kicad_build: str | None) -> None:
    """Refuse everything unless the registry's KiCad build equals the running build exactly."""
    if not kicad_build:
        raise RegistryRefusal("kicad_build_unknown", "the running KiCad build is unknown; refusing all operations")
    if registry.kicad_build != kicad_build:
        raise RegistryRefusal("registry_version_mismatch",
                              f"registry is for KiCad {registry.kicad_build}, running build is {kicad_build}",
                              registry_build=registry.kicad_build, running_build=kicad_build)


def inspect_registry_file(path: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> dict:
    """Approval-independent facts about a registry file, for reporting to the owner."""
    target = Path(path) if path else DEFAULT_REGISTRY_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {"path": str(target), "readable": False, "error": str(exc)}
    digest = sha256_bytes(raw)
    info: dict[str, Any] = {"path": str(target), "readable": True, "sha256": digest, "size": len(raw),
                            "approved": digest in approved_sha256s(env, target), "approval_env": APPROVAL_ENV,
                            "release_approved": digest in release_approved_sha256s(target)}
    try:
        data = parse_registry(raw)
        validate_registry(data)
        info.update(schema_valid=True, kicad_build=data["kicad_build"], operations=sorted(data["operations"]))
    except RegistryRefusal as exc:
        info.update(schema_valid=False, schema_error=str(exc))
    return info


# ---- schema validation (closed: unknown keys are errors) ---------------------------------------

def _fail(path: str, message: str):
    raise RegistryRefusal("registry_invalid", f"{path}: {message}")


def _object(value, path, required=(), optional=()):
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    unknown = set(value) - set(required) - set(optional)
    if unknown:
        _fail(path, f"unknown keys {sorted(unknown)}")
    missing = [key for key in required if key not in value]
    if missing:
        _fail(path, f"missing keys {missing}")
    return value


def _map(value, path):
    """An object whose keys are names (validated by the caller), not a closed key set."""
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _text(value, path, *, allow_empty=False, limit=200):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit \
            or any(ord(ch) < 32 for ch in value):
        _fail(path, "must be a short printable string")
    return value


def _number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(path, "must be a finite number")
    return value


def _window_match(value, path):
    _object(value, path, required=("name", "match"))
    _text(value["name"], f"{path}.name")
    if value["match"] not in MATCH_MODES:
        _fail(f"{path}.match", f"must be one of {MATCH_MODES}")


def _cancel(value, path):
    """``{"name", "control_type"}`` or ``{"automation_id", "control_type"}`` (a stable id, e.g. wxID_CANCEL)."""
    if isinstance(value, dict) and "automation_id" in value:
        _object(value, path, required=("automation_id", "control_type"))
        _text(value["automation_id"], f"{path}.automation_id", limit=80)
        if not stable_automation_id(value["automation_id"]):
            _fail(f"{path}.automation_id", "must be a stable automation id")
    else:
        _object(value, path, required=("name", "control_type"))
        _text(value["name"], f"{path}.name")
    if value["control_type"] not in CONTROL_TYPES:
        _fail(f"{path}.control_type", "unknown control type")


def cancel_selector(cancel: dict) -> dict:
    """The helper selector for a dialog's registered cancel control."""
    if "automation_id" in cancel:
        return {"by": "automation_id", "automation_id": cancel["automation_id"], "control_type": cancel["control_type"]}
    return {"by": "path", "name": cancel["name"], "name_match": "exact", "control_type": cancel["control_type"],
            "ancestry": []}


def _dialog(value, path):
    _object(value, path, required=("window", "safe_to_cancel", "cancel"))
    _window_match(value["window"], f"{path}.window")
    if not isinstance(value["safe_to_cancel"], bool):
        _fail(f"{path}.safe_to_cancel", "must be a boolean")
    if value["cancel"] is not None:
        _cancel(value["cancel"], f"{path}.cancel")
    if value["safe_to_cancel"] and value["cancel"] is None:
        _fail(path, "a dialog marked safe_to_cancel needs a cancel control")


def _selector(value, path, dialog_ids):
    _object(value, path, required=("dialog", "by", "control_type"),
            optional=("automation_id", "name", "name_match", "ancestry"))
    if value["dialog"] != MAIN_DIALOG and value["dialog"] not in dialog_ids:
        _fail(f"{path}.dialog", f"unknown dialog {value['dialog']!r}")
    if value["control_type"] not in CONTROL_TYPES:
        _fail(f"{path}.control_type", "unknown control type")
    if value["by"] == "automation_id":
        _text(value.get("automation_id"), f"{path}.automation_id")
        if not stable_automation_id(value["automation_id"]):
            _fail(f"{path}.automation_id", "must be a stable automation id (symbolic or a wx stock id)")
    elif value["by"] == "path":
        _text(value.get("name"), f"{path}.name")
        if value.get("name_match", "exact") not in ("exact", "prefix"):
            _fail(f"{path}.name_match", "must be exact or prefix")
    else:
        _fail(f"{path}.by", "must be automation_id or path")
    ancestry = value.get("ancestry", [])
    if not isinstance(ancestry, list) or len(ancestry) > 4:
        _fail(f"{path}.ancestry", "must be a list of at most 4 entries")
    for index, item in enumerate(ancestry):
        _object(item, f"{path}.ancestry[{index}]", required=("name", "control_type"))
        _text(item["name"], f"{path}.ancestry[{index}].name", allow_empty=True)
        if item["control_type"] not in CONTROL_TYPES:
            _fail(f"{path}.ancestry[{index}].control_type", "unknown control type")


def _param(value, path):
    if not isinstance(value, dict) or value.get("type") not in PARAM_TYPES:
        _fail(path, f"type must be one of {PARAM_TYPES}")
    kind = value["type"]
    common = ("type", "required", "description")
    if "required" in value and not isinstance(value["required"], bool):
        _fail(f"{path}.required", "must be a boolean")
    if "description" in value:
        _text(value["description"], f"{path}.description", limit=300)
    if kind == "enum":
        _object(value, path, required=("type", "values"), optional=common[1:])
        values = value["values"]
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            _fail(f"{path}.values", "must be a non-empty list of unique strings")
        for index, item in enumerate(values):
            _text(item, f"{path}.values[{index}]")
    elif kind == "number":
        _object(value, path, required=("type", "min", "max", "unit"), optional=(*common[1:], "integer"))
        low, high = _number(value["min"], f"{path}.min"), _number(value["max"], f"{path}.max")
        if low > high:
            _fail(path, "min must not exceed max")
        _text(value["unit"], f"{path}.unit", limit=20)
        if "integer" in value and not isinstance(value["integer"], bool):
            _fail(f"{path}.integer", "must be a boolean")
    else:
        _object(value, path, required=("type", "kind"), optional=(*common[1:], "must_exist"))
        if value["kind"] not in ("dir", "file"):
            _fail(f"{path}.kind", "must be dir or file")
        if "must_exist" in value and not isinstance(value["must_exist"], bool):
            _fail(f"{path}.must_exist", "must be a boolean")


def _step(step, path, params, controls, dialogs):
    if not isinstance(step, dict) or step.get("action") not in STEP_ACTIONS:
        _fail(path, f"action must be one of {STEP_ACTIONS}")
    action = step["action"]
    if action == "select_menu":
        _object(step, path, required=("action", "path"))
        menu = step["path"]
        if not isinstance(menu, list) or not 1 <= len(menu) <= 6:
            _fail(f"{path}.path", "must be a list of 1-6 menu names")
        for index, item in enumerate(menu):
            _text(item, f"{path}.path[{index}]")
    elif action in ("wait_dialog", "wait_dialog_closed"):
        _object(step, path, required=("action", "dialog", "timeout_s"))
        if step["dialog"] not in dialogs:
            _fail(f"{path}.dialog", f"unknown dialog {step['dialog']!r}")
        if not 1 <= _number(step["timeout_s"], f"{path}.timeout_s") <= 120:
            _fail(f"{path}.timeout_s", "must be within 1..120")
    elif action == "set_value":
        _object(step, path, required=("action", "control", "param"), optional=("verify",))
        if step["control"] not in controls:
            _fail(f"{path}.control", f"unknown control {step['control']!r}")
        if step["param"] not in params:
            _fail(f"{path}.param", f"unknown parameter {step['param']!r}")
        if "verify" in step and not isinstance(step["verify"], bool):
            _fail(f"{path}.verify", "must be a boolean")
    elif action == "invoke":
        _object(step, path, required=("action", "control"))
        if step["control"] not in controls:
            _fail(f"{path}.control", f"unknown control {step['control']!r}")
    else:  # read_value
        _object(step, path, required=("action", "control", "output"))
        if step["control"] not in controls:
            _fail(f"{path}.control", f"unknown control {step['control']!r}")
        if not isinstance(step["output"], str) or not IDENT.match(step["output"]):
            _fail(f"{path}.output", "must be an identifier")


def _postcondition(post, path, params, outputs):
    if not isinstance(post, dict) or post.get("type") not in POSTCONDITION_TYPES:
        _fail(path, f"type must be one of {POSTCONDITION_TYPES}")
    kind = post["type"]
    if kind == "file_rewritten":
        _object(post, path, required=("type", "file", "loads_with_kicad_cli"))
        if post["file"] != "board" or not isinstance(post["loads_with_kicad_cli"], bool):
            _fail(path, "file must be 'board' and loads_with_kicad_cli a boolean")
    elif kind == "files_exist":
        _object(post, path, required=("type", "dir_param", "glob", "min_count"))
        spec = params.get(post["dir_param"])
        if not spec or spec.get("type") != "clone_path" or spec.get("kind") != "dir":
            _fail(f"{path}.dir_param", "must name a clone_path directory parameter")
        if not isinstance(post["glob"], str) or not SAFE_GLOB.match(post["glob"]):
            _fail(f"{path}.glob", "must be a simple file pattern")
        if isinstance(post["min_count"], bool) or not isinstance(post["min_count"], int) \
                or not 1 <= post["min_count"] <= 1000:
            _fail(f"{path}.min_count", "must be an integer within 1..1000")
    else:
        _object(post, path, required=("type", "file", "required_outputs"))
        if post["file"] != "board":
            _fail(f"{path}.file", "must be 'board'")
        required = post["required_outputs"]
        if not isinstance(required, list) or not set(required) <= set(outputs):
            _fail(f"{path}.required_outputs", "must be a list of read_value outputs of this operation")


def _operation(op, path):
    _object(op, path, required=("description", "application", "api_alternative", "params", "dialogs",
                                "controls", "steps", "postcondition"))
    _text(op["description"], f"{path}.description", limit=400)
    _text(op["api_alternative"], f"{path}.api_alternative", limit=400)
    if op["application"] not in APPLICATIONS:
        _fail(f"{path}.application", f"must be one of {APPLICATIONS}")
    params = _map(op["params"], f"{path}.params")
    for name, spec in params.items():
        if not IDENT.match(name):
            _fail(f"{path}.params", f"bad parameter name {name!r}")
        _param(spec, f"{path}.params.{name}")
    dialogs = _map(op["dialogs"], f"{path}.dialogs")
    for name, dialog in dialogs.items():
        if not IDENT.match(name):
            _fail(f"{path}.dialogs", f"bad dialog id {name!r}")
        _dialog(dialog, f"{path}.dialogs.{name}")
    controls = _map(op["controls"], f"{path}.controls")
    for name, selector in controls.items():
        if not IDENT.match(name):
            _fail(f"{path}.controls", f"bad control reference {name!r}")
        _selector(selector, f"{path}.controls.{name}", set(dialogs))
    steps = op["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        _fail(f"{path}.steps", "must be a list of 1-30 steps")
    outputs = []
    for index, step in enumerate(steps):
        _step(step, f"{path}.steps[{index}]", params, controls, dialogs)
        if step["action"] == "read_value":
            outputs.append(step["output"])
    _postcondition(op["postcondition"], f"{path}.postcondition", params, outputs)


def validate_registry(data: Any) -> None:
    """Closed schema check; raises :class:`RegistryRefusal` (code ``registry_invalid``) with the failing path."""
    try:
        _validate_registry(data)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:  # malformed shapes must never crash a caller
        raise RegistryRefusal("registry_invalid", f"registry has an unexpected shape: {type(exc).__name__}: {exc}") from exc


def _validate_registry(data: Any) -> None:
    _object(data, "registry", required=("schema_version", "kicad_build", "main_window", "ambient_windows",
                                        "known_dialogs", "operations"), optional=("provenance",))
    if data["schema_version"] != SCHEMA_VERSION or isinstance(data["schema_version"], bool):
        _fail("registry.schema_version", f"must be {SCHEMA_VERSION}")
    _text(data["kicad_build"], "registry.kicad_build", limit=40)
    _window_match(data["main_window"], "registry.main_window")
    if not isinstance(data["ambient_windows"], list):
        _fail("registry.ambient_windows", "must be a list")
    for index, window in enumerate(data["ambient_windows"]):
        _window_match(window, f"registry.ambient_windows[{index}]")
    known = _map(data["known_dialogs"], "registry.known_dialogs")
    for name, dialog in known.items():
        if not IDENT.match(name):
            _fail("registry.known_dialogs", f"bad dialog id {name!r}")
        _dialog(dialog, f"registry.known_dialogs.{name}")
    operations = _map(data["operations"], "registry.operations")
    if not operations:
        _fail("registry.operations", "must not be empty")
    for name, op in operations.items():
        if not OP_NAME.match(name):
            _fail("registry.operations", f"bad operation name {name!r}")
        _operation(op, f"registry.operations.{name}")


# ---- typed value validation -------------------------------------------------------------------

def resolve_clone_relative(clone_dir: str | os.PathLike, value: Any, *, kind: str = "dir",
                           must_exist: bool = False) -> str:
    """Resolve a clone-relative path, rejecting every escape. Returns the absolute path inside the clone."""
    if not isinstance(value, str):
        raise RegistryRefusal("invalid_parameter_type", "a path parameter must be a string")
    if not value or len(value) > 200 or any(ord(ch) < 32 for ch in value) or any(ch in '<>"|?*' for ch in value):
        raise RegistryRefusal("path_invalid", f"path {value!r} is empty, too long or has forbidden characters")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise RegistryRefusal("path_not_relative", f"path {value!r} must be relative to the project clone")
    if ":" in value:
        raise RegistryRefusal("path_invalid", f"path {value!r} contains ':' (drive or stream syntax)")
    parts = [part for part in re.split(r"[\\/]+", value) if part != ""]
    if not parts:
        raise RegistryRefusal("path_invalid", "path names nothing")
    for part in parts:
        if part == "..":
            raise RegistryRefusal("path_escape", f"path {value!r} uses '..'")
        if part == "." or part != part.rstrip(" .") or part.split(".")[0].upper() in WINDOWS_DEVICE_NAMES:
            raise RegistryRefusal("path_invalid", f"path segment {part!r} is not allowed")
    base = Path(clone_dir).resolve()
    target = (base / Path(*parts)).resolve()
    if target == base or base not in target.parents:
        raise RegistryRefusal("path_escape", f"path {value!r} resolves outside the project clone")
    if must_exist and not (target.is_dir() if kind == "dir" else target.is_file()):
        raise RegistryRefusal("path_not_found", f"path {value!r} does not exist in the clone")
    return str(target)


def validate_params(operation: dict, params: Any, clone_dir: str | os.PathLike) -> dict[str, Any]:
    """Typed validation of caller-supplied values against the operation's parameter schema.

    Unknown parameters, missing required ones, wrong types, out-of-range numbers, enum values not
    listed and clone-relative path escapes are all refused. Returns validated values; clone paths are
    returned as absolute paths inside ``clone_dir``.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict) or any(not isinstance(key, str) for key in params):
        raise RegistryRefusal("invalid_parameters", "params must be an object with string keys")
    schema = operation["params"]
    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise RegistryRefusal("unknown_parameter", f"unknown parameters {unknown}", allowed=sorted(schema))
    validated: dict[str, Any] = {}
    for name, spec in schema.items():
        if name not in params:
            if spec.get("required", True):
                raise RegistryRefusal("missing_parameter", f"parameter {name!r} is required")
            continue
        value = params[name]
        kind = spec["type"]
        if kind == "enum":
            if not isinstance(value, str) or value not in spec["values"]:
                raise RegistryRefusal("invalid_parameter_value", f"{name!r} must be one of {spec['values']}")
            validated[name] = value
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise RegistryRefusal("invalid_parameter_type", f"{name!r} must be a finite number")
            if spec.get("integer") and not (isinstance(value, int) or float(value).is_integer()):
                raise RegistryRefusal("invalid_parameter_type", f"{name!r} must be an integer")
            if not spec["min"] <= value <= spec["max"]:
                raise RegistryRefusal("parameter_out_of_range",
                                      f"{name!r} must be within {spec['min']}..{spec['max']} {spec['unit']}")
            validated[name] = int(value) if spec.get("integer") else value
        else:
            validated[name] = resolve_clone_relative(clone_dir, value, kind=spec["kind"],
                                                     must_exist=spec.get("must_exist", False))
    return validated


def describe_operations(registry: Registry) -> list[dict]:
    """Operation summaries safe to show a caller (no identifiers)."""
    out = []
    for name in registry.operation_names():
        op = registry.operation(name)
        out.append({"name": name, "description": op["description"], "application": op["application"],
                    "api_alternative": op["api_alternative"], "params": op["params"],
                    "dialogs": {key: {"safe_to_cancel": value["safe_to_cancel"]}
                                for key, value in op["dialogs"].items()},
                    "postcondition": op["postcondition"]["type"]})
    return out


# ---- identifier stability (pure; used by record_identifiers) ------------------------------------

WX_STOCK_ID_RANGE = (5000, 5999)  # wxID_LOWEST..wxID_HIGHEST: fixed constants (wxID_OK=5100, wxID_CANCEL=5101)


_SYMBOLIC_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,79}")
_STOCK_ID = re.compile(r"[0-9]{4}")


def stable_automation_id(automation_id: Any) -> bool:
    """Exactly two forms are stable: a symbolic id (starts with a letter or underscore, then letters,
    digits, ``_ . : -``) or a plain 4-digit wx stock id inside WX_STOCK_ID_RANGE (wxID_OK=5100,
    wxID_CANCEL=5101 ...). Everything else - negative or other numbers (wxID_ANY allocations, window
    counters), signs, decimals, padding, empty or non-string values - is treated as dynamic."""
    if not isinstance(automation_id, str):
        return False
    if _STOCK_ID.fullmatch(automation_id):
        return WX_STOCK_ID_RANGE[0] <= int(automation_id) <= WX_STOCK_ID_RANGE[1]
    return _SYMBOLIC_ID.fullmatch(automation_id) is not None


def choose_selector(observations: list[dict | None], launches: int = 3) -> tuple[dict | None, str]:
    """Pick a selector only if the same control resolves identically in every one of ``launches`` launches.

    Each observation is ``{"name", "control_type", "automation_id", "ancestry": [{"name","control_type"}],
    "match_count", "automation_id_matches"}`` or None when nothing resolved. A name + control type +
    ancestry-suffix selector is preferred; an AutomationId is accepted only when it is stable (see
    :func:`stable_automation_id`) and unique and equal in every launch.
    Returns ``(selector without 'dialog', reason)``; selector is None when the identifier is dropped.
    """
    if len(observations) != launches or any(o is None for o in observations):
        return None, "did_not_resolve_in_every_launch"
    first = observations[0]

    def path_key(o):
        return (norm_name(o["name"]), o["control_type"],
                tuple((norm_name(a["name"]), a["control_type"]) for a in o.get("ancestry", [])[-4:]))

    if all(o.get("match_count") == 1 for o in observations) and norm_name(first["name"]) \
            and all(path_key(o) == path_key(first) for o in observations):
        return ({"by": "path", "name": norm_name(first["name"]), "name_match": "exact",
                 "control_type": first["control_type"],
                 "ancestry": [{"name": norm_name(a["name"]), "control_type": a["control_type"]}
                              for a in first.get("ancestry", [])[-4:]]}, "stable_name_path")
    ids = {o.get("automation_id") for o in observations}
    only = next(iter(ids)) if len(ids) == 1 else None
    if only and stable_automation_id(only) and all(o.get("automation_id_matches") == 1 for o in observations):
        return ({"by": "automation_id", "automation_id": only, "control_type": first["control_type"]},
                "stable_automation_id")
    return None, "name_path_or_automation_id_not_stable"


def choose_menu_path(observations: list[list[str] | None], launches: int = 3) -> tuple[list[str] | None, str]:
    """A menu path is admitted if every level resolved to the same normalised name in every launch."""
    if len(observations) != launches or any(not o for o in observations):
        return None, "did_not_resolve_in_every_launch"
    first = [norm_name(n) for n in observations[0]]
    if not all(first) or any([norm_name(n) for n in o] != first for o in observations):
        return None, "menu_names_differ_between_launches"
    return first, "stable_menu_path"
