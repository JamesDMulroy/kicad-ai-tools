"""Guards added in the open-source security review: embedded-file names stay inside dest_dir, and the
external BOM generator needs the host's own opt-in, not only the client's allow_execute flag."""
from __future__ import annotations

import pytest

from kicad_mcp.ipc.session import SessionError
from kicad_mcp.tools import function_fallbacks as ff


@pytest.mark.parametrize("name", ["C:evil.txt", "a.txt:stream", "..", ".", "", "CON", "nul.txt", "bad\x01.txt",
                                  "trailing."])
def test_unsafe_embedded_names_are_refused(name):
    with pytest.raises(SessionError):
        ff._safe_name(name)


@pytest.mark.parametrize("name, leaf", [("note.txt", "note.txt"), ("sub/dir/data.bin", "data.bin"),
                                        ("..\\..\\x.step", "x.step")])
def test_embedded_names_reduce_to_a_plain_leaf(name, leaf):
    assert ff._safe_name(name) == leaf


def test_bom_generator_needs_the_client_flag(monkeypatch):
    monkeypatch.setenv("KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR", "1")
    with pytest.raises(SessionError, match="allow_execute"):
        ff.run_bom_generator(object(), "python x.py %I %O")


def test_bom_generator_needs_the_host_opt_in(monkeypatch):
    monkeypatch.delenv("KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR", raising=False)
    with pytest.raises(SessionError, match="KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR"):
        ff.run_bom_generator(object(), "python x.py %I %O", allow_execute=True)


def test_bom_generator_fills_placeholders_per_argument(tmp_path):
    """A quote or space in a path stays inside its own argument."""
    project = tmp_path / 'odd "dir" name'
    argv = ff.generator_argv('python "bom.py" "%I" "%O.csv"', project / "n.xml", project / "out", project)
    assert argv == ["python", "bom.py", str(project / "n.xml"), (str(project / "out") + ".csv").replace("\\", "/")]


def test_bom_generator_never_falls_back_to_the_server_interpreter(tmp_path):
    class Session:
        app_root = tmp_path  # no bin/python here

    with pytest.raises(SessionError, match="python"):
        ff._kicad_python(Session())


def _zip(*members: str) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in members:
            archive.writestr(name, "syntax = 'proto3';\n")
    return buffer.getvalue()


@pytest.mark.parametrize("member", ["kicad-x/api/proto/../evil.proto", "kicad-x/api/proto//abs.proto",
                                    "kicad-x/api/proto/C:/evil.proto",  # portability-ok: a hostile archive member
                                    r"kicad-x/api/proto/\\host\share\e.proto",
                                    r"kicad-x/api/proto/..\evil.proto"])
def test_proto_archive_members_cannot_escape_the_destination(tmp_path, member):
    from kicad_mcp.ipc.schema import SchemaError, extract_protos

    with pytest.raises(SchemaError, match="unsafe"):
        extract_protos(_zip("kicad-x/api/proto/common/ok.proto", member), tmp_path / "dest")
    assert not (tmp_path / "evil.proto").exists()


def test_proto_archive_extracts_its_api_proto_members(tmp_path):
    from kicad_mcp.ipc.schema import extract_protos

    extract_protos(_zip("kicad-x/api/proto/common/ok.proto", "kicad-x/README.md"), tmp_path / "dest")
    assert (tmp_path / "dest" / "common" / "ok.proto").exists()
    assert not (tmp_path / "dest" / "README.md").exists()
