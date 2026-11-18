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


def test_proto_archive_expansion_is_capped(tmp_path, monkeypatch):
    import io
    import zipfile

    from kicad_mcp.ipc import schema

    monkeypatch.setattr(schema, "MAX_EXTRACTED_BYTES", 1000)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("kicad-x/api/proto/a.proto", "x" * 600)
        archive.writestr("kicad-x/api/proto/b.proto", "x" * 600)
    with pytest.raises(schema.SchemaError, match="expands"):
        schema.extract_protos(buffer.getvalue(), tmp_path / "dest")


def test_proto_download_errors_become_schema_errors(tmp_path, monkeypatch):
    import urllib.error

    from kicad_mcp.ipc import schema
    from kicad_mcp.utils import source_catalog

    def refuse(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(source_catalog._opener, "open", refuse)
    with pytest.raises(schema.SchemaError, match="could not download"):
        schema.fetch_protos("0" * 40, tmp_path / "dest")


def test_generator_placeholders_fill_in_one_pass(tmp_path):
    """A path that itself contains a placeholder's text is inserted as it is, never expanded again."""
    project = tmp_path / "proj%I"
    line = ff.expand_generator_command("%P %I", project / "n.xml", project / "out", project)
    assert line == f"{project} {project / 'n.xml'}"


def test_generator_inner_quotes_are_dropped_within_one_argument(tmp_path):
    argv = ff.generator_argv('python bom.py --title="a b" "%I"', tmp_path / "n.xml", tmp_path / "out", tmp_path)
    assert argv == ["python", "bom.py", "--title=a b", str(tmp_path / "n.xml")]


def test_generator_split_keeps_apostrophes_and_refuses_unclosed_quotes(tmp_path):
    argv = ff.generator_argv("python bom.py O'Brien.csv '%I'", tmp_path / "n.xml", tmp_path / "out", tmp_path)
    assert argv == ["python", "bom.py", "O'Brien.csv", str(tmp_path / "n.xml")]
    with pytest.raises(SessionError, match="unclosed"):
        ff.generator_argv('python "bom.py', tmp_path / "n.xml", tmp_path / "out", tmp_path)
