"""Focused synthetic tests for KiCad 10.0.5 capability discovery, pinned
source access, and plugin inventory.

Every GitLab call is mocked through an injected transport; this suite makes
no live network access. Registration is exercised against a bare FastMCP
instance so it never depends on the external owned-process runtime that
``workflow_server.create_server`` requires for durable board-tooling jobs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

import kicad_mcp.workflow_server as workflow_server
from kicad_mcp.tools import cli_tools
from kicad_mcp.tools.cli_tools import register_cli_tools
from kicad_mcp.tools.full_capability_tools import register_full_capability_tools
from kicad_mcp.tools.workflow_tools import register_workflow_tools
from kicad_mcp.utils import plugin_catalog, source_catalog


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """Never read the real per-machine config, data folder or KiCad overrides; discovery stays live."""
    monkeypatch.setenv("KICAD_AI_TOOLS_CONFIG", str(tmp_path / "isolated-config.json"))
    monkeypatch.setenv("KICAD_AI_TOOLS_DATA_DIR", str(tmp_path / "isolated-data"))
    for var in ("KICAD_APP_PATH", "KICAD_CLI_PATH"):
        monkeypatch.delenv(var, raising=False)

PINNED_SHA = "18fb9289" + "0123456789abcdef" * 2  # 8 + 32 = 40 hex chars, correct prefix
assert len(PINNED_SHA) == 40


def _tag_response(sha: str = PINNED_SHA) -> tuple[int, dict, bytes]:
    body = json.dumps({"name": "10.0.5", "commit": {"id": sha}}).encode("utf-8")
    return 200, {"Content-Type": "application/json"}, body


class StubTransport:
    """Routes (url, headers) -> canned (status, headers, body); records every call made."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, headers=None):
        self.calls.append((url, headers))
        for matcher, response in self.routes:
            if matcher(url):
                return response
        raise AssertionError(f"Unexpected URL requested: {url}")


# --- Pin resolution ---------------------------------------------------------

def test_resolve_pinned_commit_accepts_matching_prefix():
    transport = StubTransport([(lambda u: "repository/tags/10.0.5" in u, _tag_response())])
    result = source_catalog.resolve_pinned_commit(transport=transport)
    assert result["resolved_sha"] == PINNED_SHA
    assert result["verified"] is True
    assert result["verified_prefix"] == source_catalog.PINNED_SHA_PREFIX


def test_resolve_pinned_commit_rejects_forged_tag():
    forged = "deadbeef" + "0" * 32
    transport = StubTransport([(lambda u: "repository/tags/10.0.5" in u, _tag_response(forged))])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.resolve_pinned_commit(transport=transport)


def test_resolve_pinned_commit_rejects_malformed_sha():
    body = json.dumps({"commit": {"id": "not-a-sha"}}).encode("utf-8")
    transport = StubTransport([(lambda u: "repository/tags" in u, (200, {}, body))])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.resolve_pinned_commit(transport=transport)


def test_resolve_pinned_commit_wraps_missing_network():
    def raise_network_error(url, headers=None):
        raise source_catalog.SourceCatalogError("GitLab request failed: simulated outage")

    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.resolve_pinned_commit(transport=raise_network_error)


# --- Path safety on the GitLab tree/file surface ----------------------------

@pytest.mark.parametrize("bad_path", ["../etc/passwd", "/etc/passwd", "a/../../b", "a\\..\\b", "a/./b"])
def test_list_source_tree_rejects_traversal_before_any_network_call(bad_path):
    transport = StubTransport([])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.list_source_tree(bad_path, transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "/etc/passwd", "a\\..\\b"])
def test_read_source_file_rejects_traversal_before_any_network_call(bad_path):
    transport = StubTransport([])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.read_source_file(bad_path, transport=transport)
    assert transport.calls == []


def test_list_source_tree_root_path_is_valid():
    tree_body = json.dumps([
        {"id": "abc123", "name": "pcbnew", "type": "tree", "path": "pcbnew", "mode": "040000"}
    ]).encode("utf-8")
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "repository/tree" in u, (200, {"X-Next-Page": "", "X-Total-Pages": "1"}, tree_body)),
    ])
    result = source_catalog.list_source_tree("", transport=transport)
    assert result["entries"][0]["name"] == "pcbnew"
    assert result["ref"]["resolved_sha"] == PINNED_SHA
    assert result["next_page"] is None


def test_list_source_tree_reads_lowercase_pagination_headers():
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "repository/tree" in u, (200, {"x-next-page": "2", "x-total-pages": "3"}, b"[]")),
    ])
    result = source_catalog.list_source_tree(transport=transport)
    assert result["next_page"] == 2
    assert result["total_pages"] == 3


def test_list_source_tree_rejects_bad_pagination():
    transport = StubTransport([])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.list_source_tree("", page=0, transport=transport)
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.list_source_tree("", per_page=101, transport=transport)
    assert transport.calls == []


# --- Transport-level host/redirect hazards ----------------------------------

def test_default_transport_refuses_non_gitlab_host():
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog._default_transport("https://evil.example/api/v4/projects")


def test_default_transport_refuses_plain_http():
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog._default_transport("http://gitlab.com/api/v4/projects")


def test_redirect_handler_refuses_malicious_redirect():
    handler = source_catalog._NoRedirect()
    with pytest.raises(source_catalog.SourceCatalogError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/payload")


def test_default_transport_wraps_url_error(monkeypatch):
    import urllib.error

    def raise_url_error(request, timeout=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(source_catalog._opener, "open", raise_url_error)
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog._default_transport("https://gitlab.com/api/v4/projects")


# --- Streaming file reads ----------------------------------------------------

def test_read_source_file_streams_with_continuation():
    content = b"hello kicad world"
    meta_body = json.dumps({"size": len(content), "content_sha256": "deadbeef", "blob_id": "blobsha"}).encode()
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "/raw" not in u and "repository/files" in u, (200, {}, meta_body)),
        (lambda u: "/raw" in u, (206, {"Content-Range": f"bytes 0-4/{len(content)}"}, content[:5])),
    ])
    result = source_catalog.read_source_file("README.md", offset=0, length=5, transport=transport)
    assert result["returned_bytes"] == 5
    assert result["next_offset"] == 5
    assert result["done"] is False
    assert result["total_size"] == len(content)
    assert result["blob_id"] == "blobsha"


def test_read_source_file_slices_full_response_when_range_is_ignored():
    content = b"hello kicad world"
    meta_body = json.dumps({"size": len(content), "blob_id": "blobsha"}).encode()
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "/raw" not in u and "repository/files" in u, (200, {}, meta_body)),
        (lambda u: "/raw" in u, (200, {}, content)),
    ])
    result = source_catalog.read_source_file("README.md", offset=6, length=5, transport=transport)
    import base64
    assert base64.b64decode(result["content_base64"]) == b"kicad"
    assert result["returned_bytes"] == 5
    assert result["next_offset"] == 11


def test_read_source_file_rejects_wrong_partial_range():
    meta_body = json.dumps({"size": 20}).encode()
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "/raw" not in u and "repository/files" in u, (200, {}, meta_body)),
        (lambda u: "/raw" in u, (206, {"Content-Range": "bytes 0-4/20"}, b"hello")),
    ])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.read_source_file("README.md", offset=6, length=5, transport=transport)


def test_read_source_file_accepts_length_above_default_chunk():
    content = b"hello"
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "/raw" not in u and "repository/files" in u,
         (200, {}, json.dumps({"size": len(content)}).encode())),
        (lambda u: "/raw" in u, (200, {}, content)),
    ])
    result = source_catalog.read_source_file("README.md", length=source_catalog.DEFAULT_CHUNK_BYTES + 1,
                                            transport=transport)
    assert result["returned_bytes"] == len(content)
    assert result["done"] is True


def test_read_source_file_rejects_partial_range_past_reported_eof():
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "/raw" not in u and "repository/files" in u, (200, {}, b'{"size": 3}')),
        (lambda u: "/raw" in u, (206, {"Content-Range": "bytes 0-3/3"}, b"four")),
    ])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.read_source_file("README.md", length=4, transport=transport)


def test_read_source_file_rejects_offset_past_end():
    meta_body = json.dumps({"size": 3, "content_sha256": "x", "blob_id": "y"}).encode()
    transport = StubTransport([
        (lambda u: "repository/tags/10.0.5" in u, _tag_response()),
        (lambda u: "repository/files" in u and "/raw" not in u, (200, {}, meta_body)),
    ])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.read_source_file("README.md", offset=10, transport=transport)


# --- Search contract ---------------------------------------------------------

@pytest.mark.parametrize("query", ["", "a\nb", "x" * 201])
def test_search_source_code_rejects_invalid_query_before_network(query):
    transport = StubTransport([])
    with pytest.raises(source_catalog.SourceCatalogError):
        source_catalog.search_source_code(query, transport=transport)
    assert transport.calls == []


# --- Plugin metadata inventory ------------------------------------------------

def test_plugin_inventory_reports_absent_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "nokicad"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)  # would take precedence over APPDATA
    monkeypatch.delenv("KICAD10_3RD_PARTY", raising=False)
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "nodocs"))
    monkeypatch.setattr(plugin_catalog.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    result = plugin_catalog.inventory_plugins()
    assert result["legacy_scripting_plugins"]["status"] == "absent"
    assert result["ipc_addon_plugins"]["status"] == "absent"
    assert result["pcm_third_party_packages"]["status"] == "absent"
    assert result["settings_root"]["status"] == "absent"


def test_legacy_scripting_plugins_lists_filenames_only_never_imported(tmp_path, monkeypatch):
    legacy_root = tmp_path / "KiCad" / "share" / "kicad" / "scripting" / "plugins"
    legacy_root.mkdir(parents=True)
    (legacy_root / "wizard.py").write_text("raise RuntimeError('should never run')", encoding="utf-8")
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "KiCad"))
    result = plugin_catalog.inventory_plugins()
    legacy = result["legacy_scripting_plugins"]
    assert legacy["status"] == "present"
    assert legacy["entries"][0]["filename"] == "wizard.py"
    assert legacy["entries"][0]["type"] == "legacy_scripting_plugin_file"
    assert "sha256" in legacy["entries"][0]


def test_pcm_package_invalid_metadata_reported_structurally(tmp_path, monkeypatch):
    third_party = tmp_path / "3rdparty"
    pkg = third_party / "badpkg"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    result = plugin_catalog.inventory_plugins()
    entry = result["pcm_third_party_packages"]["entries"][0]
    assert entry["status"] == "invalid_metadata"
    assert entry["type"] == "pcm_package"


def test_pcm_package_invalid_utf8_reported_structurally(tmp_path, monkeypatch):
    third_party = tmp_path / "3rdparty"
    pkg = third_party / "badpkg"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_bytes(b"\xff")
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    result = plugin_catalog.inventory_plugins()
    entry = result["pcm_third_party_packages"]["entries"][0]
    assert entry["status"] == "invalid_metadata"
    assert "UnicodeDecodeError" in entry["error"]


def test_pcm_package_version_mismatch_is_flagged(tmp_path, monkeypatch):
    third_party = tmp_path / "3rdparty"
    pkg = third_party / "goodpkg"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_text(
        json.dumps({"name": "Example", "versions": [{"kicad_version": "9.0"}]}), encoding="utf-8")
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    result = plugin_catalog.inventory_plugins()
    entry = result["pcm_third_party_packages"]["entries"][0]
    assert entry["status"] == "ok"
    assert entry["compatibility"]["checked"] is True
    assert entry["compatibility"]["compatible_with_pinned"] is False
    # a non-empty process KICAD10_3RD_PARTY wins and is reported as the 'env' source
    assert result["pcm_third_party_packages"]["resolved_from"] == "env"
    assert Path(result["pcm_third_party_packages"]["path"]) == third_party


def test_plugin_inventory_empty_documents_home_is_reported_not_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "nokicad"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
    monkeypatch.delenv("KICAD10_3RD_PARTY", raising=False)
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", "")
    monkeypatch.setattr(plugin_catalog.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    result = plugin_catalog.inventory_plugins()
    for key in ("user_scripting_plugins", "user_scripting_root_modules", "ipc_addon_plugins",
                "pcm_third_party_packages", "pcm_installed_package_code"):
        assert result[key]["status"] == "kicad_documents_home_empty", key
        assert result[key]["path"] is None
        assert result[key]["resolved_from"] == "KICAD_DOCUMENTS_HOME"
    assert result["legacy_scripting_plugins"]["status"] == "absent"  # non-documents roots unaffected


def test_ipc_addon_manifest_distinguished_from_pcm_metadata(tmp_path, monkeypatch):
    ipc_root = tmp_path / "Documents" / "KiCad" / "10.0" / "plugins"
    pkg = ipc_root / "myaddon"
    pkg.mkdir(parents=True)
    (pkg / "plugin.json").write_text(
        json.dumps({"name": "MyAddon", "versions": [{"kicad_version": "10.0"}]}), encoding="utf-8")
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "Documents"))
    result = plugin_catalog.inventory_plugins()
    entry = result["ipc_addon_plugins"]["entries"][0]
    assert entry["type"] == "ipc_addon"
    assert entry["compatibility"]["compatible_with_pinned"] is True


def test_pcm_symlink_escape_is_reported_not_followed(tmp_path, monkeypatch):
    third_party = tmp_path / "3rdparty"
    third_party.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = third_party / "evil"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    result = plugin_catalog.inventory_plugins()
    # the link is reported (not followed, not hashed) instead of being silently dropped
    assert result["pcm_third_party_packages"]["entries"] == [
        {"type": "pcm_package", "package_dir": "evil", "status": "link_entry_not_followed",
         "reason": "resolves_outside_root"}]


# --- FastMCP registration surface -------------------------------------------

def test_registration_surface_includes_new_and_prior_tools():
    server = FastMCP("test-kicad-capability")
    register_workflow_tools(server, jobs=None)
    register_full_capability_tools(server)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    for expected in ("kicad_capability_report", "kicad_source_resolve_pin", "kicad_source_list_tree",
                     "kicad_source_read_file", "kicad_source_search_code", "kicad_plugin_inventory",
                     "kicad_plugin_activate", "kicad_plugin_inspect", "kicad_plugin_closure_check"):
        assert expected in names
    assert "boardtooling_capabilities" in names  # prior tool surface preserved


def test_capability_matrix_declares_structured_limitations():
    from kicad_mcp.tools import full_capability_tools as fct

    matrix = fct._capability_matrix()
    assert "kicad_source_search_code" in matrix["supported"]
    assert "kicad_source_search_code" not in matrix["callable_unverified"]
    assert "kicad_source_search_code" not in matrix["unsupported"]
    assert "kicad_source_search_code" not in matrix["blocked"]
    assert "kicad_plugin_activate" in matrix["blocked"]
    assert "kicad_source_list_tree" in matrix["supported"]


# --- D1 plugin inspection tools: registration, matrix, structured rejection --------------------
# These tests use the real plugin_inspect module with the host quarantine root pointed at a
# tmp_path directory through KICAD_MCP_QUARANTINE_ROOT; nothing is monkeypatched for error codes.
# Nothing here imports or runs a plugin file.

PLUGIN_INSPECT_TOOLS = ("kicad_plugin_inspect", "kicad_plugin_closure_check")


@pytest.fixture
def qroot(tmp_path, monkeypatch):
    root = tmp_path / "host-quarantine"
    root.mkdir()
    monkeypatch.setenv("KICAD_MCP_QUARANTINE_ROOT", str(root))
    return root


def _fixture_zip(root: Path, name: str = "pkg.zip") -> Path:
    import zipfile

    path = root / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tool.py", "x = 1\n")
    return path


def _fixture_wheel(root: Path, name: str = "app", version: str = "1.0") -> Path:
    import zipfile

    path = root / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return path


def _reported_root(result, root: Path):
    assert Path(result["quarantine_root"]["path"]) == root
    assert result["quarantine_root"]["source"] == "KICAD_MCP_QUARANTINE_ROOT"


def _tool_payload(result):
    """Normalise FastMCP.call_tool output across mcp versions to the returned dict."""
    if isinstance(result, tuple):
        result = result[1] if len(result) > 1 and isinstance(result[1], dict) else result[0]
    if isinstance(result, list):
        result = json.loads(result[0].text)
    if isinstance(result, dict) and set(result) == {"result"} and isinstance(result["result"], dict):
        result = result["result"]
    return result


def test_plugin_inspect_tools_registered_and_listed_as_supported():
    from kicad_mcp.tools import full_capability_tools as fct

    server = FastMCP("test-kicad-plugin-inspect")
    register_full_capability_tools(server)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    matrix = fct._capability_matrix()
    for tool in PLUGIN_INSPECT_TOOLS:
        assert tool in names
        assert tool in matrix["supported"]
        for other in ("callable_unverified", "blocked", "unsupported"):
            assert tool not in matrix[other]
    assert "kicad_plugin_activate" in matrix["blocked"]
    assert "kicad_plugin_activate" not in matrix["supported"]


def test_plugin_tool_schemas_have_no_quarantine_root():
    server = FastMCP("test-kicad-plugin-schemas")
    register_full_capability_tools(server)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in PLUGIN_INSPECT_TOOLS:
        assert "quarantine_root" not in json.dumps(tools[name].inputSchema)
    assert set(tools["kicad_plugin_inspect"].inputSchema["properties"]) == {"package"}
    assert set(tools["kicad_plugin_closure_check"].inputSchema["properties"]) == {"wheels"}


def test_plugin_inspect_wrapper_rejections_carry_real_codes(qroot):
    from kicad_mcp.tools import full_capability_tools as fct

    _fixture_zip(qroot)
    cases = {
        "../pkg.zip": "path_traversal",
        str(qroot / "pkg.zip"): "path_not_relative",
        "C:/Windows": "path_not_relative",
        "//server/share/pkg.zip": "path_not_relative",
        ".": "path_outside_quarantine",
        "missing.zip": "path_not_found",
        "": "path_invalid",
    }
    for package, code in cases.items():
        result = fct.run_plugin_inspect(package)
        assert result["status"] == "rejected", package
        assert result["error"]["code"] == code, package
        assert result["error"]["message"]
        _reported_root(result, qroot)


def test_plugin_closure_check_wrapper_rejections_carry_real_codes(tmp_path, qroot):
    from kicad_mcp.tools import full_capability_tools as fct

    outside = _fixture_wheel(tmp_path, "outside")
    link = qroot / outside.name
    try:
        link.symlink_to(outside)
    except OSError:
        link = None  # symlink creation not permitted here; the other cases still run
    cases = [
        ([str(outside)], "path_not_relative"),
        (["../" + outside.name], "path_traversal"),
        (["missing.whl"], "path_not_found"),
    ]
    if link is not None:
        cases.append(([outside.name], "wheel_outside_quarantine"))
    for wheels, code in cases:
        result = fct.run_plugin_closure_check(wheels)
        assert result["status"] == "rejected", wheels
        assert result["error"]["code"] == code, wheels
        _reported_root(result, qroot)


def test_plugin_wrappers_report_invalid_host_root_without_a_root(tmp_path, monkeypatch):
    from kicad_mcp.tools import full_capability_tools as fct

    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    for bad in (str(a_file), "relative/root"):
        monkeypatch.setenv("KICAD_MCP_QUARANTINE_ROOT", bad)
        for result in (fct.run_plugin_inspect("pkg.zip"), fct.run_plugin_closure_check([])):
            assert result["status"] == "rejected"
            assert result["error"]["code"] == "quarantine_root_invalid"
            assert "quarantine_root" not in result


def test_plugin_inspect_wrappers_pass_through_success(qroot):
    from kicad_mcp.tools import full_capability_tools as fct

    archive = _fixture_zip(qroot)
    wheel = _fixture_wheel(qroot)
    inspected = fct.run_plugin_inspect("pkg.zip")
    assert inspected["status"] == "ok" and inspected["executes_nothing"] is True
    assert inspected["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert [f["path"] for f in inspected["files"]] == ["tool.py"]
    _reported_root(inspected, qroot)
    closure = fct.run_plugin_closure_check([wheel.name])
    assert closure["blocks"] is False and [m["name"] for m in closure["members"]] == ["app"]
    assert closure["members"][0]["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    _reported_root(closure, qroot)
    # a member-level rejection (not an exception) also reports the root
    bad = qroot / "bad.zip"
    bad.write_bytes(b"not a zip")
    rejected = fct.run_plugin_inspect("bad.zip")
    assert rejected["status"] == "rejected" and rejected["rejection"]["code"] == "archive_member_rejected"
    _reported_root(rejected, qroot)


def test_plugin_inspect_default_root_is_created_under_localappdata(tmp_path, monkeypatch):
    from kicad_mcp.tools import full_capability_tools as fct

    monkeypatch.delenv("KICAD_MCP_QUARANTINE_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    result = fct.run_plugin_inspect("missing.zip")
    expected = tmp_path / "lad" / "kicad-mcp" / "quarantine"
    assert result["error"]["code"] == "path_not_found"
    assert Path(result["quarantine_root"]["path"]) == expected and expected.is_dir()
    assert result["quarantine_root"]["source"] == "LOCALAPPDATA_default"


def test_plugin_inspect_tools_callable_through_the_server(qroot):
    _fixture_zip(qroot)
    wheel = _fixture_wheel(qroot)
    server = FastMCP("test-kicad-plugin-inspect-call")
    register_full_capability_tools(server)
    rejected = _tool_payload(asyncio.run(server.call_tool("kicad_plugin_inspect", {"package": "../pkg.zip"})))
    assert rejected["status"] == "rejected" and rejected["error"]["code"] == "path_traversal"
    _reported_root(rejected, qroot)
    ok = _tool_payload(asyncio.run(server.call_tool("kicad_plugin_inspect", {"package": "pkg.zip"})))
    assert ok["status"] == "ok"
    _reported_root(ok, qroot)
    closed = _tool_payload(asyncio.run(server.call_tool(
        "kicad_plugin_closure_check", {"wheels": [wheel.name]})))
    assert closed["blocks"] is False
    _reported_root(closed, qroot)
    refused = _tool_payload(asyncio.run(server.call_tool(
        "kicad_plugin_closure_check", {"wheels": [str(wheel)]})))
    assert refused["status"] == "rejected" and refused["error"]["code"] == "path_not_relative"


# --- kicad-cli tools: registration and capability matrix ---------------------------------------

CLI_TOOLS = ("kicad_cli_commands", "kicad_cli_describe", "kicad_cli_run")


def test_cli_tools_registered_and_listed_as_supported():
    server = FastMCP("test-kicad-cli")
    register_full_capability_tools(server)
    register_cli_tools(server)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    for tool in CLI_TOOLS:
        assert tool in names
    from kicad_mcp.tools import full_capability_tools as fct

    matrix = fct._capability_matrix()
    for tool in CLI_TOOLS:
        assert tool in matrix["supported"]
        for other in ("callable_unverified", "blocked", "unsupported"):
            assert tool not in matrix[other]
    assert "kicad_cli_command:jobset run" in matrix["blocked"]
    assert "kicad_cli_command:pcb export hpgl" in matrix["unsupported"]
    assert "kicad_cli_command:pcb render" in matrix["callable_unverified"]


def test_cli_tools_registered_by_create_server(monkeypatch):
    monkeypatch.setattr(workflow_server, "WorkflowJobs", lambda: None)  # no durable job runtime needed
    server = workflow_server.create_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert set(CLI_TOOLS) <= names


def test_cli_command_coverage_reconciles_to_43():
    coverage = cli_tools.command_coverage()
    assert coverage["registered"] == 43
    assert (coverage["supported"], coverage["callable_unverified"], coverage["blocked"],
            coverage["deprecated"]) == (40, 1, 1, 1)
    assert coverage["supported"] + coverage["callable_unverified"] + coverage["blocked"] \
        + coverage["deprecated"] == coverage["registered"]
    assert coverage["runnable"] == 41
    groups = {}
    for cmd in cli_tools.cli_registry.list_commands():
        groups[cmd.group] = groups.get(cmd.group, 0) + 1
    assert groups == {"fp": 2, "jobset": 1, "pcb": 27, "sch": 10, "sym": 2, "version": 1}


def test_every_path_like_option_has_an_explicit_role():
    assert cli_tools.unclassified_path_options() == []
    # the explicit table only names options that exist
    names = {option.long for cmd in cli_tools.cli_registry.list_commands() for option in cmd.options}
    assert set(cli_tools.PATH_ROLES) <= names


# --- kicad-cli tools: fixtures -----------------------------------------------------------------

def _discovered_cli() -> tuple[str, str]:
    """The real kicad-cli for the live tests, found by discovery alone (the per-machine config and the
    KICAD_APP_PATH / KICAD_CLI_PATH overrides are ignored, as in the isolated tests), and why not if absent."""
    import os
    from kicad_mcp.adaptive_editor import kicad_env
    saved = {k: os.environ.pop(k, None) for k in ("KICAD_APP_PATH", "KICAD_CLI_PATH")}
    import tempfile
    absent = os.path.join(tempfile.gettempdir(), f"kicad-ai-tools-no-config-{os.getpid()}.json")  # never created
    saved_config = os.environ.get("KICAD_AI_TOOLS_CONFIG")
    os.environ["KICAD_AI_TOOLS_CONFIG"] = absent
    kicad_env.kicad_app_root.cache_clear()
    try:
        return str(kicad_env.kicad_cli()), ""
    except (kicad_env.KiCadNotFound, kicad_env.ConfigError) as exc:
        return "", f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
    finally:
        kicad_env.kicad_app_root.cache_clear()
        for key, value in [*saved.items(), ("KICAD_AI_TOOLS_CONFIG", saved_config)]:
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


KICAD_CLI, KICAD_CLI_PROBLEM = _discovered_cli()
requires_kicad = pytest.mark.skipif(not Path(KICAD_CLI).is_file(),
                                    reason=f"no real kicad-cli 10.0.5 found by discovery ({KICAD_CLI_PROBLEM})")
FAKE_CLI = "C:/fake/kicad-cli.exe"

PCB_TEXT = """(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (9 "F.Adhes" user "F.Adhesive")
    (11 "B.Adhes" user "B.Adhesive")
    (13 "F.Paste" user)
    (15 "B.Paste" user)
    (5 "F.SilkS" user "F.Silkscreen")
    (7 "B.SilkS" user "B.Silkscreen")
    (1 "F.Mask" user)
    (3 "B.Mask" user)
    (17 "Dwgs.User" user "User.Drawings")
    (19 "Cmts.User" user "User.Comments")
    (21 "Eco1.User" user "User.Eco1")
    (23 "Eco2.User" user "User.Eco2")
    (25 "Edge.Cuts" user)
    (27 "Margin" user)
    (31 "F.CrtYd" user "F.Courtyard")
    (29 "B.CrtYd" user "B.Courtyard")
    (35 "F.Fab" user)
    (33 "B.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default)) (fill no)
    (layer "Edge.Cuts") (uuid "11111111-1111-1111-1111-111111111111"))
)
"""

SCH_TEXT = """(kicad_sch
  (version 20250114)
  (generator "eeschema")
  (generator_version "9.0")
  (uuid "22222222-2222-2222-2222-222222222222")
  (paper "A4")
  (lib_symbols)
  (sheet_instances (path "/" (page "1")))
  (embedded_fonts no)
)
"""


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.kicad_pcb").write_bytes(PCB_TEXT.encode("utf-8"))
    (proj / "sheet.kicad_sch").write_bytes(SCH_TEXT.encode("utf-8"))
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "evidence").mkdir()
    return proj, out


def _run(tmp_path, *args, **kwargs):
    return cli_tools.run_cli(*args, evidence_base=tmp_path / "evidence", **kwargs)


def _hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
            if p.is_file()}


@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setattr(cli_tools, "get_kicad_cli_path", lambda required=True: FAKE_CLI)


@pytest.fixture
def real_cli(monkeypatch):
    monkeypatch.setattr(cli_tools, "get_kicad_cli_path", lambda required=True: KICAD_CLI)


def _fake_runner(monkeypatch, *, stdout=b"", stderr=b"", exit_code=0, raises=None,
                 side_effect=None, seen=None):
    def fake(argv, *, cwd=None, timeout=None, text=True, encoding=None, errors=None,
             capture_output=True, check=False, evidence_dir=None, evidence_context=None):
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / "stdout.bin").write_bytes(stdout)
        (evidence / "stderr.bin").write_bytes(stderr)
        if seen is not None:
            seen.update(argv=list(argv), cwd=cwd, timeout=timeout)
        if side_effect is not None:
            side_effect()
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, exit_code, "", "")

    monkeypatch.setattr(cli_tools.secure_subprocess, "run_contained", fake)


# --- kicad-cli tools: refusals (no process is ever launched) -----------------------------------


def _refusal_code(result):
    assert result["ok"] is False and result["status"] == "refused", result
    return result["error"]["code"]


def test_cli_run_refuses_unknown_command_and_option(tmp_path, project):
    proj, out = project
    assert _refusal_code(_run(tmp_path, "pcb frobnicate", str(proj / "board.kicad_pcb"))) \
        == "unknown_command"
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"),
                  {"rm-rf": True}, str(out / "n.net"))
    assert _refusal_code(result) == "unknown_option"
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"),
                  {"format": "no-such-format"}, str(out / "n.net"))
    assert _refusal_code(result) == "invalid_choice"
    assert list(out.iterdir()) == []


def test_cli_run_refuses_mutation_without_allow_and_changes_nothing(tmp_path, project):
    proj, out = project
    before = _hashes(proj)
    for name, options in (("sch upgrade", None), ("pcb upgrade", {"force": True})):
        target = proj / ("sheet.kicad_sch" if name.startswith("sch") else "board.kicad_pcb")
        assert _refusal_code(_run(tmp_path, name, str(target), options)) == "mutation_refused"
    result = _run(tmp_path, "pcb drc", str(proj / "board.kicad_pcb"),
                  {"refill_zones": True, "save_board": True}, str(out / "drc.rpt"))
    assert _refusal_code(result) == "mutation_refused"
    assert _hashes(proj) == before  # no backup files, no rewritten files
    assert list(out.iterdir()) == []


def test_cli_run_refuses_reserved_output_force_and_existing_output(tmp_path, project):
    proj, out = project
    board = str(proj / "board.kicad_pcb")
    assert _refusal_code(_run(tmp_path, "pcb export step", board, {"output": str(out / "x.step")},
                              str(out / "b.step"))) == "reserved_option"
    assert _refusal_code(_run(tmp_path, "pcb export step", board, {"force": True},
                              str(out / "b.step"))) == "force_refused"
    (out / "taken.step").write_bytes(b"keep me")
    assert _refusal_code(_run(tmp_path, "pcb export step", board, None,
                              str(out / "taken.step"))) == "output_exists"
    assert (out / "taken.step").read_bytes() == b"keep me"
    assert _refusal_code(_run(tmp_path, "pcb export step", board)) == "output_path_required"
    assert _refusal_code(_run(tmp_path, "pcb export step", board, None, "relative/b.step")) \
        == "path_not_absolute"
    assert _refusal_code(_run(tmp_path, "pcb export step", board, None,
                              str(out / ".." / "out" / "b.step"))) == "path_traversal"
    assert _refusal_code(_run(tmp_path, "pcb export step", board, None, str(out / "${HOME}.step"))) \
        == "unsafe_path"
    assert _refusal_code(_run(tmp_path, "version", None, None, str(out / "v.txt"))) \
        == "output_not_supported"


def test_cli_run_refuses_paths_outside_the_project(tmp_path, project):
    proj, out = project
    board = str(proj / "board.kicad_pcb")
    outside = tmp_path / "elsewhere.kicad_wks"
    outside.write_bytes(b"(kicad_wks)")
    result = _run(tmp_path, "pcb export svg", board, {"drawing_sheet": str(outside)}, str(out / "svg"))
    assert _refusal_code(result) == "path_outside_project"
    result = _run(tmp_path, "pcb export drill", board, {"report_path": str(tmp_path / "r.rpt")},
                  str(out / "drill"))
    assert _refusal_code(result) == "path_outside_output_root"
    result = _run(tmp_path, "pcb export vrml", board, {"models_dir": "../escape"}, str(out / "m.wrl"))
    assert _refusal_code(result) == "invalid_value"
    result = _run(tmp_path, "pcb export svg", board, {"variant": "../evil"}, str(out / "svg"))
    assert _refusal_code(result) == "invalid_value"
    assert not (out / "svg").exists()


def test_cli_run_refuses_blocked_and_deprecated_commands(tmp_path, project):
    proj, out = project
    jobset = proj / "x.kicad_jobset"
    jobset.write_bytes(b"{}")
    assert _refusal_code(_run(tmp_path, "jobset run", str(jobset), None, str(out / "j"))) \
        == "blocked_command"
    assert _refusal_code(_run(tmp_path, "pcb export hpgl", str(proj / "board.kicad_pcb"), None,
                              str(out / "h"))) == "deprecated_command"


def test_cli_run_input_checks(tmp_path, project):
    proj, out = project
    assert _refusal_code(_run(tmp_path, "pcb upgrade", None, None, None, True)) == "input_required"
    assert _refusal_code(_run(tmp_path, "version", str(proj / "board.kicad_pcb"))) \
        == "input_not_expected"
    assert _refusal_code(_run(tmp_path, "sch export netlist", str(proj / "missing.kicad_sch"), None,
                              str(out / "n.net"))) == "input_not_found"
    assert _refusal_code(_run(tmp_path, "sch export netlist", str(proj), None, str(out / "n.net"))) \
        == "input_wrong_kind"
    assert _refusal_code(_run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"), None,
                              str(proj / "sheet.kicad_sch"))) == "output_exists"


# --- kicad-cli tools: runner behaviour with a stand-in process ---------------------------------


def test_cli_run_builds_exact_argv_and_bounds_output(tmp_path, project, fake_cli, monkeypatch):
    proj, out = project
    seen = {}
    payload = b"x" * 200_000
    _fake_runner(monkeypatch, stdout=payload, stderr=b"warning\n", seen=seen)
    sheet = proj / "sheet.kicad_sch"
    result = _run(tmp_path, "sch export netlist", str(sheet), {"format": "spice"},
                  str(out / "n.net"))
    assert result["ok"] is True and result["exit_code"] == 0 and result["timed_out"] is False
    assert seen["argv"] == [FAKE_CLI, "sch", "export", "netlist", "--format", "spice", "--output",
                            str((out / "n.net").parent.resolve() / "n.net"), str(sheet.resolve())]
    assert result["argv"] == seen["argv"]
    assert seen["timeout"] == cli_tools.DEFAULT_TIMEOUT_SECONDS
    assert result["stdout_bytes"] == len(payload)
    assert result["stdout_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) < cli_tools.MAX_STREAM_BYTES + 200
    assert result["stderr"] == "warning\n" and result["stderr_truncated"] is False
    assert Path(result["evidence_dir"]).is_dir()  # kept because the output was truncated


def test_cli_run_lists_new_output_files_with_sha256(tmp_path, project, fake_cli, monkeypatch):
    proj, out = project
    produced = out / "svg"

    def write_outputs():
        (produced / "a.svg").write_bytes(b"<svg/>")
        (produced / "b.svg").write_bytes(b"<svg>b</svg>")

    _fake_runner(monkeypatch, side_effect=write_outputs)
    result = _run(tmp_path, "pcb export svg", str(proj / "board.kicad_pcb"),
                  {"layers": "Edge.Cuts", "mode_multi": True}, str(produced))
    assert result["ok"] is True and result["output_dir_created"] is True
    listed = {Path(f["path"]).name: f for f in result["output_files"]}
    assert listed["a.svg"]["sha256"] == hashlib.sha256(b"<svg/>").hexdigest()
    assert listed["b.svg"]["size"] == len(b"<svg>b</svg>")
    assert all(f["change"] == "new" for f in listed.values())
    assert result["output_files_total"] == 2 and result["output_files_truncated"] is False
    assert result["changed_project_files"] == []
    assert result["evidence_dir"] is None  # small clean run: scratch evidence removed


def test_cli_run_structured_timeout(tmp_path, project, fake_cli, monkeypatch):
    proj, out = project
    _fake_runner(monkeypatch, raises=subprocess.TimeoutExpired(["kicad-cli"], 1))
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"), None,
                  str(out / "n.net"), timeout_seconds=5)
    assert result["ok"] is False and result["timed_out"] is True and result["exit_code"] is None
    assert result["status"] == "timed_out" and result["error"]["code"] == "timeout"
    assert result["timeout_seconds"] == 5


def test_cli_run_runner_failure_and_nonzero_exit_are_reported(tmp_path, project, fake_cli,
                                                              monkeypatch):
    proj, out = project
    _fake_runner(monkeypatch, raises=RuntimeError("Could not verify all owned jobs stopped"))
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"), None,
                  str(out / "n1.net"))
    assert result["ok"] is False and result["error"]["code"] == "runner_error"
    _fake_runner(monkeypatch, exit_code=3, stderr=b"boom")
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"), None,
                  str(out / "n2.net"))
    assert result["ok"] is False and result["exit_code"] == 3
    assert result["error"]["code"] == "nonzero_exit" and result["stderr"] == "boom"


def test_cli_run_flags_unexpected_project_change_on_read_only_run(tmp_path, project, fake_cli,
                                                                  monkeypatch):
    proj, out = project
    sheet = proj / "sheet.kicad_sch"
    original = sheet.read_bytes()

    def tamper():
        sheet.write_bytes(original + b"; rewritten\n")
        (proj / "~sheet.kicad_sch.lck").write_bytes(b"lock")

    _fake_runner(monkeypatch, side_effect=tamper)
    result = _run(tmp_path, "sch export netlist", str(sheet), None, str(out / "n.net"))
    assert result["ok"] is False
    assert result["error"]["code"] == "unexpected_project_change"
    changes = {Path(c["path"]).name: c["change"] for c in result["changed_project_files"]}
    assert changes == {"sheet.kicad_sch": "modified", "~sheet.kicad_sch.lck": "added"}
    assert result["backup"]["created"] is False


def test_cli_run_mutation_makes_verified_backup_before_launch(tmp_path, project, fake_cli,
                                                              monkeypatch):
    proj, out = project
    sheet, board = proj / "sheet.kicad_sch", proj / "board.kicad_pcb"
    original_sheet, original_board = sheet.read_bytes(), board.read_bytes()
    state = {}

    def rewrite():  # the backup must already exist when the process starts
        state["backups_at_launch"] = sorted(p.name for p in proj.iterdir() if ".bak-" in p.name)
        sheet.write_bytes(b"(kicad_sch (version 99999999))\n")

    _fake_runner(monkeypatch, side_effect=rewrite)
    result = _run(tmp_path, "sch upgrade", str(sheet), {"force": True}, None, True)
    assert result["ok"] is True and result["mutation_authorised"] is True
    backup = result["backup"]
    assert backup["created"] is True and backup["verified"] is True
    by_source = {Path(i["source"]).name: Path(i["backup"]) for i in backup["items"]}
    assert set(by_source) == {"sheet.kicad_sch", "board.kicad_pcb"}
    assert by_source["sheet.kicad_sch"].name.startswith("sheet.kicad_sch.bak-")
    assert by_source["sheet.kicad_sch"].parent == sheet.resolve().parent  # next to the input
    assert by_source["sheet.kicad_sch"].read_bytes() == original_sheet
    assert by_source["board.kicad_pcb"].read_bytes() == original_board
    assert len(state["backups_at_launch"]) >= 3  # two copies and the manifest, before launch
    manifest = json.loads(Path(backup["manifest_path"]).read_text(encoding="utf-8"))
    assert {Path(e["source"]).name for e in manifest["entries"]} == set(by_source)
    assert {e["sha256"] for e in manifest["entries"]} == {
        hashlib.sha256(original_sheet).hexdigest(), hashlib.sha256(original_board).hexdigest()}
    assert [Path(c["path"]).name for c in result["changed_project_files"]] == ["sheet.kicad_sch"]


def test_cli_run_directory_mutation_backs_up_whole_library(tmp_path, fake_cli, monkeypatch):
    lib = tmp_path / "parts.pretty"
    lib.mkdir()
    (lib / "r.kicad_mod").write_bytes(b"(footprint r)")
    (lib / "notes.txt").write_bytes(b"keep")
    (tmp_path / "evidence").mkdir()
    _fake_runner(monkeypatch)
    result = _run(tmp_path, "fp upgrade", str(lib), None, None, True)
    assert result["ok"] is True
    item = result["backup"]["items"][0]
    backup_dir = Path(item["backup"])
    assert backup_dir.name.startswith("parts.pretty.bak-") and backup_dir.parent == tmp_path.resolve()
    assert (backup_dir / "r.kicad_mod").read_bytes() == b"(footprint r)"
    assert (backup_dir / "notes.txt").read_bytes() == b"keep"
    assert item["files"] == 2


# --- kicad-cli tools: integration with the real kicad-cli (hidden, tmp_path fixtures only) -----


@requires_kicad
def test_cli_run_version_real_kicad(tmp_path, real_cli):
    result = _run(tmp_path, "version", None, {"format": "plain"})
    assert result["ok"] is True, result
    assert result["exit_code"] == 0 and result["timed_out"] is False
    assert result["argv"][0] == KICAD_CLI and result["argv"][1:] == ["version", "--format", "plain"]
    assert "10." in result["stdout"]
    assert result["project_root"] is None and result["backup"]["created"] is False


@requires_kicad
def test_cli_run_pcb_export_svg_real_kicad(tmp_path, project, real_cli):
    proj, out = project
    before = _hashes(proj)
    result = _run(tmp_path, "pcb export svg", str(proj / "board.kicad_pcb"),
                  {"layers": "Edge.Cuts", "mode_multi": True}, str(out / "svg"))
    assert result["ok"] is True, result
    svgs = [f for f in result["output_files"] if f["path"].lower().endswith(".svg")]
    assert svgs, result
    for item in svgs:
        assert len(item["sha256"]) == 64 and item["size"] > 0
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert result["changed_project_files"] == []
    assert result["changed_project_files_total"] == 0
    # KiCad 10.0.5 may create board.kicad_prl (project-local UI state). That is the only
    # tolerated addition; every pre-existing file must stay byte-identical.
    after = _hashes(proj)
    assert {k: after[k] for k in before} == before
    side = result["kicad_side_effects"]
    assert set(after) - set(before) == {Path(s["path"]).name for s in side}
    assert [Path(s["path"]).name for s in side] in ([], ["board.kicad_prl"])
    for s in side:
        assert s["sha256_after"] == after[Path(s["path"]).name]


def test_cli_run_prl_side_effect_policy(tmp_path, project, fake_cli, monkeypatch):
    proj, out = project
    board = str(proj / "board.kicad_pcb")
    prl = proj / "board.kicad_prl"

    def run(name, **kw):
        return _run(tmp_path, "pcb export svg", board, {"layers": "Edge.Cuts"},
                    str(out / name), **kw)

    # (c) only board.kicad_prl added: tolerated, reported, kept on disk
    _fake_runner(monkeypatch, side_effect=lambda: prl.write_bytes(b"(prl)"))
    result = run("c")
    assert result["ok"] is True and result["changed_project_files"] == []
    assert [Path(s["path"]).name for s in result["kicad_side_effects"]] == ["board.kicad_prl"]
    assert result["kicad_side_effects"][0]["sha256_after"] == hashlib.sha256(b"(prl)").hexdigest()
    assert prl.exists()
    # (a) existing prl rewritten: still a failure
    _fake_runner(monkeypatch, side_effect=lambda: prl.write_bytes(b"(changed)"))
    result = run("a")
    assert result["error"]["code"] == "unexpected_project_change"
    assert {Path(c["path"]).name: c["change"] for c in result["changed_project_files"]} \
        == {"board.kicad_prl": "modified"}
    prl.unlink()
    # (b) other added files, and a prl in a subdirectory: still failures
    for extra in ("evil.kicad_prl", "board.kicad_pro"):
        _fake_runner(monkeypatch, side_effect=lambda e=extra: (proj / e).write_bytes(b"x"))
        result = run("b-" + extra)
        assert result["error"]["code"] == "unexpected_project_change"
        assert result["kicad_side_effects"] == []
        (proj / extra).unlink()

    def sub():
        (proj / "sub").mkdir()
        (proj / "sub" / "board.kicad_prl").write_bytes(b"x")

    _fake_runner(monkeypatch, side_effect=sub)
    assert run("b-sub")["error"]["code"] == "unexpected_project_change"


def test_cli_run_flags_deleted_project_file_on_read_only_run(tmp_path, project, fake_cli,
                                                             monkeypatch):
    proj, out = project
    sheet = proj / "sheet.kicad_sch"
    _fake_runner(monkeypatch, side_effect=sheet.unlink)
    result = _run(tmp_path, "sch export netlist", str(sheet), None, str(out / "n.net"))
    assert result["ok"] is False
    assert result["error"]["code"] == "unexpected_project_change"
    assert {Path(c["path"]).name: c["change"] for c in result["changed_project_files"]} \
        == {"sheet.kicad_sch": "removed"}
    assert result["kicad_side_effects"] == []


def test_cli_run_other_stem_prl_beside_matching_pro_is_not_tolerated(tmp_path, project, fake_cli,
                                                                    monkeypatch):
    proj, out = project
    board = str(proj / "board.kicad_pcb")
    (proj / "other.kicad_pro").write_bytes(b"{}")
    other_prl = proj / "other.kicad_prl"
    _fake_runner(monkeypatch, side_effect=lambda: other_prl.write_bytes(b"(prl)"))
    result = _run(tmp_path, "pcb export svg", board, {"layers": "Edge.Cuts"}, str(out / "a"))
    assert result["error"]["code"] == "unexpected_project_change"
    assert result["kicad_side_effects"] == []
    assert {Path(c["path"]).name: c["change"] for c in result["changed_project_files"]} \
        == {"other.kicad_prl": "added"}
    other_prl.unlink()
    # the input's own stem is still tolerated with other.kicad_pro present
    own_prl = proj / "board.kicad_prl"
    _fake_runner(monkeypatch, side_effect=lambda: own_prl.write_bytes(b"(prl)"))
    result = _run(tmp_path, "pcb export svg", board, {"layers": "Edge.Cuts"}, str(out / "b"))
    assert result["ok"] is True and result["changed_project_files"] == []
    assert [Path(s["path"]).name for s in result["kicad_side_effects"]] == ["board.kicad_prl"]


def test_cli_run_authorised_change_to_unbacked_file_is_flagged(tmp_path, project, fake_cli,
                                                               monkeypatch):
    proj, out = project
    locked = proj / "locked.kicad_dru"
    locked.write_bytes(b"(rules)")
    real_sha = cli_tools._sha256_file
    state = {"unreadable": True}

    def flaky_sha(path):
        if state["unreadable"] and Path(path).name == "locked.kicad_dru":
            raise OSError("simulated lock")
        return real_sha(path)

    monkeypatch.setattr(cli_tools, "_sha256_file", flaky_sha)

    def rewrite():  # the file becomes readable and changes; it had no hash and so no backup
        state["unreadable"] = False
        locked.write_bytes(b"(rules changed)")

    _fake_runner(monkeypatch, side_effect=rewrite)
    result = _run(tmp_path, "sch upgrade", str(proj / "sheet.kicad_sch"), None, None, True)
    assert result["ok"] is False
    assert result["error"]["code"] == "changed_without_backup"
    assert "locked.kicad_dru" in result["error"]["message"]
    assert "locked.kicad_dru" not in {Path(i["source"]).name for i in result["backup"]["items"]}
    assert {Path(c["path"]).name: c["change"] for c in result["changed_project_files"]} \
        == {"locked.kicad_dru": "modified"}


@requires_kicad
def test_cli_run_sch_export_netlist_real_kicad(tmp_path, project, real_cli):
    proj, out = project
    before = _hashes(proj)
    result = _run(tmp_path, "sch export netlist", str(proj / "sheet.kicad_sch"), None,
                  str(out / "net.net"))
    assert result["ok"] is True, result
    produced = Path(result["output_path"])
    assert produced.is_file() and produced.read_bytes().lstrip().startswith(b"(export")
    assert [Path(f["path"]).name for f in result["output_files"]] == ["net.net"]
    assert result["output_files"][0]["sha256"] == hashlib.sha256(produced.read_bytes()).hexdigest()
    assert _hashes(proj) == before


@requires_kicad
def test_cli_run_sch_upgrade_backup_is_byte_exact_real_kicad(tmp_path, project, real_cli):
    proj, _out = project
    sheet = proj / "sheet.kicad_sch"
    original = sheet.read_bytes()
    assert _refusal_code(_run(tmp_path, "sch upgrade", str(sheet), {"force": True})) \
        == "mutation_refused"
    assert sheet.read_bytes() == original
    result = _run(tmp_path, "sch upgrade", str(sheet), {"force": True}, None, True)
    assert result["backup"]["created"] is True and result["backup"]["verified"] is True
    assert result["exit_code"] == 0 and result["ok"] is True, result
    backups = {Path(i["source"]).name: Path(i["backup"]) for i in result["backup"]["items"]}
    assert backups["sheet.kicad_sch"].parent == proj.resolve()
    assert backups["sheet.kicad_sch"].read_bytes() == original
    assert Path(result["backup"]["manifest_path"]).is_file()


def test_cli_run_refuses_path_like_names_and_define_var_expansion(tmp_path, project):
    proj, out = project
    sheet = str(proj / "sheet.kicad_sch")
    for options in ({"theme": "../x"}, {"default_font": "a/b"}, {"define_var": "A=$" + "{HOME}"}):
        result = _run(tmp_path, "sch export svg", sheet, options, str(out / "svg"))
        assert _refusal_code(result) == "invalid_value"


def test_cli_run_backs_up_lib_table(tmp_path, project, fake_cli, monkeypatch):
    proj, out = project
    table = proj / "sym-lib-table"
    table.write_bytes(b"(sym_lib_table)")
    _fake_runner(monkeypatch, side_effect=lambda: table.write_bytes(b"changed"))
    result = _run(tmp_path, "sch upgrade", str(proj / "sheet.kicad_sch"), None, None, True)
    assert result["ok"] is True
    backups = {Path(i["source"]).name: Path(i["backup"]) for i in result["backup"]["items"]}
    assert backups["sym-lib-table"].read_bytes() == b"(sym_lib_table)"


# --- L4 UI broker tools: registration and capability matrix --------------------------------------
# The matrix lists only what tests prove. The L4 tools are live-verified by the l4_live tier of
# tests/test_l4.py, so they are supported; all three recorded operations are supported (the shipped
# registry is approved by registry/APPROVED.sha256), pcb_board_setup_read included.

L4_TOOLS = ("l4_operations", "l4_run", "l4_record_identifiers")
L4_OPERATIONS = ("l4_operation:pcb_open_and_save", "l4_operation:pcb_export_gerbers_dialog",
                 "l4_operation:pcb_board_setup_read")


def test_l4_tools_registered_and_matrix_claims_only_what_is_proven():
    from kicad_mcp.tools import full_capability_tools as fct
    from kicad_mcp.tools.l4_tools import register_l4_tools

    server = FastMCP("test-kicad-l4")
    register_l4_tools(server)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(L4_TOOLS) <= set(tools)
    matrix = fct._capability_matrix()
    for tool in L4_TOOLS:  # live-verified by the l4_live tests in tests/test_l4.py
        assert tool in matrix["supported"]
        for other in ("callable_unverified", "blocked", "unsupported"):
            assert tool not in matrix[other], (tool, other)
    # recorded, release-approved (registry/APPROVED.sha256) and live-verified
    for operation in L4_OPERATIONS:  # all three are recorded, release-approved and live-verified
        expected = "supported"
        assert operation in matrix[expected], (operation, expected)
        for other in {"supported", "callable_unverified", "blocked", "unsupported"} - {expected}:
            assert operation not in matrix[other], (operation, other)
    # callers pass typed values only: no identifier, selector, coordinate or registry path is a parameter
    for name, expected in (("l4_run", {"operation", "project_dir", "params"}),
                           ("l4_record_identifiers", {"project_dir"}), ("l4_operations", set())):
        assert set(tools[name].inputSchema.get("properties", {})) == expected


def test_l4_tools_registered_by_create_server(monkeypatch):
    monkeypatch.setattr(workflow_server, "WorkflowJobs", lambda: None)  # no durable job runtime needed
    server = workflow_server.create_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert set(L4_TOOLS) <= names


def test_l4_operations_tool_lists_the_release_approved_registry(monkeypatch):
    from kicad_mcp.l4 import registry

    monkeypatch.delenv(registry.APPROVAL_ENV, raising=False)
    server = FastMCP("test-kicad-l4-call")
    from kicad_mcp.tools.l4_tools import register_l4_tools

    register_l4_tools(server)
    result = _tool_payload(asyncio.run(server.call_tool("l4_operations", {})))
    assert result["registry"]["release_approved"] is True
    assert sorted(op["name"] for op in result["operations"]) == ["pcb_board_setup_read", "pcb_export_gerbers_dialog", "pcb_open_and_save"]


def test_l4_operations_tool_refuses_a_tampered_registry(monkeypatch, tmp_path):
    from kicad_mcp.l4 import registry

    monkeypatch.delenv(registry.APPROVAL_ENV, raising=False)
    shipped = registry.DEFAULT_REGISTRY_PATH
    tampered = tmp_path / shipped.name
    tampered.write_bytes(shipped.read_bytes().replace(b'"Plot"', b'"Plot "', 1))
    (tmp_path / registry.RELEASE_APPROVALS).write_bytes((shipped.parent / registry.RELEASE_APPROVALS).read_bytes())
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", tampered)
    server = FastMCP("test-kicad-l4-tampered")
    from kicad_mcp.tools.l4_tools import register_l4_tools

    register_l4_tools(server)
    result = _tool_payload(asyncio.run(server.call_tool("l4_operations", {})))
    assert result["status"] == "refused" and result["operations"] == []
    assert result["error"]["code"] == "registry_unapproved"
