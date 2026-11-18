"""The IPC layer without KiCad: candidate copies, schema loading and request building."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kicad_mcp.ipc import api, schema
from kicad_mcp.ipc.session import Candidate, SessionError

PROTO_TYPES = """
syntax = "proto3";
package kiapi.common.types;
enum DocumentType { DOCTYPE_UNKNOWN = 0; DOCTYPE_SCHEMATIC = 1; DOCTYPE_PCB = 3; }
message ProjectSpecifier { string name = 1; string path = 2; }
message DocumentSpecifier { DocumentType type = 1; ProjectSpecifier project = 2; }
message KIID { string value = 1; }
message ItemHeader { DocumentSpecifier document = 1; KIID container = 2; }
message RunJobSettings { DocumentSpecifier document = 1; string output_path = 2; }
"""
PROTO_COMMANDS = """
syntax = "proto3";
package kiapi.common.commands;
import "common/types/base_types.proto";
message GetThing { kiapi.common.types.DocumentSpecifier document = 1; int32 count = 2; }
message EditThing { kiapi.common.types.ItemHeader header = 1; repeated kiapi.common.types.KIID ids = 2;
                    map<string, string> vars = 3; }
message RunThingJob { kiapi.common.types.RunJobSettings job_settings = 1; }
"""


@pytest.fixture
def test_schema(tmp_path: Path, monkeypatch) -> schema.Schema:
    protos = tmp_path / "proto"
    (protos / "common" / "types").mkdir(parents=True)
    (protos / "common" / "commands").mkdir(parents=True)
    (protos / "common" / "types" / "base_types.proto").write_text(PROTO_TYPES, encoding="utf-8")
    (protos / "common" / "commands" / "thing_commands.proto").write_text(PROTO_COMMANDS, encoding="utf-8")
    monkeypatch.setenv("KICAD_AI_TOOLS_CACHE", str(tmp_path / "cache"))
    try:
        schema.find_protoc()
    except schema.SchemaError:
        pytest.skip("protoc not installed")
    commit = "a" * 40
    return schema.Schema(commit, schema.descriptor_set(commit, protos))


def test_schema_resolves_and_builds_strictly(test_schema: schema.Schema) -> None:
    request = api.build_request(test_schema, "GetThing", {"count": 3})
    assert request.count == 3
    with pytest.raises(api.ApiCallError) as refused:
        api.build_request(test_schema, "GetThing", {"bogus": 1})
    assert refused.value.status == "bad_request"
    with pytest.raises(api.ApiCallError) as unknown:
        api.resolve(test_schema, "NoSuchCommand")
    assert unknown.value.status == "unknown_command"
    edit = api.build_request(test_schema, "EditThing", {"ids": [{"value": "x"}], "vars": {"A": "1"}})
    assert edit.vars["A"] == "1" and edit.ids[0].value == "x"


def test_describe_lists_fields_and_enums(test_schema: schema.Schema) -> None:
    described = api.describe(test_schema, "GetThing")
    document = described["fields"]["document"]
    assert document["type"] == "kiapi.common.types.DocumentSpecifier"
    assert "DOCTYPE_PCB" in document["schema"]["fields"]["type"]["values"]


def test_document_fill_top_level_header_and_nested(test_schema: schema.Schema) -> None:
    document = test_schema.new("kiapi.common.types.DocumentSpecifier")
    document.type = 3
    document.project.name = "demo"
    get = api.fill_document(api.build_request(test_schema, "GetThing", {}), document)
    assert get.document.project.name == "demo"
    edit = api.fill_document(api.build_request(test_schema, "EditThing", {}), document)
    assert edit.header.document.type == 3
    job = api.fill_document(api.build_request(test_schema, "RunThingJob", {"job_settings": {"output_path": "o"}}),
                            document)
    assert job.job_settings.document.project.name == "demo" and job.job_settings.output_path == "o"
    explicit = api.build_request(test_schema, "GetThing", {"document": {"type": "DOCTYPE_SCHEMATIC"}})
    assert api.fill_document(explicit, document).document.type == 1  # an explicit document is kept


def _project(tmp_path: Path) -> Path:
    src = tmp_path / "proj"
    (src / "lib.pretty").mkdir(parents=True)
    (src / "lib.pretty" / "R.kicad_mod").write_text("(footprint R)", encoding="utf-8")
    for name, body in {"demo.kicad_pro": "{}", "demo.kicad_sch": "(kicad_sch)", "demo.kicad_pcb": "(kicad_pcb)",
                       "lib.kicad_sym": "(kicad_symbol_lib)", "notes.txt": "not copied"}.items():
        (src / name).write_text(body, encoding="utf-8")
    return src


def test_candidate_copies_project_and_libraries(tmp_path: Path) -> None:
    candidate = Candidate.of(_project(tmp_path) / "demo.kicad_sch", tmp_path / "work")
    names = {p.name for p in candidate.work_dir.iterdir()}
    assert {"demo.kicad_pro", "demo.kicad_sch", "demo.kicad_pcb", "lib.kicad_sym", "lib.pretty"} <= names
    assert "notes.txt" not in names
    assert candidate.changed_files() == []


def test_promote_writes_changes_with_backup_and_skips_locks_and_libraries(tmp_path: Path) -> None:
    src = _project(tmp_path)
    candidate = Candidate.of(src / "demo.kicad_sch", tmp_path / "work")
    (candidate.work_dir / "demo.kicad_sch").write_text("(kicad_sch edited)", encoding="utf-8")
    (candidate.work_dir / "~demo.kicad_pro.lck").write_text("lock", encoding="utf-8")
    (candidate.work_dir / "lib.kicad_sym").write_text("(changed lib)", encoding="utf-8")
    result = candidate.promote(tmp_path / "backups")
    assert [w["file"] for w in result["written"]] == ["demo.kicad_sch"]
    assert (src / "demo.kicad_sch").read_text(encoding="utf-8") == "(kicad_sch edited)"
    assert (Path(result["backup_dir"]) / "demo.kicad_sch").read_text(encoding="utf-8") == "(kicad_sch)"
    assert not (src / "~demo.kicad_pro.lck").exists()
    assert (src / "lib.kicad_sym").read_text(encoding="utf-8") == "(kicad_symbol_lib)"
    assert candidate.changed_files() == []  # promoted bytes are the new baseline
    (candidate.work_dir / "demo.kicad_sch").write_text("(kicad_sch edited twice)", encoding="utf-8")
    assert [w["file"] for w in candidate.promote(tmp_path / "backups2")["written"]] == ["demo.kicad_sch"]


def test_promote_refuses_when_the_original_changed(tmp_path: Path) -> None:
    src = _project(tmp_path)
    candidate = Candidate.of(src / "demo.kicad_sch", tmp_path / "work")
    (candidate.work_dir / "demo.kicad_sch").write_text("(mine)", encoding="utf-8")
    (src / "demo.kicad_sch").write_text("(someone else's edit)", encoding="utf-8")
    with pytest.raises(SessionError, match="originals changed"):
        candidate.promote(tmp_path / "backups")
    assert (src / "demo.kicad_sch").read_text(encoding="utf-8") == "(someone else's edit)"


def test_candidate_refuses_a_document_outside_its_project(tmp_path: Path) -> None:
    with pytest.raises(SessionError):
        Candidate.of(_project(tmp_path) / "missing.kicad_sch", tmp_path / "work")


def _sleepers(*folders: Path):
    import subprocess

    return [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(folder / "m")],
                             creationflags=subprocess.CREATE_NO_WINDOW) for folder in folders]


def _reap(procs) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process table")
def test_stop_strays_stops_only_processes_naming_our_folders(tmp_path: Path) -> None:
    import time

    from kicad_mcp.ipc.session import stop_strays

    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    since = time.time()
    procs = mine, other = _sleepers(ours, theirs)
    try:
        time.sleep(1)
        stopped = stop_strays(ours, image_root=Path(sys.executable).parent, since=since)
        time.sleep(1)
        assert mine.pid in stopped and mine.poll() is not None
        assert other.pid not in stopped and other.poll() is None
    finally:
        _reap(procs)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process table")
def test_stop_strays_needs_image_root_and_start_time_evidence(tmp_path: Path) -> None:
    """A command-line match alone is never enough: the image must lie under the session's KiCad installation and
    the process must not predate the session."""
    import time

    from kicad_mcp.ipc.session import stop_strays

    ours = tmp_path / "ours"
    ours.mkdir()
    procs = [mine] = _sleepers(ours)
    try:
        time.sleep(1)
        assert stop_strays(ours) == []  # no evidence given: nothing stopped
        assert stop_strays(ours, image_root=tmp_path, since=0) == []  # wrong installation
        assert stop_strays(ours, image_root=Path(sys.executable).parent, since=time.time() + 60) == []  # too old
        assert stop_strays(ours, image_root=Path(Path(sys.executable).anchor), since=0) == []  # a drive root
        assert mine.poll() is None
    finally:
        _reap(procs)


def test_every_process_launch_is_windowless():
    """Silent-launch rule: each subprocess call in the IPC and coverage modules passes a no-window flag
    (or is the POSIX branch, which has no windows), so no console ever flashes on the user's screen."""
    import ast

    root = Path(__file__).resolve().parents[1] / "kicad_mcp"
    files = [*(root / "ipc").glob("*.py"), *(root / "coverage").glob("*.py"),
             root / "tools" / "ipc_tools.py", root / "utils" / "winproc.py"]
    loud = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run", "Popen", "call", "check_output", "check_call")
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                keywords = {k.arg for k in node.keywords}
                if not keywords & {"creationflags", "start_new_session"}:
                    loud.append(f"{path.name}:{node.lineno}")
    assert not loud, f"process launches without CREATE_NO_WINDOW: {loud}"
