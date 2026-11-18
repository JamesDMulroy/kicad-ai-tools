"""Thin, fail-closed adapter to the maintained shared BoardTooling source.

This is a cooperative same-user workflow, not an operating-system boundary.
Geometry, gate definitions, bundle identity and release validation live upstream.
"""
from __future__ import annotations

import hashlib
import asyncio
from importlib import metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from kicad_mcp.utils.secure_subprocess import run_contained

from kicad_mcp.adaptive_editor import kicad_env

BOARDTOOLING_ENV = "KICAD_AI_TOOLS_BOARDTOOLING_ROOT"


def _unconfigured(what: str, how: str) -> Path:
    """A path that never exists and says what is missing, so every availability check reports it."""
    if CONFIG_ERROR:
        return Path(f"<{what} unavailable: the per-machine config is invalid ({CONFIG_ERROR})>")
    return Path(f"<{what} not configured: {how}>")


CONFIG_ERROR = ""  # set when the per-machine config cannot be read; every placeholder then names it


def _setting(key: str, env: str):
    """A setting that never breaks this import. A malformed per-machine config is logged and reported in
    every 'not configured' message (it is the actual cause), never silently treated as unset."""
    global CONFIG_ERROR
    try:
        return kicad_env.setting(key, env)
    except kicad_env.ConfigError as exc:
        import logging
        CONFIG_ERROR = str(exc)
        logging.getLogger(__name__).warning("%s", exc)
        return None


def _kicad_bin() -> Path:
    try:
        return kicad_env.kicad_cli().parent
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        return _unconfigured("KiCad", str(exc))


def _kicad_cli() -> Path:
    try:
        return kicad_env.kicad_cli()
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        return _unconfigured("kicad-cli", str(exc))


def _kicad_site_packages() -> Path:
    try:
        return kicad_env.kicad_site_packages() or _unconfigured("KiCad site-packages", "unknown on this platform")
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        return _unconfigured("KiCad site-packages", str(exc))


def _kicad_python() -> Path:
    try:
        return kicad_env.kicad_python() or _unconfigured("KiCad python", "this platform has no bundled KiCad python")
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        return _unconfigured("KiCad python", str(exc))


# BoardTooling (contract/gate/release workflow) is an optional, separately installed tool. It is
# enabled by setting its root (environment variable or per-machine config); never assumed.
_bt = _setting("boardtooling_root", BOARDTOOLING_ENV)
ROOT = Path(_bt) if _bt else _unconfigured("BoardTooling", f"set {BOARDTOOLING_ENV} or run python -m kicad_mcp.setup")
SCRIPTS = ROOT / "scripts"
KICAD_BIN = _kicad_bin()
EXPORT_PYTHON = Path(_setting("export_python", "KICAD_AI_TOOLS_EXPORT_PYTHON") or sys.executable)
_pwsh = _setting("powershell", "KICAD_AI_TOOLS_POWERSHELL") or shutil.which("pwsh")
POWERSHELL = Path(_pwsh) if _pwsh else _unconfigured("PowerShell 7", "install pwsh or set KICAD_AI_TOOLS_POWERSHELL")
REVIEW_KEY_ENV = "BOARDTOOLING_VISUAL_REVIEW_PUBLIC_KEY"
TRUST_CONFIG_ENV = "BOARDTOOLING_TRUST_CONFIG"


class BoardToolingError(ValueError):
    pass


def _module(name: str, path: Path):
    if not path.is_file():
        raise BoardToolingError(f"Required shared module unavailable: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _bundle():
    return _module("_shared_board_bundle", SCRIPTS / "board_bundle.py")


def _exporter():
    # Exporter imports its sibling board_bundle. It owns the release predicates.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return _module("_shared_export_release", SCRIPTS / "export_release.py")


def _contract():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return _module("_shared_design_contract", SCRIPTS / "design_contract.py")


def _review_key() -> tuple[Path, str]:
    configured = _setting("boardtooling_visual_review_public_key", REVIEW_KEY_ENV)
    if not configured:
        raise BoardToolingError(f"Independent visual reviewer public key is unconfigured: {REVIEW_KEY_ENV}")
    if not Path(configured).is_absolute():
        raise BoardToolingError("Configured independent reviewer key must use an absolute path")
    configured_path = Path(configured)
    if configured_path.is_symlink() or not configured_path.is_file():
        raise BoardToolingError("Configured independent visual reviewer public key is unavailable")
    path = configured_path.resolve()
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = load_pem_public_key(path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("Expected Ed25519 public key")
    except (ImportError, OSError, ValueError) as exc:
        raise BoardToolingError(f"Configured reviewer key cannot be verified: {exc}") from exc
    return path, _digest(path)


def _host_trust(project: Path, *, required: bool = False) -> tuple[Path | None, str | None]:
    configured = _setting("boardtooling_trust_config", TRUST_CONFIG_ENV)
    if not configured:
        if required:
            raise BoardToolingError(f"Independent design authority is unconfigured: {TRUST_CONFIG_ENV}")
        return None, None
    path = Path(configured)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve() != path or path.is_relative_to(project.parent)):
        raise BoardToolingError("Host design trust configuration must be a direct external file")
    return path, _digest(path)


def _evidence_selection(pro: Path, gate_report: str | None,
                        attestation_paths: list[str] | tuple[str, ...],
                        *, require_complete: bool = False) -> dict:
    if not isinstance(attestation_paths, (list, tuple)) or any(
            not isinstance(item, str) for item in attestation_paths):
        raise BoardToolingError("Explicit attestation file paths must be a typed list")
    if require_complete and (not gate_report or not attestation_paths):
        raise BoardToolingError("Release requires exact gate report and signed attestation files")
    trust_path, trust_digest = _host_trust(pro, required=require_complete)
    selected = {"trust_config_path": str(trust_path) if trust_path else None,
                "trust_config_sha256": trust_digest,
                "gate_report_path": None, "gate_report_sha256": None,
                "attestations": {}}
    def checked(value: str) -> Path:
        path = Path(value)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.resolve() != path or path.is_relative_to(pro.parent)):
            raise BoardToolingError("Evidence must be a direct external existing file: " + str(value))
        return path
    if gate_report is not None:
        gate = checked(gate_report)
        selected["gate_report_path"] = str(gate)
        selected["gate_report_sha256"] = _digest(gate)
    for value in attestation_paths:
        path = checked(value)
        if str(path) in selected["attestations"]:
            raise BoardToolingError("Duplicate attestation file: " + str(path))
        selected["attestations"][str(path)] = _digest(path)
    return selected


def _same_evidence(pro: Path, pinned: dict) -> bool:
    try:
        current = _evidence_selection(pro, pinned.get("gate_report_path"),
                                      list(pinned.get("attestations", {})),
                                      require_complete=bool(pinned.get("trust_config_path")))
    except (ValueError, OSError, TypeError):
        return False
    return current == pinned


def _release_contract(pro: Path, expected_digest: str, variant: str,
                      gate_report: str, attestation_paths: list[str]) -> tuple[dict, dict]:
    checked = evaluate_contract(str(pro), expected_digest, gate_report, attestation_paths,
                                require_evidence=True)
    result = checked["evaluation"]
    binding = result.get("binding") or {}
    if (not result.get("accepted") or binding.get("bundle_digest") != expected_digest
            or binding.get("variant") != variant):
        raise BoardToolingError("Selected valid design contract required for release: "
                                + "; ".join(result.get("requirements") or ["contract unavailable or stale"]))
    return binding, checked["evidence_selection"]


def _project(value: str) -> Path:
    path = Path(value).absolute()
    if path.suffix != ".kicad_pro" or not path.is_file() or path.is_symlink():
        raise BoardToolingError("Explicit existing .kicad_pro file required")
    return path.resolve()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identity(project: str, expected_digest: str) -> tuple[Path, dict]:
    pro = _project(project)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise BoardToolingError("Exact 64-character lowercase bundle digest required")
    bundle = _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
    if bundle["project"] != pro.name:
        raise BoardToolingError("Bundle project mismatch")
    return pro, bundle


def _same_path(left: str, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(str(right.absolute()))


def inspect_bundle(project: str) -> dict:
    pro = _project(project)
    return _bundle().snapshot(pro, require_complete=True)


def inspect_courtyards(project: str) -> dict:
    """Geometry research only; incomplete 3D bundles remain ineligible for gates."""
    pro = _project(project)
    board = pro.with_suffix('.kicad_pcb')
    before = _digest(board)
    from kicad_mcp.courtyard_research import inventory
    rows = inventory(_bundle()._sexpr(board.read_text(encoding='utf-8-sig')))
    if _digest(board) != before:
        raise BoardToolingError('Board changed during inventory')
    return dict(board_sha256=before, footprints=rows, acceptance_eligible=False,
                method='structural local courtyard graphic boxes including stroke; no save')


def design_profiles() -> dict:
    return _contract().catalog()


def create_contract_draft(project: str, expected_digest: str,
                          profile: str, variant: str) -> dict:
    pro, before = _identity(project, expected_digest)
    bundle = _bundle()
    with bundle.exclusive_writer(pro):
        bundle.verify_snapshot(pro, expected_digest, require_complete=True)
        draft = _contract().author_draft(pro, profile, variant)
        after = bundle.snapshot(pro, require_complete=True)
        if ({name: digest for name, digest in after["files"].items()
             if name != "board-contract.json"} != before["files"]):
            raise BoardToolingError("Project sources changed while authoring draft; expert review required")
    return {"state": "draft", "contract": draft, "bundle": after,
            "previous_bundle_digest": expected_digest, "accepted": False}


def evaluate_contract(project: str, expected_digest: str,
                      gate_report: str | None = None,
                      attestation_paths: list[str] | None = None,
                      *, require_evidence: bool = False) -> dict:
    pro, before = _identity(project, expected_digest)
    selected = _evidence_selection(pro, gate_report, attestation_paths or [],
                                   require_complete=require_evidence)
    result = _contract().evaluate(pro, gate_report=selected["gate_report_path"],
                                  attestations=list(selected["attestations"]),
                                  trust_config=selected["trust_config_path"])
    _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
    if not _same_evidence(pro, selected):
        raise BoardToolingError("Signed evidence or host trust changed during evaluation")
    binding = result.get("binding") or {}
    if result.get("contract_state") not in {"legacy", "error"} and binding.get("bundle_digest") != expected_digest:
        raise BoardToolingError("Design contract evaluation used another project revision")
    if result.get("accepted") and result.get("trust_config_sha256") != selected["trust_config_sha256"]:
        raise BoardToolingError("Design contract used another trust inventory")
    return {"bundle": before, "evaluation": result, "evidence_selection": selected}


def plan_placement(project: str, expected_digest: str,
                   contract_sha256: str, variant: str, operation: str,
                   moves: list[dict]) -> dict:
    pro, _ = _identity(project, expected_digest)
    api = _contract()
    if not re.fullmatch(r"[0-9a-f]{64}", contract_sha256):
        raise BoardToolingError("Exact contract SHA-256 required")
    if operation not in {"inspect_placement", "plan_placement"}:
        raise BoardToolingError("Only typed placement inspection and planning are available")
    typed_moves = []
    for move in moves:
        dx, dy = move["dx_mm"], move["dy_mm"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (dx, dy)):
            raise BoardToolingError("Finite mm displacement required")
        typed_moves.append({"footprint_uuid": move["footprint_uuid"],
                            "dx": {"value": dx, "unit": "mm"},
                            "dy": {"value": dy, "unit": "mm"}})
    request = {"expected_bundle_digest": expected_digest,
               "contract_sha256": contract_sha256, "variant": variant,
               "operation": operation, "moves": typed_moves,
               "required_native_capability": api.CAPABILITY}
    result = api.plan_placement(pro, request)
    _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
    if (result.get("bundle_digest") != expected_digest
            or result.get("contract_sha256") != contract_sha256
            or result.get("variant") != variant):
        raise BoardToolingError("Placement result identity mismatch")
    return result


def _run(argv: list[str], *, cwd: Path | None = None,
         evidence_dir: Path | None = None, context: dict | None = None) -> dict:
    job_root = os.environ.get("KICAD_MCP_ACTIVE_JOB_DIR")
    if evidence_dir is None and job_root:
        parent = Path(job_root) / "native-commands"
        parent.mkdir(parents=True, exist_ok=True)
        evidence_dir = parent / str(uuid.uuid4())
    evidence = str(evidence_dir) if evidence_dir is not None else None
    source = dict(context or {})
    if job_root:
        request_path = Path(job_root) / "request.json"
        if request_path.is_file():
            source["job_request_path"] = str(request_path)
            source["job_request_sha256"] = _digest(request_path)
            source["job_request"] = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result = run_contained(
            argv, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
            evidence_dir=evidence, evidence_context=source,
        )
    except subprocess.TimeoutExpired as exc:
        return {"argv": argv, "exit_code": None, "stdout": exc.output or "",
                "stderr": exc.stderr or "", "launch_error": str(exc),
                "native_evidence_dir": evidence}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        return {"argv": argv, "exit_code": getattr(exc, "returncode", None),
                "stdout": getattr(exc, "stdout", ""),
                "stderr": getattr(exc, "stderr", ""),
                "launch_error": f"Silent child launch failed: {exc}",
                "native_evidence_dir": evidence}
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        return {"argv": argv, "exit_code": getattr(exc, "returncode", None),
                "stdout": getattr(exc, "stdout", ""),
                "stderr": getattr(exc, "stderr", ""),
                "launch_error": f"Contained command interrupted: {type(exc).__name__}",
                "native_evidence_dir": evidence}
    return {"argv": argv, "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
            "native_evidence_dir": evidence}


def capabilities() -> dict:
    cli = _kicad_cli()
    python = _kicad_python()
    paths = {
        "bundle": SCRIPTS / "board_bundle.py",
        "gates": SCRIPTS / "pred_gates.ps1",
        "scene": SCRIPTS / "board_preflight.py",
        "exporter": SCRIPTS / "export_release.py",
        "contained_runner": Path(__file__).with_name("secure_subprocess.py"),
        "cli": cli, "native_python": python,
        "design_contract": SCRIPTS / "design_contract.py",
        "export_python": EXPORT_PYTHON,
    }
    tools = {name: {"path": str(path), "available": path.is_file(),
                    "sha256": _digest(path) if path.is_file() else None}
             for name, path in paths.items()}
    if cli.is_file():
        version = _run([str(cli), "--version"])
        tools["cli"]["version_check"] = version
        tools["cli"]["available"] = version["exit_code"] == 0
    try:
        crypto_version = metadata.version("cryptography")
    except metadata.PackageNotFoundError:
        crypto_version = None
    tools["export_python"]["dependency_check"] = {
        "server_interpreter": str(Path(sys.executable).resolve()),
        "cryptography_version": crypto_version}
    tools["export_python"]["available"] = (
        EXPORT_PYTHON.is_file() and crypto_version is not None and
        Path(sys.executable).resolve() == EXPORT_PYTHON.resolve())
    policy = _kicad_site_packages() / "kicad-headless-asserts.pth"
    tools["native_parser_policy"] = {"path": str(policy),
                                     "configured": policy.is_file()}
    key_configured = bool(_setting("boardtooling_visual_review_public_key", REVIEW_KEY_ENV))
    try:
        key_path, key_digest = _review_key()
        review_key = {"available": True, "path": str(key_path), "sha256": key_digest}
    except BoardToolingError as exc:
        review_key = {"available": False, "reason": str(exc), "configured": key_configured}
    return {
        "tools": tools,
        "independent_visual_review_key": review_key,
        "design_process_reference": str(ROOT / "docs/BOARD-DESIGN-BOOTSTRAP.md"),
        "expert_design_contract": {"api_available": tools["design_contract"]["available"],
            "draft_and_evaluation_supported": tools["design_contract"]["available"],
            "independent_acceptance_supported": False,
            "signed_evidence_paths_supported": tools["design_contract"]["available"],
            "host_trust_configured": bool(_setting("boardtooling_trust_config", TRUST_CONFIG_ENV)),
            "reason": "Acceptance requires valid host trust, complete signed evidence and exact native gates for the selected project"},
        "design_signoff": {"supported": False,
            "reason": "Gate results alone do not provide visual QC or independent design approval"},
        "native_interactive_footprint_drag": {"supported": False,
            "reason": "No maintained native interactive drag adapter"},
        "native_interactive_edge_shove": {"supported": False,
            "reason": "No maintained native interactive shove adapter"},
        "structural_validation_is_design_signoff": False,
    }


def _fresh_out(project: Path, output_dir: str) -> Path:
    out = Path(output_dir).absolute().resolve()
    project = project.resolve()
    if out.exists():
        raise BoardToolingError("Evidence output must be a new unique directory")
    if out == project.parent or out in project.parents:
        raise BoardToolingError("Evidence output cannot replace project sources")
    if project.parent in out.parents:
        relative = out.relative_to(project.parent)
        if relative.parts[0] not in {".tmp", "qc", "release", "gates"}:
            raise BoardToolingError("Evidence output inside a source directory is unsupported")
    if not out.parent.is_dir():
        raise BoardToolingError("Evidence output parent must exist")
    return out


def _write_run(out: Path, result: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, value in (("adapter.stdout.log", result.get("stdout") or ""),
                        ("adapter.stderr.log", result.get("stderr") or "")):
        if isinstance(value, bytes):
            (out / name).write_bytes(value)
        else:
            (out / name).write_text(str(value), encoding="utf-8")
    (out / "adapter-run.json").write_text(
        json.dumps({"argv": result["argv"], "exit_code": result["exit_code"],
                    "launch_error": result.get("launch_error"),
                    "native_evidence_dir": result.get("native_evidence_dir"),
                    "stdout_file": "adapter.stdout.log", "stderr_file": "adapter.stderr.log"}, indent=2),
        encoding="utf-8")


def _begin_native(out: Path, status: dict, argv: list[str], native_evidence: Path) -> None:
    status["attempt_phase"] = "native_running"
    status["native_evidence_dir"] = str(native_evidence)
    _write_run(out, {"argv": argv, "exit_code": None, "stdout": "", "stderr": "",
                     "native_evidence_dir": str(native_evidence)})
    (out / "adapter-status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8")


def evaluate_gates(project: str, expected_digest: str, variant: str,
                   rules_path: str, output_dir: str) -> dict:
    out = _fresh_out(Path(project).absolute(), output_dir)
    out.mkdir()
    result = None
    response = {"ok": False, "evidence_dir": str(out),
                "request": {"project_path": str(Path(project).absolute()),
                            "expected_bundle_digest": expected_digest,
                            "variant": variant, "rules_path": str(Path(rules_path).absolute())}}
    try:
        pro, before = _identity(project, expected_digest)
        if not variant or not variant.strip():
            raise BoardToolingError("Explicit variant required")
        rules = Path(rules_path).absolute().resolve()
        if not rules.is_file():
            raise BoardToolingError("Explicit rule slice missing")
        response["request"]["rules_sha256"] = _digest(rules)
        script = SCRIPTS / "pred_gates.ps1"
        if not script.is_file():
            raise BoardToolingError("Shared gate runner unavailable")
        if not POWERSHELL.is_file():
            raise BoardToolingError("Explicit hidden PowerShell executable unavailable")
        argv = [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-File", str(script),
                "-Board", str(pro.with_suffix(".kicad_pcb")), "-Out", str(out),
                "-Rules", str(rules), "-Variant", variant]
        native_evidence = out / "native-command"
        response["bundle"] = before
        _begin_native(out, response, argv, native_evidence)
        result = _run(argv, evidence_dir=native_evidence, context=response["request"])
        result.setdefault("native_evidence_dir", str(native_evidence))
        response["attempt_phase"] = "native_finished"
        response.update(bundle=before, **result)
        _write_run(out, result)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        result = {"argv": [], "exit_code": None, "stdout": "", "stderr": "",
                  "launch_error": str(exc)}
        response.update(result, error=str(exc))
        _write_run(out, result)
        (out / "adapter-status.json").write_text(json.dumps(response, indent=2, default=str), encoding="utf-8")
        return response
    report_path = out / "exits.json"
    if not report_path.is_file():
        response["error"] = result.get("launch_error") or "Shared gates produced no exits.json evidence"
        (out / "adapter-status.json").write_text(json.dumps(response, indent=2, default=str), encoding="utf-8")
        return response
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        exporter = _exporter()
        exporter.checked_gate_report(report)
        for skip in report.get("applicability_skips", []):
            if skip.get("gate") == "edge_plating":
                measured_path = out / "edge-plating-applicability.json"
                if not measured_path.is_file():
                    raise BoardToolingError("Measured edge-plating applicability evidence is missing")
                measured = json.loads(measured_path.read_text(encoding="utf-8-sig"))
                if measured != skip.get("measurement"):
                    raise BoardToolingError("Measured edge-plating applicability evidence differs from receipt")
        if not _same_path(report.get("board", ""), pro.with_suffix(".kicad_pcb")):
            raise BoardToolingError("Gate board path differs from exact project")
        if (report.get("bundle") or {}).get("digest") != expected_digest:
            raise BoardToolingError("Gate bundle digest mismatch")
        if report.get("variant") != variant or Path(report.get("rules_slice", "")).resolve() != rules:
            raise BoardToolingError("Gate variant or rules path mismatch")
        if report.get("rules_digest") != _digest(rules):
            raise BoardToolingError("Gate rules changed")
        if report.get("toolchain_digest") != _digest(_kicad_cli()):
            raise BoardToolingError("Gate toolchain identity mismatch")
        coverage = report.get("rule_coverage")
        if not isinstance(coverage, dict) or not coverage:
            raise BoardToolingError("Missing rule coverage")
        for rule, item in coverage.items():
            if not isinstance(item, dict) or item.get("status") not in {"required", "inapplicable"}:
                raise BoardToolingError(f"Unproved rule coverage: {rule}")
            if item["status"] == "required" and (not item.get("gates") or
                    any(g not in report["exits"] for g in item["gates"])):
                raise BoardToolingError(f"Missing required gate for rule: {rule}")
            if item["status"] == "inapplicable" and not item.get("reason"):
                raise BoardToolingError(f"Missing applicability reason: {rule}")
        _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
        if result["exit_code"] != 0:
            raise BoardToolingError("Gate command exited nonzero")
    except (ValueError, OSError, KeyError) as exc:
        message = str(exc)
        response["error"] = ("Shared gate applicability contract mismatch: " + message
                             if "applicability skip" in message else message)
        response["report"] = report if "report" in locals() else None
        (out / "adapter-status.json").write_text(json.dumps(response, indent=2, default=str), encoding="utf-8")
        return response
    response.update(ok=True, report=report)
    (out / "adapter-status.json").write_text(json.dumps(response, indent=2, default=str), encoding="utf-8")
    return response


def query_scene(project: str, expected_digest: str, bbox_mm: list[float],
                margin_mm: float = 0.5) -> dict:
    pro, before = _identity(project, expected_digest)
    if len(bbox_mm) != 4 or any(not math.isfinite(x) for x in bbox_mm):
        raise BoardToolingError("Finite four-coordinate bbox required")
    if not math.isfinite(margin_mm) or margin_mm < 0:
        raise BoardToolingError("Finite nonnegative margin required")
    native = _kicad_python()
    script = SCRIPTS / "board_preflight.py"
    rules = ROOT / "rules/BOARD-RULE-MATRIX.yaml"
    policy = _kicad_site_packages() / "kicad-headless-asserts.pth"
    if not all(path.is_file() for path in (native, script, rules, policy)):
        raise BoardToolingError("Native read-only scene parser or assertion policy unavailable")
    board = pro.with_suffix(".kicad_pcb")
    before_board = _digest(board)
    argv = [str(native), str(script), str(board), "--bbox",
            *(str(x) for x in bbox_mm), "--margin", str(margin_mm),
            "--rules", str(rules), "--allow-large"]
    result = _run(argv)
    if result["exit_code"] != 0:
        return {"ok": False, "error": result.get("launch_error") or "Native scene query failed",
                **result}
    try:
        scene = json.loads(result["stdout"])
        if (Path(scene["board"]["path"]).resolve() != board or
                scene["board"]["sha256"] != before_board or
                scene["rules"]["sha256"] != _digest(rules)):
            raise BoardToolingError("Scene source or rules identity mismatch")
        _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
    except (ValueError, KeyError, OSError) as exc:
        return {"ok": False, "error": str(exc), **result}
    return {"ok": True, "bundle": before, "scene": scene, **result}


def release(project: str, expected_digest: str, variant: str, fab: str,
            output_dir: str, *, rules_path: str | None = None,
            fab_notes: str | None = None, assembly: bool = False,
            gate_report: str | None = None,
            attestation_paths: list[str] | None = None,
            finalize_preview: str | None = None,
            visual_review: str | None = None) -> dict:
    if finalize_preview:
        return _finalize_release(project, expected_digest, variant, fab,
                                 output_dir, finalize_preview, visual_review)
    evidence = _fresh_out(Path(project).absolute(), output_dir)
    evidence.mkdir()
    result = None
    preview_out = evidence / "preview"
    requested_project = Path(project).absolute()
    requested_rules = Path(rules_path).absolute() if rules_path else requested_project.with_suffix(".rule-slice.json")
    requested_notes = Path(fab_notes).absolute() if fab_notes else requested_project.parent / "FAB-NOTES.md"
    status = {"ok": False, "evidence_dir": str(evidence),
              "preview_dir": str(preview_out),
              "request": {"project_path": str(Path(project).absolute()),
                          "expected_bundle_digest": expected_digest,
                          "variant": variant, "fab": fab,
                          "rules_path": str(requested_rules),
                          "fab_notes": str(requested_notes),
                          "assembly": assembly,
                          "gate_report": gate_report,
                          "attestation_paths": attestation_paths or []}}
    try:
        pro, before = _identity(project, expected_digest)
        if not variant or not variant.strip() or fab not in {"jlcpcb", "pcbway", "generic"}:
            raise BoardToolingError("Explicit declared variant and supported fab required")
        status["evidence_selection"] = _evidence_selection(
            pro, gate_report, attestation_paths or [], require_complete=True)
        contract_binding, selected = _release_contract(
            pro, expected_digest, variant, gate_report, attestation_paths or [])
        if selected != status["evidence_selection"]:
            raise BoardToolingError("Signed evidence changed during release preflight")
        key_path, key_digest = _review_key()
        if requested_rules.is_file():
            status["request"]["rules_sha256"] = _digest(requested_rules)
        if requested_notes.is_file():
            status["request"]["fab_notes_sha256"] = _digest(requested_notes)
        if key_path.is_relative_to(pro.parent):
            raise BoardToolingError("Independent reviewer key cannot be stored in project sources")
        exporter = SCRIPTS / "export_release.py"
        if not exporter.is_file() or not EXPORT_PYTHON.is_file():
            raise BoardToolingError("Maintained exporter or verified Python runtime unavailable")
        argv = [str(EXPORT_PYTHON), str(exporter), str(pro), "--fab", fab,
                "--variant", variant, "--visual-public-key", str(key_path),
                "--design-trust-config", selected["trust_config_path"],
                "--design-gate-report", selected["gate_report_path"],
                "--prepare", "--out", str(preview_out)]
        for path in selected["attestations"]:
            argv += ["--design-attestation", path]
        if rules_path:
            argv += ["--rules", str(Path(rules_path).absolute())]
        if fab_notes:
            argv += ["--fab-notes", str(Path(fab_notes).absolute())]
        if assembly:
            argv.append("--assembly")
        native_evidence = evidence / "native-command"
        status["contract_binding"] = contract_binding
        status["evidence_selection"] = selected
        status["review_public_key_digest"] = key_digest
        _begin_native(evidence, status, argv, native_evidence)
        result = _run(argv, evidence_dir=native_evidence, context=status["request"])
        result.setdefault("native_evidence_dir", str(native_evidence))
        status["attempt_phase"] = "native_finished"
        status.update(result)
        if result["exit_code"] != 0:
            raise BoardToolingError(result.get("launch_error") or "Maintained release exporter failed")
        state = json.loads((preview_out / "release-inputs.json").read_text(encoding="utf-8"))
        if (state["binding"]["bundle"]["digest"] != expected_digest or
                state["binding"]["variant"] != variant or
                state["binding"].get("review_public_key_digest") != key_digest or
                state["binding"].get("design_contract", {}).get("identity") != contract_binding or
                any(state["binding"].get("design_contract", {}).get(key) != selected[key]
                    for key in selected)):
            raise BoardToolingError("Release input binding mismatch")
        _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
        if not _same_evidence(pro, selected):
            raise BoardToolingError("Signed evidence or host trust changed during preview")
        if _review_key()[1] != key_digest:
            raise BoardToolingError("Configured visual reviewer key changed during preview")
        (preview_out / "mcp-preview-binding.json").write_text(
            json.dumps({"project_path": str(pro), "bundle_digest": expected_digest,
                        "variant": variant, "fab": fab,
                        "review_public_key_digest": key_digest,
                        "contract_sha256": contract_binding["contract_sha256"],
                        "evidence_selection": selected}, indent=2), encoding="utf-8")
        status["binding"] = state["binding"]
        status["manifest"] = state["manifest"]
        status["status"] = "preview"
        status["preview_dir"] = str(preview_out)
        status["ok"] = True
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        status["error"] = str(exc)
    finally:
        if result is None:
            result = {"argv": [], "exit_code": None, "stdout": "", "stderr": "",
                      "launch_error": status.get("error", "Preview preflight failed")}
            status.update(result)
        _write_run(evidence, result)
        (evidence / "adapter-status.json").write_text(
            json.dumps(status, indent=2, default=str), encoding="utf-8")
    return status


def _finalize_release(project: str, expected_digest: str, variant: str, fab: str,
                      attempt_dir: str, preview_dir: str,
                      visual_review: str | None) -> dict:
    """Every attempt has fresh evidence, including preflight and exporter failure."""
    requested_project = Path(project).absolute()
    attempt = _fresh_out(requested_project, attempt_dir)
    preview_location = Path(preview_dir).absolute().resolve()
    if (attempt == preview_location or attempt in preview_location.parents
            or preview_location in attempt.parents):
        raise BoardToolingError("Finalization evidence must be disjoint from preview")
    attempt.mkdir()
    result = None
    status = {"ok": False, "evidence_dir": str(attempt),
              "preview_dir": str(Path(preview_dir).absolute()),
              "request": {"project_path": str(Path(project).absolute()),
                          "expected_bundle_digest": expected_digest,
                          "variant": variant, "fab": fab,
                          "preview_dir": str(Path(preview_dir).absolute()),
                          "visual_review": visual_review}}
    try:
        pro, _ = _identity(project, expected_digest)
        if not variant or not variant.strip() or fab not in {"jlcpcb", "pcbway", "generic"}:
            raise BoardToolingError("Explicit declared variant and supported fab required")
        if not visual_review:
            raise BoardToolingError("Independent visual-review receipt required for finalization")
        key_path, key_digest = _review_key()
        if key_path.is_relative_to(pro.parent):
            raise BoardToolingError("Independent reviewer key cannot be stored in project sources")
        preview = Path(preview_dir).absolute().resolve()
        state = json.loads((preview / "release-inputs.json").read_text(encoding="utf-8"))
        adapter = json.loads((preview / "mcp-preview-binding.json").read_text(encoding="utf-8"))
        contract_state = state.get("binding", {}).get("design_contract", {})
        pinned = {key: contract_state.get(key) for key in (
            "trust_config_path", "trust_config_sha256", "gate_report_path",
            "gate_report_sha256", "attestations")}
        status["evidence_selection"] = pinned
        if (not _same_path(adapter.get("project_path", ""), pro)
                or adapter.get("bundle_digest") != expected_digest
                or adapter.get("variant") != variant or adapter.get("fab") != fab
                or adapter.get("review_public_key_digest") != key_digest):
            raise BoardToolingError("Preview belongs to a different exact project or selection")
        if (not isinstance(pinned["attestations"], dict)
                or not pinned["gate_report_path"]
                or adapter.get("evidence_selection") != pinned
                or not _same_evidence(pro, pinned)):
            raise BoardToolingError("Preview signed evidence or host trust changed")
        contract_binding, current_selection = _release_contract(
            pro, expected_digest, variant, pinned["gate_report_path"],
            list(pinned["attestations"]))
        if current_selection != pinned or adapter.get("contract_sha256") != contract_binding["contract_sha256"]:
            raise BoardToolingError("Preview contract or authority identity changed")
        if (state.get("binding", {}).get("bundle", {}).get("digest") != expected_digest
                or state.get("binding", {}).get("variant") != variant
                or state.get("binding", {}).get("review_public_key_digest") != key_digest
                or state.get("binding", {}).get("design_contract", {}).get("identity") != contract_binding):
            raise BoardToolingError("Preview does not bind exact project revision and variant")
        exporter = SCRIPTS / "export_release.py"
        if not exporter.is_file() or not EXPORT_PYTHON.is_file():
            raise BoardToolingError("Maintained release exporter unavailable")
        argv = [str(EXPORT_PYTHON), str(exporter), str(pro), "--fab", fab,
                 "--variant", variant, "--finalize", str(preview),
                 "--visual-review", str(Path(visual_review).absolute()),
                 "--visual-public-key", str(key_path)]
        native_evidence = attempt / "native-command"
        status["contract_binding"] = contract_binding
        status["evidence_selection"] = pinned
        status["review_public_key_digest"] = key_digest
        _begin_native(attempt, status, argv, native_evidence)
        result = _run(argv, evidence_dir=native_evidence, context=status["request"])
        result.setdefault("native_evidence_dir", str(native_evidence))
        status["attempt_phase"] = "native_finished"
        status.update(result)
        if result["exit_code"] != 0:
            raise BoardToolingError(result.get("launch_error")
                                    or "Maintained release exporter failed")
        receipt = json.loads((preview / "release-receipt.json").read_text(encoding="utf-8"))
        if (receipt.get("status") != "verified"
                or receipt.get("binding") != state.get("binding")):
            raise BoardToolingError("Missing or mismatched verified release receipt")
        _bundle().verify_snapshot(pro, expected_digest, require_complete=True)
        if not _same_evidence(pro, pinned):
            raise BoardToolingError("Signed evidence or host trust changed during finalization")
        if _review_key()[1] != key_digest:
            raise BoardToolingError("Configured visual reviewer key changed during finalization")
        status.update(ok=True, status="verified", receipt=receipt)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        status["error"] = str(exc)
    finally:
        if result is None:
            result = {"argv": [], "exit_code": None, "stdout": "", "stderr": "",
                      "launch_error": status.get("error", "Finalization preflight failed")}
            status.update(result)
        _write_run(attempt, result)
        (attempt / "adapter-status.json").write_text(
            json.dumps(status, indent=2, default=str), encoding="utf-8")
    return status
