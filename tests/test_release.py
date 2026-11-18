"""Release bill of materials: plugin pins, the installed-dependency closure and the CycloneDX document."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)


def test_every_plugin_requirement_is_an_exact_pin():
    pins = br.plugin_pins()
    assert len(pins) >= 1 and all(version for _name, version in pins)
    assert ("kicad-python", "0.8.0") in pins


def test_the_closure_follows_installed_requirements_and_flags_missing_ones():
    closure = br.resolve_closure(sys.executable, ["mcp>=1", "no-such-distribution-kicad-ai-tools>=1"])
    assert closure["mcp"]["installed"] and closure["mcp"]["version"] and closure["mcp"]["top_level"]
    assert closure["pydantic"]["installed"] and not closure["pydantic"]["top_level"]  # transitive, from mcp
    missing = closure["no-such-distribution-kicad-ai-tools"]
    assert missing["installed"] is False and missing["declared"] == "no-such-distribution-kicad-ai-tools>=1"


def test_the_sbom_lists_server_plugin_and_external_parts_with_artifact_hashes():
    project = {"name": "kicad-ai-tools", "version": "9.9.9"}
    closure = {"mcp": {"name": "mcp", "version": "1.0", "license": "MIT", "installed": True, "declared": "mcp>=1",
                       "top_level": True},
               "pandas": {"name": "pandas", "installed": False, "declared": "pandas>=2", "top_level": True}}
    artifacts = {"x.whl": "a" * 64, "x.tar.gz": "b" * 64}
    sbom = br.build_sbom(project, closure, [("kicad-python", "0.8.0")], artifacts, "2026-10-12T00:00:00Z")
    json.dumps(sbom)  # serialisable
    assert sbom["bomFormat"] == "CycloneDX" and sbom["specVersion"] == "1.5"
    assert {h["content"] for h in sbom["metadata"]["component"]["hashes"]} == {"a" * 64, "b" * 64}
    by_ref = {c["bom-ref"]: c for c in sbom["components"]}
    assert by_ref["server:mcp"]["purl"] == "pkg:pypi/mcp@1.0"
    assert {"name": "kicad-ai-tools:installed", "value": "false"} in by_ref["server:pandas"]["properties"]
    assert by_ref["plugin:kicad-python"]["version"] == "0.8.0"
    assert by_ref["external:kicad"]["scope"] == "excluded"  # KiCad is used, never bundled
    assert "external:kicad" not in sbom["dependencies"][0]["dependsOn"]


def test_requested_extras_are_followed_and_extra_only_packages_are_optional():
    closure = br.resolve_closure(sys.executable, ["mcp[cli]>=1"])
    assert "cli" in closure["mcp"]["extras"]
    assert "typer" in closure["mcp"]["requires"] and "typer" in closure  # mcp's cli extra (installed or not)
    plain = br.resolve_closure(sys.executable, ["mcp>=1"])
    assert "typer" not in plain["mcp"]["requires"]  # not reached without the extra
    sbom = br.build_sbom({"name": "kicad-ai-tools", "version": "1"},
                         {"a": {"name": "a", "version": "1", "installed": True, "top_level": True, "requires": ["b"]},
                          "b": {"name": "b", "version": "1", "installed": True, "top_level": False, "requires": []},
                          "x": {"name": "x", "version": "1", "installed": True, "top_level": True, "requires": []}},
                         [], {"w": "c" * 64}, "2026-10-12T00:00:00Z", required={"a", "b"})
    scopes = {c["bom-ref"]: c["scope"] for c in sbom["components"]}
    assert scopes["server:a"] == scopes["server:b"] == "required" and scopes["server:x"] == "optional"
    graph = {d["ref"]: d["dependsOn"] for d in sbom["dependencies"]}
    assert graph["kicad-ai-tools"] == ["server:a", "server:x"]  # direct only; b hangs off a
    assert graph["server:a"] == ["server:b"]


def test_checksums_use_lf_line_endings_on_every_platform(tmp_path):
    out = tmp_path / "SHA256SUMS"
    br.write_checksums(out, {"x.whl": "a" * 64, "x.tar.gz": "b" * 64})
    assert out.read_bytes() == f"{'a' * 64}  x.whl\n{'b' * 64}  x.tar.gz\n".encode()
