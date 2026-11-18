"""Tests for the L4 UI broker (docs/L4-UI-BROKER-SPEC.md).

Unit tests need no GUI: registry schema, approval gate, version refusal, typed values and path
escapes, monitor logic on fake window lists, helper-side pure logic, launch-precondition logic and a
source scan. Live tests (marked ``l4_live``) drive real pcbnew on a private desktop on synthetic
temporary projects; they are skipped only when KiCad's pcbnew.exe is absent or KICAD_MCP_L4_LIVE=0,
and a skip is not a pass.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

from kicad_mcp.l4 import broker, monitor, registry
from kicad_mcp.l4 import uia_helper as helper
from kicad_mcp.l4.registry import APPROVAL_ENV, RegistryRefusal

try:  # the repository's pytest config uses --strict-markers and does not register l4_live
    l4_live = pytest.mark.l4_live
except (Exception, pytest.fail.Exception):  # pragma: no cover
    l4_live = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "kicad_mcp"
TEXT_BOARD = ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "10.0") '
              '(general (thickness 1.6)) (paper "A4") (layers (0 "F.Cu" signal) (2 "B.Cu" signal) '
              '(25 "Edge.Cuts" user)) (setup (pad_to_mask_clearance 0)) (net 0 ""))\n')


# ---- synthetic recording observations (no GUI) ----------------------------------------------------

def _obs(name, control_type, *, automation_id="", ancestry=(), match_count=1, id_matches=None):
    return {"name": name, "control_type": control_type, "automation_id": automation_id, "class_name": "wx",
            "ancestry": [{"name": n, "control_type": t} for n, t in ancestry], "match_count": match_count,
            "enabled": True, "automation_id_matches": id_matches}


def _launch(*, close_name="Close", output_aid="", output_name="Output directory:", save_enabled=True):
    dialog_ancestry = (("Plot panel", "Pane"),)
    return {
        "main_window": {"name": "board - PCB Editor", "count": 1},
        "baseline_windows": [{"name": "Hidden helper", "class_name": "wxWindowNR"}],
        "ops": {
            "pcb_open_and_save": {"menu": ["File", "Save"], "leaf_enabled": save_enabled},
            "pcb_export_gerbers_dialog": {
                "menu": ["File", "Fabrication Outputs", "Gerbers (.gbr)..."],
                "dialog_window": {"name": "Plot", "control_type": "Window"}, "closed": True,
                "controls": {
                    "output_dir": _obs(output_name, "Edit", automation_id=output_aid, ancestry=dialog_ancestry,
                                       id_matches=1 if output_aid else None),
                    "plot_button": _obs("Plot", "Button", ancestry=dialog_ancestry),
                    "close_button": _obs(close_name, "Button", ancestry=dialog_ancestry)}},
            "pcb_board_setup_read": {
                "menu": ["File", "Board Setup..."],
                "dialog_window": {"name": "Board Setup", "control_type": "Window"}, "closed": True,
                "controls": {"stackup_page": _obs("Physical Stackup", "TreeItem", ancestry=dialog_ancestry),
                             "copper_layers": _obs("Copper layers:", "ComboBox", ancestry=dialog_ancestry),
                             "cancel_button": _obs("Cancel", "Button", ancestry=dialog_ancestry)}}},
        "errors": [], "monitor_ok": True}


@pytest.fixture(autouse=True)
def _isolated_from_machine_config(monkeypatch, tmp_path):
    """Every test (live ones included) resolves KiCad by discovery alone, never from the per-machine config
    or an ambient KICAD_APP_PATH, so results do not depend on this machine's settings."""
    monkeypatch.setenv("KICAD_AI_TOOLS_CONFIG", str(tmp_path / "isolated-config.json"))
    if os.environ.get("KICAD_MCP_L4_APP_ROOT"):  # an explicit live-tier choice: launch exactly that install
        monkeypatch.setenv("KICAD_APP_PATH", os.environ["KICAD_MCP_L4_APP_ROOT"])
    else:
        monkeypatch.delenv("KICAD_APP_PATH", raising=False)
    from kicad_mcp.adaptive_editor import kicad_env
    kicad_env.kicad_app_root.cache_clear()
    kicad_env.kicad_version.cache_clear()  # the version belongs to the install just resolved
    yield
    getattr(kicad_env.kicad_app_root, "cache_clear", lambda: None)()  # a test may have replaced it
    getattr(kicad_env.kicad_version, "cache_clear", lambda: None)()


@pytest.fixture
def registry_doc():
    document, report = broker.build_registry_document("10.0.5", [_launch(), _launch(), _launch()])
    assert document is not None, report
    return document


@pytest.fixture
def registry_file(tmp_path, registry_doc):
    path = tmp_path / "ui-registry-10.0.5.json"
    path.write_bytes(broker.registry_bytes(registry_doc))
    return path


@pytest.fixture
def approved(registry_file):
    return {APPROVAL_ENV: hashlib.sha256(registry_file.read_bytes()).hexdigest()}


def _write_registry(tmp_path, document, name="reg.json"):
    path = tmp_path / name
    path.write_bytes(broker.registry_bytes(document))
    return path, {APPROVAL_ENV: hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def text_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_pcb").write_text(TEXT_BOARD, encoding="utf-8")
    return project


# ---- registry: schema ------------------------------------------------------------------------------

def test_assembled_registry_is_schema_valid_and_closed(registry_doc):
    registry.validate_registry(registry_doc)
    assert sorted(registry_doc["operations"]) == ["pcb_board_setup_read", "pcb_export_gerbers_dialog",
                                                  "pcb_open_and_save"]
    for op in registry_doc["operations"].values():
        assert op["api_alternative"] and op["application"] == "pcbnew"
        for dialog in op["dialogs"].values():
            assert isinstance(dialog["safe_to_cancel"], bool)
    assert registry_doc["operations"]["pcb_open_and_save"]["steps"][0]["path"] == ["File", "Save"]


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.update(extra=1), "unknown keys"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.pop("known_dialogs"), "missing keys"),
    (lambda d: d["operations"].update({"Bad Name": d["operations"]["pcb_open_and_save"]}), "bad operation name"),
    (lambda d: d["operations"]["pcb_open_and_save"].update(application="eeschema"), "application"),
    (lambda d: d["operations"]["pcb_open_and_save"]["steps"].append({"action": "click_at", "x": 1, "y": 2}), "action"),
    (lambda d: d["operations"]["pcb_open_and_save"]["steps"].append({"action": "invoke", "control": "nope"}), "unknown control"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["steps"][2].update(param="nope"), "unknown parameter"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["dialogs"]["plot"].update(cancel=None), "safe_to_cancel"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["dialogs"]["plot"].pop("safe_to_cancel"), "missing keys"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["params"]["output_dir"].update(type="regex"), "type must be"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["controls"]["plot_button"].update(control_type="Nope"), "control type"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["controls"]["plot_button"].update(dialog="ghost"), "unknown dialog"),
    (lambda d: d["operations"]["pcb_export_gerbers_dialog"]["postcondition"].update(glob="../*.gbr"), "glob"),
    (lambda d: d["operations"]["pcb_board_setup_read"]["postcondition"].update(required_outputs=["ghost"]), "required_outputs"),
    (lambda d: d["main_window"].update(match="regex"), "match"),
])
def test_registry_schema_rejects(registry_doc, mutate, fragment):
    document = copy.deepcopy(registry_doc)
    mutate(document)
    with pytest.raises(RegistryRefusal) as caught:
        registry.validate_registry(document)
    assert caught.value.code == "registry_invalid"
    assert fragment in str(caught.value)


def test_registry_rejects_duplicate_json_keys():
    with pytest.raises(RegistryRefusal) as caught:
        registry.parse_registry(b'{"a": 1, "a": 2}')
    assert caught.value.code == "registry_invalid"


def test_registry_number_param_schema_is_validated(registry_doc):
    document = copy.deepcopy(registry_doc)
    document["operations"]["pcb_open_and_save"]["params"]["tolerance"] = {
        "type": "number", "min": 0, "max": 10, "unit": "mm"}
    registry.validate_registry(document)
    document["operations"]["pcb_open_and_save"]["params"]["tolerance"]["min"] = 11
    with pytest.raises(RegistryRefusal):
        registry.validate_registry(document)
    document["operations"]["pcb_open_and_save"]["params"]["tolerance"] = {"type": "number", "min": 0, "max": 1}
    with pytest.raises(RegistryRefusal):
        registry.validate_registry(document)


def test_control_type_names_match_the_helper():
    assert tuple(sorted(helper.CONTROL_TYPES.values())) == registry.CONTROL_TYPES
    assert helper.MATCH_MODES == registry.MATCH_MODES


# ---- registry: approval, build, shipped file --------------------------------------------------------

def test_unapproved_sha_refuses_all_operations(registry_file, approved):
    digest = approved[APPROVAL_ENV]
    for env in ({}, {APPROVAL_ENV: ""}, {APPROVAL_ENV: "0" * 64}, {APPROVAL_ENV: "not-a-hash," + "f" * 63},
                {APPROVAL_ENV: digest[:-1]}):
        with pytest.raises(RegistryRefusal) as caught:
            registry.load_registry(registry_file, env, kicad_build="10.0.5")
        assert caught.value.code == "registry_unapproved"
        assert caught.value.details["sha256"] == digest
    loaded = registry.load_registry(registry_file, approved, kicad_build="10.0.5")
    assert loaded.sha256 == digest and loaded.operation_names()


def test_approval_list_is_comma_separated_and_case_insensitive(registry_file, approved):
    digest = approved[APPROVAL_ENV]
    env = {APPROVAL_ENV: f" {'a' * 64} , {digest.upper()} "}
    assert registry.load_registry(registry_file, env, kicad_build="10.0.5").sha256 == digest


def test_any_byte_change_needs_new_approval(registry_file, approved):
    registry_file.write_bytes(registry_file.read_bytes() + b" ")
    with pytest.raises(RegistryRefusal) as caught:
        registry.load_registry(registry_file, approved, kicad_build="10.0.5")
    assert caught.value.code == "registry_unapproved"


def test_version_mismatch_refuses(registry_file, approved):
    with pytest.raises(RegistryRefusal) as caught:
        registry.load_registry(registry_file, approved, kicad_build="10.0.6")
    assert caught.value.code == "registry_version_mismatch"
    assert caught.value.details == {"registry_build": "10.0.5", "running_build": "10.0.6"}
    for unknown in (None, ""):
        with pytest.raises(RegistryRefusal) as caught:
            registry.load_registry(registry_file, approved, kicad_build=unknown)
        assert caught.value.code == "kicad_build_unknown"
    assert registry.load_registry(registry_file, approved, skip_build_check=True).kicad_build == "10.0.5"


def test_approved_but_invalid_registry_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b"{}\n")
    env = {APPROVAL_ENV: hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(RegistryRefusal) as caught:
        registry.load_registry(path, env, kicad_build="10.0.5")
    assert caught.value.code == "registry_invalid"
    with pytest.raises(RegistryRefusal) as caught:
        registry.load_registry(tmp_path / "missing.json", env, kicad_build="10.0.5")
    assert caught.value.code == "registry_unreadable"


def test_shipped_registry_is_release_approved_and_any_byte_change_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv(APPROVAL_ENV, raising=False)
    loaded = registry.load_registry(kicad_build="10.0.5")  # approved by registry/APPROVED.sha256, no env needed
    assert loaded.operation_names() == ["pcb_board_setup_read", "pcb_export_gerbers_dialog", "pcb_open_and_save"]
    info = registry.inspect_registry_file()
    assert info["approved"] is True and info["release_approved"] is True
    # a copy with one changed byte, next to the same APPROVED.sha256, is refused
    shipped = registry.DEFAULT_REGISTRY_PATH
    copy = tmp_path / shipped.name
    copy.write_bytes(shipped.read_bytes().replace(b'"Plot"', b'"Plot "', 1))
    (tmp_path / registry.RELEASE_APPROVALS).write_bytes((shipped.parent / registry.RELEASE_APPROVALS).read_bytes())
    with pytest.raises(RegistryRefusal) as caught:
        registry.load_registry(copy, {}, kicad_build="10.0.5")
    assert caught.value.code == "registry_unapproved"
    # an approval line for another file name approves nothing
    (tmp_path / "other.json").write_bytes(shipped.read_bytes())
    assert registry.release_approved_sha256s(tmp_path / "other.json") == set()
    assert not re.search(r"[0-9a-f]{64}", (ROOT / "kicad_mcp" / "l4" / "registry.py").read_text(encoding="utf-8")), \
        "no sha256 may be hard-coded in source; approvals live in APPROVED.sha256 or the environment"


# ---- registry: unknown operation / parameter / type, typed values, paths -----------------------------

def test_unknown_operation_is_rejected(registry_file, approved):
    loaded = registry.load_registry(registry_file, approved, kicad_build="10.0.5")
    for name in ("pcb_delete_everything", "", None, 7, "PCB_OPEN_AND_SAVE"):
        with pytest.raises(RegistryRefusal) as caught:
            loaded.operation(name)
        assert caught.value.code == "unknown_operation"


@pytest.fixture
def gerbers_op(registry_doc):
    return registry_doc["operations"]["pcb_export_gerbers_dialog"]


def test_unknown_missing_and_mistyped_parameters_are_rejected(gerbers_op, tmp_path):
    clone = tmp_path / "clone"
    ok = registry.validate_params(gerbers_op, {"output_dir": "gerbers"}, clone)
    assert Path(ok["output_dir"]) == (clone / "gerbers").resolve()
    cases = [({"output_dir": "gerbers", "selector": "x"}, "unknown_parameter"),
             ({"click_x": 3}, "unknown_parameter"),
             ({}, "missing_parameter"),
             ({"output_dir": 5}, "invalid_parameter_type"),
             ({"output_dir": ["a"]}, "invalid_parameter_type"),
             ("output_dir=gerbers", "invalid_parameters"),
             ({3: "x"}, "invalid_parameters")]
    for params, code in cases:
        with pytest.raises(RegistryRefusal) as caught:
            registry.validate_params(gerbers_op, params, clone)
        assert caught.value.code == code, params
    assert registry.validate_params(gerbers_op | {"params": {}}, None, clone) == {}


def test_typed_enum_and_number_values():
    op = {"params": {"layer": {"type": "enum", "values": ["F.Cu", "B.Cu"]},
                     "width": {"type": "number", "min": 0.1, "max": 5, "unit": "mm"},
                     "count": {"type": "number", "min": 1, "max": 4, "unit": "layers", "integer": True,
                               "required": False}}}
    clone = Path("C:/nonexistent-clone")
    assert registry.validate_params(op, {"layer": "F.Cu", "width": 0.2}, clone) == {"layer": "F.Cu", "width": 0.2}
    assert registry.validate_params(op, {"layer": "B.Cu", "width": 1, "count": 2.0}, clone)["count"] == 2
    bad = [({"layer": "In1.Cu", "width": 1}, "invalid_parameter_value"),
           ({"layer": 1, "width": 1}, "invalid_parameter_value"),
           ({"layer": "F.Cu", "width": True}, "invalid_parameter_type"),
           ({"layer": "F.Cu", "width": "1"}, "invalid_parameter_type"),
           ({"layer": "F.Cu", "width": float("nan")}, "invalid_parameter_type"),
           ({"layer": "F.Cu", "width": float("inf")}, "invalid_parameter_type"),
           ({"layer": "F.Cu", "width": 6}, "parameter_out_of_range"),
           ({"layer": "F.Cu", "width": 0.05}, "parameter_out_of_range"),
           ({"layer": "F.Cu", "width": 1, "count": 2.5}, "invalid_parameter_type"),
           ({"layer": "F.Cu", "width": 1, "count": 9}, "parameter_out_of_range")]
    for params, code in bad:
        with pytest.raises(RegistryRefusal) as caught:
            registry.validate_params(op, params, clone)
        assert caught.value.code == code, params


@pytest.mark.parametrize("value,code", [
    ("../outside", "path_escape"), ("a/../../b", "path_escape"), ("a\\..\\..\\b", "path_escape"),
    ("..", "path_escape"), ("/abs", "path_not_relative"), ("\\rooted", "path_not_relative"),
    ("C:\\Windows", "path_not_relative"), ("C:rel", "path_not_relative"), ("\\\\srv\\share\\x", "path_not_relative"),
    ("//srv/share/x", "path_not_relative"), ("a:stream", "path_not_relative"), ("dir/a:stream", "path_invalid"),
    ("a\x00b", "path_invalid"),
    ("a?b", "path_invalid"), ("", "path_invalid"), (".", "path_invalid"), ("./", "path_invalid"),
    ("con", "path_invalid"), ("out/NUL.txt", "path_invalid"), ("trailing.", "path_invalid"),
    ("x" * 201, "path_invalid"), (None, "invalid_parameter_type"), (3, "invalid_parameter_type")])
def test_clone_relative_path_escapes_are_rejected(tmp_path, value, code):
    with pytest.raises(RegistryRefusal) as caught:
        registry.resolve_clone_relative(tmp_path / "clone", value)
    assert caught.value.code == code, value


def test_clone_relative_paths_accepted_and_symlink_escape_rejected(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    assert registry.resolve_clone_relative(clone, "gerbers/top") == str((clone / "gerbers" / "top").resolve())
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (clone / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted here")
    with pytest.raises(RegistryRefusal) as caught:
        registry.resolve_clone_relative(clone, "link/inside")
    assert caught.value.code == "path_escape"


# ---- broker: everything unknown is refused before anything is launched -------------------------------

@pytest.fixture
def nothing_may_launch(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a launch or placement check was attempted")

    monkeypatch.setattr(broker.hd, "placement_check", boom)
    monkeypatch.setattr(broker.hd, "create_process_on_desktop", boom)
    monkeypatch.setattr(broker.hd.HiddenKiCad, "start", boom)
    monkeypatch.setattr(broker.HiddenSession, "open", boom)
    monkeypatch.setattr(broker, "kicad_cli_loads_board", boom)


def _refused(result, code):
    assert result["status"] == "refused" and result["ok"] is False and result["launched"] is False, result
    assert result["error"]["code"] == code, result


def test_ui_fallback_refusals_precede_any_launch(tmp_path, registry_file, approved, text_project,
                                                 nothing_may_launch, monkeypatch):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(broker, "detect_kicad_build", lambda root: "10.0.5")
    run = lambda op, params=None, env=approved, project=text_project, **kw: broker.ui_fallback(  # noqa: E731
        op, project, params, registry_path=registry_file, evidence_root=evidence, approval_env=env,
        app_root=tmp_path / "kicad", **kw)
    _refused(run("pcb_open_and_save", env={}), "registry_unapproved")
    _refused(run("pcb_open_and_save", env={APPROVAL_ENV: "1" * 64}), "registry_unapproved")
    _refused(run("pcb_delete_everything"), "unknown_operation")
    _refused(run("pcb_open_and_save", {"x": 1}), "unknown_parameter")
    _refused(run("pcb_export_gerbers_dialog", {}), "missing_parameter")
    _refused(run("pcb_export_gerbers_dialog", {"output_dir": "../../escape"}), "path_escape")
    _refused(run("pcb_export_gerbers_dialog", {"output_dir": "C:\\x"}), "path_not_relative")
    _refused(run("pcb_open_and_save", project=tmp_path / "missing"), "project_not_found")
    _refused(run("pcb_open_and_save", project="relative/dir"), "project_invalid")
    monkeypatch.setattr(broker, "detect_kicad_build", lambda root: "10.0.6")
    _refused(run("pcb_open_and_save"), "registry_version_mismatch")
    assert not evidence.exists(), "a refusal must not even create an evidence directory"


def test_project_checks(tmp_path, text_project):
    project, board = broker.check_project_dir(text_project)
    assert board.name == "board.kicad_pcb"
    (text_project / "second.kicad_pcb").write_text(TEXT_BOARD, encoding="utf-8")
    with pytest.raises(broker.L4Error) as caught:
        broker.check_project_dir(text_project)
    assert caught.value.code == "board_not_unique"
    (text_project / "second.kicad_pcb").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    try:
        (text_project / "link.txt").symlink_to(outside)
    except OSError:
        return
    with pytest.raises(broker.L4Error) as caught:
        broker.check_project_dir(text_project)
    assert caught.value.code == "project_has_links"


def test_unexpected_window_logic(registry_doc, tmp_path):
    path, env = _write_registry(tmp_path, registry_doc)
    loaded = registry.load_registry(path, env, kicad_build="10.0.5")
    op = loaded.operation("pcb_export_gerbers_dialog")
    windows = [{"name": "board - PCB Editor", "control_type": "Window", "class_name": "wxWindowNR"},
               {"name": "Plot", "control_type": "Window", "class_name": "#32770"},
               {"name": "Hidden helper", "control_type": "Window", "class_name": "wxWindowNR"},
               {"name": "", "control_type": "Window", "class_name": "x"},
               {"name": "File", "control_type": "Menu", "class_name": "#32768"},
               {"name": "tip", "control_type": "Window", "class_name": "tooltips_class32"}]
    assert broker.find_unexpected(windows, loaded, op) == []
    windows.append({"name": "Save changes?", "control_type": "Window", "class_name": "#32770"})
    assert [w["name"] for w in broker.find_unexpected(windows, loaded, op)] == ["Save changes?"]


# ---- recording: 3-launch stability (pure) ---------------------------------------------------------------

def test_stable_identifiers_are_admitted_and_report_is_empty_of_drops():
    document, report = broker.build_registry_document("10.0.5", [_launch(), _launch(), _launch()])
    assert document is not None and report["dropped"] == []
    assert report["admitted"] == ["pcb_open_and_save", "pcb_export_gerbers_dialog", "pcb_board_setup_read"]
    plot = document["operations"]["pcb_export_gerbers_dialog"]
    assert plot["controls"]["output_dir"] == {
        "dialog": "plot", "by": "path", "name": "Output directory:", "name_match": "exact", "control_type": "Edit",
        "ancestry": [{"name": "Plot panel", "control_type": "Pane"}]}
    assert plot["dialogs"]["plot"]["cancel"] == {"name": "Close", "control_type": "Button"}
    assert document["ambient_windows"] == [{"name": "Hidden helper", "match": "exact"}]
    raw = broker.registry_bytes(document)
    assert b"\r" not in raw and raw.endswith(b"}\n")


def test_unstable_identifier_is_dropped_and_reported():
    launches = [_launch(), _launch(close_name="Close"), _launch(close_name="Done")]
    document, report = broker.build_registry_document("10.0.5", launches)
    assert document is not None
    assert "pcb_export_gerbers_dialog" not in document["operations"]
    dropped = [d for d in report["dropped"] if d["operation"] == "pcb_export_gerbers_dialog"]
    assert dropped and dropped[0]["element"] == "control:close_button"
    assert "pcb_open_and_save" in report["admitted"] and "pcb_board_setup_read" in report["admitted"]
    assert report["dropped"] == document["provenance"]["dropped"]


def test_numeric_automation_ids_are_not_admitted_but_stable_named_ones_are():
    nameless = [_launch(output_name="", output_aid="1234") for _ in range(3)]
    document, report = broker.build_registry_document("10.0.5", nameless)
    assert document is not None and "pcb_export_gerbers_dialog" not in document["operations"]
    named = [_launch(output_name="", output_aid="outputDirectory") for _ in range(3)]
    document, report = broker.build_registry_document("10.0.5", named)
    control = document["operations"]["pcb_export_gerbers_dialog"]["controls"]["output_dir"]
    assert control["by"] == "automation_id" and control["automation_id"] == "outputDirectory"


def test_only_wx_stock_numeric_ids_are_stable():
    assert registry.stable_automation_id("5101") and registry.stable_automation_id("5100")   # wxID_CANCEL / OK
    assert not registry.stable_automation_id("-31726")  # wxID_ANY-allocated (dynamic), negative
    assert not registry.stable_automation_id("1234") and not registry.stable_automation_id("6000")
    assert registry.stable_automation_id("outputDirectory") and not registry.stable_automation_id("  ")
    for tricky in ("+5101", "5101.0", "--5101", " 5101", "05101", "-5101", "", None, 5101, "1e3", "٥١٠١"):
        assert not registry.stable_automation_id(tricky), tricky  # never raises, never admits
    stock = [_launch(close_name="", output_aid="") for _ in range(3)]
    for launch in stock:  # nameless close button found by its stock id in every launch
        close = launch["ops"]["pcb_export_gerbers_dialog"]["controls"]["close_button"]
        close.update(automation_id="5101", automation_id_matches=1, match_count=2)
    document, _ = broker.build_registry_document("10.0.5", stock)
    control = document["operations"]["pcb_export_gerbers_dialog"]["controls"]["close_button"]
    assert control["by"] == "automation_id" and control["automation_id"] == "5101"


@pytest.mark.parametrize("aid", ["1234", "-31726", "+5101", "6000"])
def test_hand_edited_registry_with_a_dynamic_automation_id_is_invalid(registry_doc, aid):
    doc = json.loads(json.dumps(registry_doc))
    doc["operations"]["pcb_export_gerbers_dialog"]["controls"]["close_button"] = {
        "dialog": "plot", "by": "automation_id", "automation_id": aid, "control_type": "Button"}
    with pytest.raises(registry.RegistryRefusal) as info:
        registry.validate_registry(doc)
    assert info.value.code == "registry_invalid" and "stable automation id" in str(info.value)


def test_disabled_save_and_wrong_launch_count_and_missing_main_window():
    document, report = broker.build_registry_document("10.0.5", [_launch(save_enabled=False) for _ in range(3)])
    assert "pcb_open_and_save" not in document["operations"]
    assert {"operation": "pcb_open_and_save", "element": "menu_leaf",
            "reason": "menu_item_disabled_on_unmodified_board"} in report["dropped"]
    assert broker.build_registry_document("10.0.5", [_launch(), _launch()])[0] is None
    broken = [_launch(), _launch(), _launch()]
    broken[1]["main_window"] = None
    assert broker.build_registry_document("10.0.5", broken)[0] is None


def test_choose_selector_and_menu_path_directly():
    same = [_obs("Plot", "Button") for _ in range(3)]
    assert registry.choose_selector(same)[0]["name"] == "Plot"
    assert registry.choose_selector([same[0], same[1], None])[0] is None
    assert registry.choose_selector([same[0], same[1], _obs("Plot", "Button", match_count=2)])[0] is None
    assert registry.choose_selector([same[0], same[1], _obs("Plot", "Button", ancestry=[("x", "Pane")])])[0] is None
    assert registry.choose_menu_path([["&File", "Save\tCtrl+S"]] * 3)[0] == ["File", "Save"]
    assert registry.choose_menu_path([["File", "Save"], ["File", "Save"], ["File", "Save As"]])[0] is None


# ---- monitor logic on fake window lists --------------------------------------------------------------

def test_task_windows_on_input_desktop_are_found_by_pid_only():
    windows = [{"hwnd": 1, "pid": 100, "title": "owner editor"}, {"hwnd": 2, "pid": 200, "title": "pcbnew"},
               {"hwnd": 3, "pid": 300, "title": "helper"}]
    assert monitor.task_windows_on_input_desktop(windows, [200, 300]) == windows[1:]
    assert monitor.task_windows_on_input_desktop(windows, []) == []
    assert monitor.task_windows_on_input_desktop([], [1]) == []
    # a title that merely looks like KiCad is not attributed: attribution is by process only
    assert monitor.task_windows_on_input_desktop([{"hwnd": 9, "pid": 5, "title": "PCB Editor"}], [200]) == []


def test_descendant_pids_follow_parent_links_and_survive_cycles():
    table = [(1, 0), (10, 1), (11, 10), (12, 10), (20, 2), (13, 12), (5, 5), (6, 7), (7, 6)]
    assert monitor.descendant_pids([10], table) == {10, 11, 12, 13}
    assert monitor.descendant_pids([6], table) == {6, 7}
    assert monitor.descendant_pids([99], table) == {99}


def test_foreground_attribution_counts_changes_and_flags_only_task_pids():
    samples = [{"t": 0, "hwnd": 1, "pid": 100}, {"t": 1, "hwnd": 2, "pid": 101}, {"t": 2, "hwnd": 2, "pid": 101},
               {"t": 3, "hwnd": 3, "pid": 200}]
    result = monitor.foreground_attribution(samples, {200})
    assert result["changes"] == 2 and [s["hwnd"] for s in result["attributed"]] == [3]
    assert monitor.foreground_attribution(samples, set())["attributed"] == []
    assert monitor.foreground_attribution([], {1}) == {"samples": 0, "changes": 0, "attributed": []}


def _wts(**over):
    return {"ok": True, "console_session_id": 1, "connect_state": 0, "lock_state": "unlocked", **over}


def test_wts_comparison():
    assert monitor.compare_wts(_wts(), _wts()) == []
    assert monitor.compare_wts(_wts(lock_state="unlocked"), _wts(lock_state="locked")) == [
        {"field": "lock_state", "before": "unlocked", "after": "locked"}]
    assert monitor.compare_wts(_wts(), _wts(connect_state=4))[0]["field"] == "connect_state"
    assert monitor.compare_wts(_wts(), _wts(console_session_id=2))[0]["field"] == "console_session_id"
    assert monitor.compare_wts(_wts(lock_state=None), _wts(lock_state=None)) == []
    assert monitor.compare_wts(_wts(lock_state=None), _wts()) == []
    assert monitor.compare_wts(None, _wts())[0]["field"] == "snapshot_unavailable"
    assert monitor.compare_wts(_wts(), {"ok": False, "error": "x"})[0]["field"] == "snapshot_unavailable"


def test_verdict_aggregates_every_guard_and_never_passes_without_samples():
    clean = dict(input_desktop_samples=40, window_hits=[], foreground={"changes": 3, "attributed": []}, wts_diffs=[])
    verdict = monitor.build_verdict(**clean)
    assert verdict["ok"] is True and verdict["violations"] == [] and verdict["input_desktop_samples"] == 40
    assert monitor.build_verdict(**{**clean, "input_desktop_samples": 0})["ok"] is False
    hit = monitor.build_verdict(**{**clean, "window_hits": [{"pid": 1}]})
    assert hit["ok"] is False and hit["task_windows_on_input_desktop"] == 1
    fg = monitor.build_verdict(**{**clean, "foreground": {"changes": 1, "attributed": [{"pid": 1}]}})
    assert fg["ok"] is False and fg["foreground_samples_attributed_to_task"] == 1
    wts = monitor.build_verdict(**{**clean, "wts_diffs": [{"field": "lock_state", "before": 1, "after": 0}]})
    assert wts["ok"] is False and wts["violations"][0]["kind"] == "wts_state_changed"
    assert monitor.build_verdict(**clean, extra_violations=[{"kind": "tripwire"}])["ok"] is False


def test_task_pids_is_a_thread_safe_set():
    pids = monitor.TaskPids()
    pids.add(5)
    pids.add("7")
    assert pids.snapshot() == {5, 7}


# ---- helper-side pure logic ---------------------------------------------------------------------------

def test_helper_normalisation_matches_registry_normalisation():
    for text in ("&File", "Save\tCtrl+S", "Gerbers (.gbr)\u2026", "  Plot ", "", None, "A&&B"):
        assert helper.norm(text) == registry.norm_name(text)
    assert helper.name_matches("&Plot", "Plot", "exact") and not helper.name_matches("Plot all", "Plot", "exact")
    assert helper.name_matches("Gerbers (.gbr)...", "Gerbers", "prefix")
    assert helper.name_matches("x PCB Editor y", "PCB Editor", "contains")
    with pytest.raises(helper.HelperError):
        helper.name_matches("a", "a", "regex")


def test_helper_selector_and_window_checks_are_closed():
    good = {"by": "path", "name": "Plot", "control_type": "Button", "ancestry": [{"name": "p", "control_type": "Pane"}]}
    assert helper.check_selector(good)["name_match"] == "exact"
    assert helper.check_selector({"by": "automation_id", "automation_id": "x"})["control_type"] is None
    for bad in ({"by": "path", "name": "Plot"}, {"by": "xpath", "name": "a", "control_type": "Button"},
                {"by": "path", "name": "a", "control_type": "Button", "x": 1},
                {"by": "path", "name": "a", "control_type": "Nope"}, {"by": "automation_id"},
                {"by": "path", "name": "a", "control_type": "Button", "name_match": "contains"},
                {"by": "path", "name": "a", "control_type": "Button", "ancestry": [{"name": "x"}]}, "Plot", None):
        with pytest.raises(helper.HelperError):
            helper.check_selector(bad)
    assert helper.check_window(None) is None
    assert helper.check_window({"name": "Plot"})["match"] == "exact"
    for bad in ({"name": ""}, {"name": "x", "match": "regex"}, {"title": "x"}, "x"):
        with pytest.raises(helper.HelperError):
            helper.check_window(bad)
    assert helper.COMMANDS == ("find", "dump-tree", "invoke", "set-value", "read-value", "select-menu",
                               "focus", "state", "expand", "select")


def test_helper_ancestry_suffix_matching():
    chain = [{"name": "a", "control_type": "Pane"}, {"name": "b", "control_type": "Group"}]
    assert helper.ancestry_matches(chain, [])
    assert helper.ancestry_matches(chain, [{"name": "b", "control_type": "Group"}])
    assert helper.ancestry_matches(chain, chain)
    assert not helper.ancestry_matches(chain, [{"name": "a", "control_type": "Pane"}])
    assert not helper.ancestry_matches(chain[1:], chain)


def test_helper_cannot_address_by_title_alone():
    """Every command is routed through top_windows(), which filters on the target process id."""
    source = (PACKAGE / "l4" / "uia_helper.py").read_text(encoding="utf-8")
    assert "CreatePropertyCondition(P_PROCESS_ID, self.pid)" in source
    assert "FindWindow" not in source and "SendInput" not in source.replace("no SendInput", "")


# ---- launch precondition (pure parts of hidden_desktop) --------------------------------------------------

@pytest.fixture
def fake_app_root(tmp_path):
    template = tmp_path / "kicad-app" / "share" / "kicad" / "template"
    template.mkdir(parents=True)
    (template / "fp-lib-table").write_text("(fp_lib_table)\n")
    (template / "sym-lib-table").write_text("(sym_lib_table)\n")
    return tmp_path / "kicad-app"


def test_launch_env_block_is_redirected_stripped_and_verified(tmp_path, fake_app_root):
    from kicad_mcp.l4 import hidden_desktop as hd

    root = tmp_path / "profile"
    base = {"PATH": "C:\\x", "KICAD10_3RD_PARTY": "C:\\owner\\3rd", "KICAD9_3RD_PARTY": "C:\\owner\\old",
            "KICAD_API_SOCKET": "ipc://owner", "PYTHONHOME": "C:\\py", "TEMP": "C:\\owner\\temp",
            "KICAD_CONFIG_HOME": "C:\\owner\\cfg", "Appdata": "C:\\owner\\roaming"}
    env, redirected = hd.build_child_env(root, fake_app_root, base, settings_version="10.0")
    assert set(hd.required_redirects("10.0")) <= set(redirected)
    for name in hd.required_redirects("10.0"):
        assert Path(env[name]).resolve().is_relative_to(root.resolve()), name
    assert "KICAD9_3RD_PARTY" not in env and "KICAD_API_SOCKET" not in env and "PYTHONHOME" not in env
    assert env["KICAD10_3RD_PARTY"] == str(root / "documents" / "KiCad" / "10.0" / "3rdparty")
    assert env["PATH"] == "C:\\x"
    block = hd.build_env_block(env)
    assert block.endswith("\0\0")
    assert hd.verify_env_block(block, redirected, root)["TEMP"] == redirected["TEMP"]
    assert (root / "config" / "10.0" / "kicad_common.json").is_file()
    for mutate in (lambda e: e.update(TEMP="C:\\owner\\temp"), lambda e: e.pop("KICAD_CACHE_HOME"),
                   lambda e: e.update(KICAD9_3RD_PARTY="C:\\owner\\old"), lambda e: e.update(KICAD_API_SOCKET="x"),
                   lambda e: e.update(APPDATA=str(tmp_path / "elsewhere")), lambda e: e.pop(hd.NONCE_VARIABLE)):
        broken = dict(env)
        mutate(broken)
        with pytest.raises(hd.LaunchPreconditionError):
            hd.verify_env_block(hd.build_env_block(broken), redirected, root)


def test_env_block_roundtrip_keeps_drive_entries():
    from kicad_mcp.l4 import hidden_desktop as hd

    env = {"A": "1", "=C:": "C:\\dir", "B": "x=y"}
    assert hd.parse_env_block(hd.build_env_block(env)) == {"A": "1", "=C:": "C:\\dir", "B": "x=y"}


def test_missing_template_seeds_empty_tables(tmp_path):
    """A lite or nightly install ships no template library tables: the profile gets empty ones, as KiCad would."""
    from kicad_mcp.l4 import hidden_desktop as hd

    hd.seed_profile(tmp_path / "p", tmp_path / "no-kicad", "10.0")
    for name, head in (("fp-lib-table", "(fp_lib_table"), ("sym-lib-table", "(sym_lib_table")):
        table = next((tmp_path / "p").rglob(name))
        assert table.read_text(encoding="utf-8").startswith(head)


# ---- source scan: no subprocess startup-info desktop placement anywhere -----------------------------------

def _scanned_sources():
    files = [p for p in PACKAGE.rglob("*.py")] + [p for p in ROOT.glob("*.py")] + [p for p in ROOT.glob("*.pyw")]
    return [p for p in files if ".tmp" not in p.parts]


# The two private-desktop launchers: L4's (UI broker) and the adaptive editor's (live tests, GUI self-tests).
LAUNCHERS = {(PACKAGE / "l4" / "hidden_desktop.py").resolve(),
             (PACKAGE / "adaptive_editor" / "live" / "hidden_kicad.py").resolve()}


def test_no_subprocess_startupinfo_anywhere_and_lpdesktop_only_in_hidden_desktop():
    bare = re.compile(r"\bSTARTUPINFO\b")  # STARTUPINFOW (the ctypes struct) does not match
    desktop_assign = re.compile(r"\blpDesktop\s*=(?!=)")
    create_call = re.compile(r"\bCreateProcessW\s*\(")
    for path in _scanned_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not bare.search(text), f"{path}: uses a STARTUPINFO (no lpDesktop field; shows windows on the owner desktop)"
        if path.resolve() not in LAUNCHERS:
            assert not desktop_assign.search(text), f"{path}: sets lpDesktop outside the vetted launchers"
            assert not create_call.search(text), f"{path}: calls CreateProcessW outside the vetted launchers"
    for launcher in LAUNCHERS:  # each vetted launcher creates processes in exactly one place, on WinSta0\<private>
        hidden = launcher.read_text(encoding="utf-8")
        assert len(desktop_assign.findall(hidden)) == 1 and len(create_call.findall(hidden)) == 1, launcher
        assert "WinSta0" in hidden, launcher


def test_l4_package_starts_no_gui_process_except_through_the_desktop_launcher():
    forbidden = [re.compile(p) for p in (r"(?<![A-Za-z_])subprocess\.(Popen|run|call|check_output)\b",
                                         r"\bos\.(startfile|system|popen)\b", r"\bwebbrowser\b",
                                         r"CREATE_NEW_CONSOLE", r"ShellExecute", r"\bSendInput\b(?<!no SendInput)")]
    for path in (PACKAGE / "l4").glob("*.py"):
        text = path.read_text(encoding="utf-8").replace("no SendInput", "")
        for pattern in forbidden:
            assert not pattern.search(text), f"{path.name} matches {pattern.pattern}"


# =============================== live tests (real pcbnew, private desktop) ================================

MAKE_BOARD = r'''
import pcbnew, sys
b = pcbnew.BOARD()
for (x1, y1, x2, y2) in [(0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)]:
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
    s.SetStart(pcbnew.VECTOR2I_MM(x1, y1)); s.SetEnd(pcbnew.VECTOR2I_MM(x2, y2)); b.Add(s)
fp = pcbnew.FOOTPRINT(b); fp.SetReference(sys.argv[2]); fp.SetPosition(pcbnew.VECTOR2I_MM(10, 10)); b.Add(fp)
pcbnew.SaveBoard(sys.argv[1], b)
'''


def _app_root() -> Path | None:
    """KiCad for the live tier, found by discovery alone: the per-machine config and KICAD_APP_PATH are set
    aside (and restored), so whether these tests run never depends on that config."""
    import tempfile
    from kicad_mcp.adaptive_editor import kicad_env
    saved = {k: os.environ.pop(k, None) for k in ("KICAD_APP_PATH", "KICAD_AI_TOOLS_CONFIG")}
    os.environ["KICAD_AI_TOOLS_CONFIG"] = os.path.join(tempfile.gettempdir(), f"kicad-ai-tools-no-config-{os.getpid()}.json")
    kicad_env.kicad_app_root.cache_clear()
    try:
        return kicad_env.kicad_app_root()
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError):
        return None
    finally:
        kicad_env.kicad_app_root.cache_clear()
        os.environ.pop("KICAD_AI_TOOLS_CONFIG", None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


# KICAD_MCP_L4_APP_ROOT picks the install for the live tests (they build their board with its bundled Python)
LIVE_APP_ROOT = (Path(os.environ["KICAD_MCP_L4_APP_ROOT"]) if os.environ.get("KICAD_MCP_L4_APP_ROOT")
                 else _app_root() if sys.platform == "win32" else None)


def _live_reason() -> str | None:
    if sys.platform != "win32":
        return "Windows only"
    if os.environ.get("KICAD_MCP_L4_LIVE") == "0":
        return "KICAD_MCP_L4_LIVE=0"
    if LIVE_APP_ROOT is None or not (LIVE_APP_ROOT / "bin" / "pcbnew.exe").is_file():
        return f"no KiCad found by discovery (pcbnew.exe under {LIVE_APP_ROOT})"
    if not (LIVE_APP_ROOT / "bin" / "python.exe").is_file():
        return f"the KiCad at {LIVE_APP_ROOT} bundles no Python (nightly builds); set KICAD_MCP_L4_APP_ROOT to a release install"
    return None


live = pytest.mark.skipif(_live_reason() is not None, reason=str(_live_reason()))


def _build_project(directory: Path, reference: str) -> Path:
    from kicad_mcp.utils import secure_subprocess

    directory.mkdir(parents=True)
    script = directory.parent / f"make-{reference}.py"
    script.write_text(MAKE_BOARD, encoding="utf-8")
    board = directory / "synthetic.kicad_pcb"
    done = secure_subprocess.run_contained(
        [str(LIVE_APP_ROOT / "bin" / "python.exe"), "-I", str(script), str(board), reference],
        timeout=180, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0 and board.is_file(), done.stderr
    return directory


def _tree(directory: Path) -> dict:
    return broker.tree_hashes(directory)


def _pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == 259
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@pytest.fixture(scope="session")
def live_project(tmp_path_factory):
    return _build_project(tmp_path_factory.mktemp("l4-live") / "project-a", "U1")


@pytest.fixture(scope="session")
def recorded(tmp_path_factory, live_project):
    """Record identifiers once (three real launches) into a temporary registry file."""
    base = tmp_path_factory.mktemp("l4-record")
    out = base / "ui-registry-10.0.5.json"
    result = broker.record_identifiers(live_project, registry_out=out, evidence_root=base / "evidence",
                                       app_root=LIVE_APP_ROOT)  # the live install, before any per-test fixture
    return {"result": result, "path": out, "base": base}


def _env_for(recorded_registry) -> dict:
    return {APPROVAL_ENV: recorded_registry["result"]["registry_sha256"]}


def _assert_clean_run(result, project, before):
    assert result["error"] is None, result["error"]
    assert result["ok"] is True and result["status"] == "ok" and result["launched"] is True
    assert result["original_unchanged"] is True and _tree(project) == before
    guard = result["monitor"]
    assert guard["ok"] is True, guard["violations"]
    assert guard["task_windows_on_input_desktop"] == 0          # zero task windows reached the input desktop
    assert guard["input_desktop_samples"] > 0                   # ... and the monitor really sampled it
    assert guard["foreground_samples_attributed_to_task"] == 0
    assert guard["wts_differences"] == []                       # WTS console state unchanged
    assert result["session"]["placement"]["placed_correctly"] is True
    assert not _pid_alive(result["kicad"]["pid"])               # reaped by exact PID
    assert Path(result["evidence_dir"]).is_dir()                # evidence kept


@l4_live
@live
def test_placement_check_places_child_on_private_desktop(tmp_path):
    from kicad_mcp.l4 import hidden_desktop as hd

    result = hd.placement_check(tmp_path / "placement")
    assert result["placed_correctly"] is True, result
    assert result["child_desktop"] == result["expected"] and result["child_desktop"].startswith("l4-verify-")


@l4_live
@live
def test_record_identifiers_three_launch_stability(recorded):
    result = recorded["result"]
    assert result["error"] is None, result["error"]
    assert result["ok"] is True and result["launches_completed"] == 3 and result["original_unchanged"] is True
    assert all(s["monitor_ok"] for s in result["launch_summaries"])
    raw = recorded["path"].read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result["registry_sha256"] and b"\r" not in raw
    loaded = registry.load_registry(recorded["path"], _env_for(recorded), kicad_build=broker.detect_kicad_build(LIVE_APP_ROOT))
    admitted = loaded.operation_names()
    assert "pcb_open_and_save" in admitted and "pcb_export_gerbers_dialog" in admitted, result["report"]["dropped"]
    # independently re-derive every admitted selector from the three stored probes
    probes = [json.loads((Path(result["evidence_dir"]) / f"launch-{i}" / "probe.json").read_text(encoding="utf-8"))
              for i in (1, 2, 3)]
    pids = {p["session"]["stopped"]["kicad"]["pid"] for p in probes}
    assert len(pids) == 3, "the three observations must come from three independent launches"
    for name in admitted:
        for ref, selector in loaded.operation(name)["controls"].items():
            again, _why = registry.choose_selector([p["ops"][name]["controls"][ref] for p in probes])
            assert again == {k: v for k, v in selector.items() if k != "dialog"}, (name, ref)
    assert isinstance(result["report"]["dropped"], list)  # dropped identifiers are reported, even if empty


@l4_live
@live
def test_open_and_save_succeeds_and_original_stays_byte_identical(recorded, live_project, tmp_path):
    before = _tree(live_project)
    result = broker.ui_fallback("pcb_open_and_save", live_project, {}, registry_path=recorded["path"],
                                evidence_root=tmp_path, approval_env=_env_for(recorded))
    _assert_clean_run(result, live_project, before)
    assert result["postcondition"]["rewritten"] is True and result["postcondition"]["kicad_cli_load"]["loads"] is True


@l4_live
@live
def test_export_gerbers_succeeds_and_writes_into_the_clone_only(recorded, live_project, tmp_path):
    before = _tree(live_project)
    result = broker.ui_fallback("pcb_export_gerbers_dialog", live_project, {"output_dir": "gerbers"},
                                registry_path=recorded["path"], evidence_root=tmp_path,
                                approval_env=_env_for(recorded))
    _assert_clean_run(result, live_project, before)
    files = result["postcondition"]["files"]
    assert files and all(f["name"].lower().endswith(".gbr") and f["size"] > 0 for f in files)
    clone = Path(result["clone_dir"]).resolve()
    assert all(Path(result["postcondition"]["directory"]).resolve().is_relative_to(clone) for _ in files)
    assert not (live_project / "gerbers").exists()


@l4_live
@live
def test_board_setup_read_navigates_to_its_page_and_is_read_only(recorded, live_project, tmp_path):
    loaded = registry.load_registry(recorded["path"], _env_for(recorded), skip_build_check=True)
    assert "pcb_board_setup_read" in loaded.operation_names(), recorded["result"]["report"]["dropped"]
    before = _tree(live_project)
    result = broker.ui_fallback("pcb_board_setup_read", live_project, {}, registry_path=recorded["path"],
                                evidence_root=tmp_path, approval_env=_env_for(recorded))
    _assert_clean_run(result, live_project, before)
    assert result["outputs"]["copper_layers"] == "2"  # the synthetic board has two copper layers
    assert result["postcondition"]["file_unchanged"] is True


@l4_live
@live
def test_the_shipped_registry_reads_board_setup(live_project, tmp_path):
    before = _tree(live_project)
    result = broker.ui_fallback("pcb_board_setup_read", live_project, {}, evidence_root=tmp_path,
                                approval_env={})  # the shipped registry, approved by APPROVED.sha256 alone
    _assert_clean_run(result, live_project, before)
    assert result["outputs"]["copper_layers"] == "2"


def _settled_titles(kicad, timeout_s: float = 120.0) -> list[str]:
    """The decoy's window titles once it has finished loading: its PCB Editor window is up, no load progress
    window is left, and two samples a second apart agree (a slow machine can still be loading at start)."""
    import time as _time
    end, previous = _time.time() + timeout_s, None
    while _time.time() < end:
        titles = sorted(w["title"] for w in kicad.hidden_windows()
                        if w["title"] not in ("MSCTFIME UI", "Default IME"))  # input-method helpers, not KiCad state
        loaded = any("PCB Editor" in title for title in titles) and not any("Load PCB" in title for title in titles)
        if loaded and titles == previous:
            return titles
        previous = titles if loaded else None
        _time.sleep(1.0)
    raise AssertionError(f"the decoy did not settle within {timeout_s}s: {previous}")


@l4_live
@live
def test_decoy_second_instance_is_never_targeted(recorded, live_project, tmp_path):
    from kicad_mcp.l4 import hidden_desktop as hd

    decoy_project = _build_project(tmp_path / "decoy-project", "DECOY1")
    decoy_board = decoy_project / "synthetic.kicad_pcb"
    decoy_hash = hashlib.sha256(decoy_board.read_bytes()).hexdigest()
    decoy = hd.HiddenKiCad(decoy_board, tmp_path / "decoy-profile")
    # The decoy is launched outside the broker, so it gets its own continuous whole-tree watchdog for the
    # whole test (not only the start-up tripwire); any window, foreground change or blind sample fails it.
    decoy_pids, decoy_violations = monitor.TaskPids(), []
    watchdog = monitor.OwnerDesktopWatchdog(decoy_pids, lambda kind, detail: decoy_violations.append((kind, detail)))
    watchdog.start()
    try:
        decoy.start(on_created=decoy_pids.add)
        assert decoy.alive()
        before_titles = _settled_titles(decoy)  # after loading: no transient "Load PCB" progress window
        before = _tree(live_project)
        result = broker.ui_fallback("pcb_open_and_save", live_project, {}, registry_path=recorded["path"],
                                    evidence_root=tmp_path / "ev", approval_env=_env_for(recorded))
        _assert_clean_run(result, live_project, before)
        assert result["kicad"]["pid"] != decoy.pid and result["kicad"]["desktop"] != decoy.desktop_name
        assert decoy.alive(), "the decoy instance must survive the run untouched"
        assert hashlib.sha256(decoy_board.read_bytes()).hexdigest() == decoy_hash
        assert _settled_titles(decoy) == before_titles
        assert str(decoy.pid) not in json.dumps(result["steps"])
    finally:
        decoy.close()
        watched = watchdog.stop()
    assert decoy_violations == [] and watched["window_hits"] == []
    assert watched["input_desktop_samples"] > 0 and not watched.get("input_desktop_unavailable")


@l4_live
@live
def test_unapproved_registry_refuses_without_launching(recorded, live_project, tmp_path):
    before = _tree(live_project)
    for env in ({}, {APPROVAL_ENV: "0" * 64}):
        result = broker.ui_fallback("pcb_open_and_save", live_project, {}, registry_path=recorded["path"],
                                    evidence_root=tmp_path / "ev", approval_env=env)
        _refused(result, "registry_unapproved")
    assert not (tmp_path / "ev").exists() and _tree(live_project) == before


@pytest.mark.parametrize("module", ["broker", "uia_helper"])
def test_channel_files_survive_a_brief_sharing_violation(module):
    import importlib
    import time as _time
    mod = importlib.import_module(f"kicad_mcp.l4.{module}")
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:  # a scanner holds the freshly renamed file twice
            raise PermissionError(13, "sharing violation")
        return "read"

    assert mod.with_sharing_retry(flaky) == "read" and len(attempts) == 3

    def always_locked():
        raise PermissionError(13, "sharing violation")

    started = _time.monotonic()
    with pytest.raises(PermissionError):  # a lasting lock still fails, within the window
        mod.with_sharing_retry(always_locked, window_s=0.3)
    assert 0.25 <= _time.monotonic() - started < 0.6  # Windows time.time() ticks are coarse


def _held(monkeypatch, target: Path, method: str, times: int):
    """Make ``target.<method>`` fail with a sharing violation ``times`` times (an indexer holding it)."""
    real = getattr(Path, method)
    left = {"n": times}

    def guarded(self, *args, **kwargs):
        if self == target and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(13, "sharing violation")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, guarded)
    return left


def test_a_reply_file_held_briefly_still_reaches_the_broker(tmp_path, monkeypatch):
    channel = tmp_path / "uia-channel"
    channel.mkdir()
    reply = channel / "reply-000001.json"
    reply.write_text(json.dumps({"id": 1, "ok": True, "result": {"answer": 42}}), encoding="utf-8")
    left = _held(monkeypatch, reply, "read_text", 2)
    proc = type("Proc", (), {"alive": lambda self: True})()
    assert broker.UiaClient(channel, proc).call("probe", timeout_s=10) == {"answer": 42}
    assert left["n"] == 0  # the real read path met the violation and retried through it


def test_a_held_command_file_is_answered_once_and_never_run_twice(tmp_path, monkeypatch):
    uia_helper = helper
    monkeypatch.setattr(uia_helper, "SHARING_RETRY_S", 0.2)
    channel = tmp_path / "uia-channel"
    channel.mkdir()
    command = channel / "cmd-000001.json"
    command.write_text(json.dumps({"id": 1, "cmd": "invoke", "args": {}}), encoding="utf-8")
    runs = []
    uia = type("Uia", (), {"execute": lambda self, cmd, args: runs.append(cmd) or {"done": True}})()
    _held(monkeypatch, command, "unlink", 10 ** 6)  # cannot be removed for a while
    done = set()
    for _ in range(3):  # three passes of the helper loop while the file is still there
        uia_helper.handle_command(channel, command, uia, done)
    assert runs == ["invoke"]  # executed once, e.g. no second Plot or Save
    assert json.loads((channel / "reply-000001.json").read_text(encoding="utf-8"))["ok"] is True
    monkeypatch.undo()
    uia_helper.handle_command(channel, command, uia, done)  # released: removed, still not run again
    assert not command.exists() and runs == ["invoke"]


def test_an_unresolvable_kicad_is_a_typed_refusal_not_an_exception(tmp_path, registry_file, approved, text_project,
                                                                  nothing_may_launch, monkeypatch):
    from kicad_mcp.adaptive_editor import kicad_env
    monkeypatch.delenv("KICAD_APP_PATH", raising=False)

    def broken():
        raise kicad_env.ConfigError("cannot read the per-machine config")

    monkeypatch.setattr(kicad_env, "kicad_app_root", broken)
    result = broker.ui_fallback("pcb_open_and_save", text_project, None, registry_path=registry_file,
                                evidence_root=tmp_path / "evidence", approval_env=approved)
    assert result["status"] == "refused" and result["error"]["code"] == "config_invalid"
    recorded = broker.record_identifiers(text_project, evidence_root=tmp_path / "evidence")
    assert recorded["status"] == "refused" and recorded["error"]["code"] == "config_invalid"


def test_close_stops_kicad_and_the_watchdog_even_after_an_interrupt(tmp_path):
    session = object.__new__(broker.HiddenSession)
    steps = []

    class Helper:
        pid = 1

        def wait(self, _ms):
            raise KeyboardInterrupt  # e.g. Ctrl+C while waiting for the helper to stop

        def kill(self):
            steps.append("helper_kill")

        def exit_code(self):
            raise OSError("handle gone")

        def close(self):
            steps.append("helper_close")

    class KiCad:
        def identity(self):
            return {"pid": 2}

        def close(self):
            steps.append("kicad_close")

    class Watchdog:
        def stop(self):
            steps.append("watchdog_stop")
            return {"input_desktop_samples": 1, "window_hits": [],
                    "foreground": {"samples": 0, "changes": 0, "attributed": []}}

    session._closed, session._channel = False, tmp_path
    session.helper_proc, session.kicad, session.watchdog = Helper(), KiCad(), Watchdog()
    session.extra_violations, session.cleanup_errors = [], []
    session.wts_before, session.placement = broker.monitor.wts_snapshot(), {}
    session.close()
    assert steps == ["helper_kill", "helper_close", "kicad_close", "watchdog_stop"]
    assert {e["step"] for e in session.cleanup_errors} == {"helper_stop_request", "helper_exit_code"}
    assert session.summary["stopped"]["kicad"] == {"pid": 2}


def test_a_dynamic_automation_id_is_never_a_cancel_selector():
    for dynamic in ("-31999", "12345", "5100.0", " 5101", "", "05101"):
        with pytest.raises(RegistryRefusal):
            registry._cancel({"automation_id": dynamic, "control_type": "Button"}, "dialogs.plot.cancel")
    registry._cancel({"automation_id": "5101", "control_type": "Button"}, "dialogs.plot.cancel")  # wxID_CANCEL
    registry._cancel({"automation_id": "wxID_CANCEL", "control_type": "Button"}, "dialogs.plot.cancel")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation")
def test_every_private_desktop_launch_is_windowless_and_crash_quiet(monkeypatch):
    from kicad_mcp.l4 import hidden_desktop as hd
    seen = {}

    def fake_create(_app, _cmd, _pa, _ta, _inherit, flags, _env, _cwd, _startup, _info):
        seen["flags"] = flags
        seen["error_mode"] = hd.kernel32.SetErrorMode(hd.SEM_FLAGS)  # the mode in force during creation
        hd.kernel32.SetErrorMode(seen["error_mode"])
        return 0

    monkeypatch.setattr(hd.kernel32, "CreateProcessW", fake_create)
    with pytest.raises(OSError):
        hd.spawn_windowless_on_desktop("-c pass", "no-such-desktop", ".", "probe")
    assert seen["flags"] & hd.CREATE_NO_WINDOW
    assert seen["error_mode"] & hd.SEM_FLAGS == hd.SEM_FLAGS  # children inherit: no crash/error dialogs


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
def test_the_tripwire_follows_the_whole_process_tree():
    import subprocess
    from kicad_mcp.l4 import hidden_desktop as hd
    from kicad_mcp.utils import winproc
    grandchild = "import time; time.sleep(30)"
    child = (f"import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
             "creationflags=0x08000000); print(p.pid, flush=True); time.sleep(30)")
    with winproc.Tree() as tree:  # owned by a Job Object: cleanup ends exactly this tree, nothing else
        parent = tree.spawn([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
        grand_pid = int(parent.stdout.readline())
        assert {parent.pid, grand_pid} <= hd.tree_pids([parent.pid])
    parent.wait(10)


# --- the tripwires fail closed: a desktop that cannot be observed stops the task --------------------

def test_l4_tripwire_fails_closed_when_the_owner_desktop_cannot_be_observed(monkeypatch):
    from kicad_mcp.l4 import hidden_desktop as hd
    monkeypatch.setattr(hd, "input_desktop_windows", lambda: None)  # e.g. a locked workstation
    with pytest.raises(hd.OwnerDesktopUnobservable):
        hd.owner_windows_of(os.getpid())
    kicad = object.__new__(hd.HiddenKiCad)
    killed = []
    kicad.proc = type("Proc", (), {"pid": os.getpid(), "pids": lambda self: [os.getpid()]})()
    kicad.alive = lambda: True
    kicad.kill = lambda: killed.append(True)
    with pytest.raises(hd.OwnerDesktopUnobservable):
        kicad._tripwire(5)
    assert killed == [True]  # the task was stopped, not left running blind


def test_the_broker_verdict_fails_on_any_blind_monitor_sample():
    verdict = monitor.build_verdict(input_desktop_samples=500, window_hits=[], foreground={}, wts_diffs=[],
                                    input_desktop_unavailable=1)
    assert verdict["ok"] is False and verdict["violations"][0]["kind"] == "input_desktop_unobservable"
    clean = monitor.build_verdict(input_desktop_samples=500, window_hits=[], foreground={}, wts_diffs=[])
    assert clean["ok"] is True


def test_a_stranger_holding_a_reused_parent_pid_is_never_a_descendant():
    # 10 is ours (created at t=100); 11 is our child (t=120); 12 is a stranger whose real parent died long
    # ago and whose recorded parent PID 10 was then reused by our process (stranger created at t=50).
    rows = [(10, 1), (11, 10), (12, 10), (13, 11)]
    created = {10: 100, 11: 120, 12: 50, 13: 130}.get
    assert monitor.descendant_pids([10], rows, created) == {10, 11, 13}
    assert monitor.descendant_pids([10], rows) == {10, 11, 12, 13}  # without creation times: the old risk


def test_the_broker_watchdog_stops_the_task_the_moment_it_is_blind(monkeypatch):
    import threading
    monkeypatch.setattr(monitor, "input_desktop_windows", lambda: None)  # e.g. the workstation locked
    monkeypatch.setattr(monitor, "process_table", lambda: [])
    monkeypatch.setattr(monitor, "pid_creation_time", lambda pid: 1)
    monkeypatch.setattr(monitor, "foreground_sample", lambda: {"t": 0, "hwnd": 0, "pid": 0})
    fired = threading.Event()
    seen = []
    pids = monitor.TaskPids()
    pids.add(4242)
    watchdog = monitor.OwnerDesktopWatchdog(pids, lambda kind, detail: (seen.append(kind), fired.set()))
    watchdog.start()
    assert fired.wait(5)  # stopped at once, not only failed at the end
    report = watchdog.stop()
    assert seen == ["input_desktop_unobservable"] and report["input_desktop_unavailable"] >= 1


def test_a_failing_precondition_is_a_typed_refusal(tmp_path, registry_file, approved, text_project,
                                                   nothing_may_launch, monkeypatch):
    import subprocess as sp

    def cli_hangs(_root):
        raise sp.TimeoutExpired(["kicad-cli", "version"], 60)

    monkeypatch.setattr(broker, "detect_kicad_build", cli_hangs)
    result = broker.ui_fallback("pcb_open_and_save", text_project, None, registry_path=registry_file,
                                evidence_root=tmp_path / "evidence", approval_env=approved, app_root=tmp_path / "kicad")
    assert result["status"] == "refused" and result["error"]["code"] == "precondition_failed"
    monkeypatch.setattr(broker, "detect_kicad_build", lambda _root: (_ for _ in ()).throw(OSError("unreadable")))
    recorded = broker.record_identifiers(text_project, evidence_root=tmp_path / "evidence", app_root=tmp_path / "kicad")
    assert recorded["status"] == "refused" and recorded["error"]["code"] == "precondition_failed"


def test_a_failing_verdict_still_gives_a_typed_failed_summary(tmp_path, monkeypatch):
    session = object.__new__(broker.HiddenSession)
    session._closed, session._channel = False, tmp_path
    session.helper_proc = session.kicad = session.watchdog = None
    session.extra_violations, session.cleanup_errors, session.placement = [], [], {}
    session.wts_before = {}
    monkeypatch.setattr(broker.monitor, "wts_snapshot", lambda: (_ for _ in ()).throw(OSError("WTS unavailable")))
    session.close()
    assert session.summary["monitor"]["ok"] is False
    assert session.summary["monitor"]["violations"][0]["kind"] == "monitor_verdict_failed"


def test_kicads_job_is_watched_from_the_moment_it_exists():
    session = object.__new__(broker.HiddenSession)
    session.pids = monitor.TaskPids()
    owned = type("Owned", (), {"pids": lambda self: [7, 8, 9]})()
    session.kicad = type("K", (), {"proc": owned})()
    session._register_kicad(7)  # what HiddenKiCad.start calls before its start-up tripwire
    assert session.pids.snapshot() == {7, 8, 9}


def test_post_run_filesystem_failures_are_recorded_not_raised(tmp_path, monkeypatch):
    result = {"error": None}
    monkeypatch.setattr(broker, "tree_hashes", lambda project: (_ for _ in ()).throw(OSError("project vanished")))
    assert broker._hashes_after_run(tmp_path, result) is None  # never equal to the before-hashes: the run fails
    assert result["post_run_errors"][0]["step"] == "hash_original"
    monkeypatch.setattr(broker, "_write_json", lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
    broker._write_result(tmp_path, result)
    assert "disk full" in result["result_json_error"]



def test_kicad_close_closes_its_job_and_desktop_even_if_stopping_is_interrupted():
    from kicad_mcp.l4 import hidden_desktop as hd
    kicad = object.__new__(hd.HiddenKiCad)
    closed = []

    def interrupted():
        raise KeyboardInterrupt

    kicad.proc = type("Proc", (), {"kill_descendants": lambda self: interrupted(), "kill": lambda self: None,
                                   "close": lambda self: closed.append("job")})()
    kicad.desktop = type("Desk", (), {"close": lambda self: closed.append("desktop")})()
    with pytest.raises(KeyboardInterrupt):
        kicad.close()
    assert closed == ["job", "desktop"]  # the job (KILL_ON_JOB_CLOSE) and the desktop are closed regardless



@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation")
def test_a_failed_job_never_leaves_a_suspended_process_behind(tmp_path, monkeypatch):
    from kicad_mcp.l4 import hidden_desktop as hd

    def no_job():
        raise OSError("CreateJobObjectW failed")

    monkeypatch.setattr(hd, "Job", no_job)
    with hd.PrivateDesktop() as desk:
        info = hd.create_process_on_desktop(f'"{sys.executable}" -c "import time; time.sleep(60)"', desk.name,
                                            cwd=str(tmp_path))
        pid = int(info.dwProcessId)
        with pytest.raises(OSError):
            hd.OwnedProcess(info, "probe")
        handle = hd.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:  # a process object may linger briefly while handles close; it must be signalled (ended)
            try:
                assert hd.kernel32.WaitForSingleObject(handle, 5000) == 0
            finally:
                hd.kernel32.CloseHandle(handle)



def test_a_containment_failure_before_launch_is_a_typed_refusal(tmp_path, registry_file, approved, text_project,
                                                               nothing_may_launch, monkeypatch):
    def not_contained(_root):
        raise RuntimeError("Could not resume contained process")  # what run_contained raises

    monkeypatch.setattr(broker, "detect_kicad_build", not_contained)
    result = broker.ui_fallback("pcb_open_and_save", text_project, None, registry_path=registry_file,
                                evidence_root=tmp_path / "evidence", approval_env=approved, app_root=tmp_path / "kicad")
    assert result["status"] == "refused" and result["error"]["code"] == "precondition_failed"
    recorded = broker.record_identifiers(text_project, evidence_root=tmp_path / "evidence", app_root=tmp_path / "kicad")
    assert recorded["status"] == "refused" and recorded["error"]["code"] == "precondition_failed"
