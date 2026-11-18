"""Tests for L3 tier D1: offline, non-executing plugin inspection.

Every package here is synthetic and built under ``tmp_path``.  Nothing is
launched, no network is used, and the sentinel tests prove that no inspected
file is imported, compiled or executed.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import warnings
import zipfile

import pytest

from kicad_mcp.utils import plugin_inspect as pi

NOT_EXECUTED = "present, not executed"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    root = tmp_path / "quarantine"
    root.mkdir()
    return root


def _as_bytes(data) -> bytes:
    if data is None:
        return b""
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo("placeholder")
    info.filename = name  # bypass ZipInfo name sanitising so hostile names survive
    info.external_attr = 0o100644 << 16
    return info


def write_zip(path: Path, members) -> Path:
    """Write ``(name, data)`` pairs verbatim; a trailing '/' makes a directory entry."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns on duplicate names
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members:
                info = _info(name)
                if name.endswith("/"):
                    info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, _as_bytes(data), compress_type=zipfile.ZIP_DEFLATED)
    return path


def materialize(root: Path, name: str, members: dict, mode: str) -> str:
    """Create the same synthetic package as a zip or as a directory."""
    if mode == "zip":
        return str(write_zip(root / f"{name}.zip", list(members.items())))
    base = root / name
    base.mkdir()
    for rel, data in members.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_as_bytes(data))
    return str(base)


def sha(data) -> str:
    return hashlib.sha256(_as_bytes(data)).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    result = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            full = Path(current) / name
            result[str(full.relative_to(root))] = hashlib.sha256(full.read_bytes()).hexdigest()
    return result


def sentinel_source(marker: Path) -> str:
    return f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"


def try_symlink(link: Path, target: Path, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation is not permitted here")


PCM_META = {
    "$schema": "https://go.kicad.org/pcm/schemas/v1",
    "name": "Edge Tool",
    "identifier": "com.example.edge",
    "type": "plugin",
    "author": {"name": "Ada Example", "contact": {"web": "https://example.com/ada"}},
    "license": "MIT",
    "resources": {"repository": "https://example.com/repo", "homepage": "https://example.com"},
    "versions": [{"version": "1.0.0", "status": "stable", "kicad_version": "10.0"}],
}

ACTION_SRC = (
    "import pcbnew\n"
    "\n"
    "class EdgePlugin(pcbnew.ActionPlugin):\n"
    "    def defaults(self):\n"
    "        self.name = 'edge'\n"
)


def pcm_members(meta=None) -> dict:
    return {
        "metadata.json": json.dumps(PCM_META if meta is None else meta),
        "plugins/edge.py": ACTION_SRC,
        "plugins/requirements.txt": "requests==2.31.0\n",
        "resources/icon.png": b"\x89PNG\r\n\x1a\n",
    }


def rel(root: Path, target) -> str:
    """Root-relative name for a fixture path inside ``root`` (the library takes relative names only)."""
    text = str(target)
    base = os.path.normcase(str(root)).rstrip("\\/") + os.sep
    if os.path.isabs(text) and os.path.normcase(text).startswith(base):
        return os.path.relpath(text, str(root))
    return text


def inspect(root: Path, target: str, **kwargs) -> dict:
    return pi.inspect_package(rel(root, target), str(root), **kwargs)


# ---------------------------------------------------------------------------
# Name rules: archive rejections
# ---------------------------------------------------------------------------

HOSTILE_NAMES = [
    ("/abs/evil.py", "absolute"),
    ("../evil.py", "dotdot"),
    ("pkg/../../evil.py", "dotdot"),
    ("C:/evil.py", "drive_letter"),
    ("c:evil.py", "drive_letter"),
    ("//server/share/evil.py", "unc"),
    ("\\\\server\\share\\evil.py", "unc"),
    ("pkg\\evil.py", "backslash"),
    ("pkg/evil\x00.py", "nul"),
    ("a//b.py", "empty_segment"),
    ("./a.py", "dot_segment"),
    ("pkg/a:stream.py", "colon_in_name"),
    ("pkg/evil.py.", "trailing_dot_or_space"),
]


@pytest.mark.parametrize(("name", "reason"), HOSTILE_NAMES)
def test_archive_member_name_is_rejected(quarantine: Path, name: str, reason: str) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("ok.py", "x = 1\n"), (name, b"data")])
    result = inspect(quarantine, str(archive))
    assert result["status"] == "rejected"
    assert result["accepted"] is False
    assert result["rejection"]["reason"] == reason
    assert result["rejection"]["member"] == name
    assert result["executes_nothing"] is True
    assert "files" not in result  # never partially accepted
    assert "limits_applied" in result


DEVICES = ["CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]]


@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("template", ["{d}", "{lower}.txt", "sub/{d}.tar.gz"])
def test_windows_device_names_are_rejected_on_every_segment(quarantine: Path, device: str, template: str) -> None:
    name = template.format(d=device, lower=device.lower())
    archive = write_zip(quarantine / "pkg.zip", [("ok.py", "x = 1\n"), (name, b"data")])
    result = inspect(quarantine, str(archive))
    assert result["status"] == "rejected"
    assert result["rejection"] == {
        "member": name, "reason": "device_name", "code": "archive_member_rejected", "detail": ""}


def test_device_name_in_a_directory_segment_is_rejected(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("Aux/readme.txt", b"x")])
    result = inspect(quarantine, str(archive))
    assert result["rejection"]["reason"] == "device_name"


def test_non_device_lookalikes_are_accepted(quarantine: Path) -> None:
    archive = write_zip(
        quarantine / "pkg.zip",
        [("console.py", "x = 1\n"), ("com10.txt", b"x"), ("auxiliary/lpt.py", "y = 2\n")],
    )
    assert inspect(quarantine, str(archive))["status"] == "ok"


def test_name_problem_function_covers_directory_style_names() -> None:
    assert pi._name_problem("con.txt") == "device_name"
    assert pi._name_problem("pkg/Aux") == "device_name"
    assert pi._name_problem("pkg/mod.py") is None
    assert pi._name_problem("pkg/") is None


def test_symlink_member_is_rejected(quarantine: Path) -> None:
    link = _info("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(quarantine / "pkg.zip", "w") as archive:
        archive.writestr("ok.py", "x = 1\n")
        archive.writestr(link, "target.txt")
    result = inspect(quarantine, str(quarantine / "pkg.zip"))
    assert result["status"] == "rejected"
    assert result["rejection"]["reason"] == "symlink_member"
    assert result["rejection"]["member"] == "link"


def test_reparse_and_special_members_are_rejected(quarantine: Path) -> None:
    for name, attr, reason in (
        ("reparse", 0x400, "reparse_point_member"),
        ("fifo", (stat.S_IFIFO | 0o644) << 16, "special_file_member"),
    ):
        info = _info(name)
        info.external_attr = attr
        path = quarantine / f"{name}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, b"x")
        assert inspect(quarantine, str(path))["rejection"]["reason"] == reason


def test_duplicate_names_are_rejected_case_insensitively(quarantine: Path) -> None:
    exact = write_zip(quarantine / "exact.zip", [("a.py", "1"), ("a.py", "2")])
    assert inspect(quarantine, str(exact))["rejection"]["reason"] == "duplicate_name"
    folded = write_zip(quarantine / "folded.zip", [("a.py", "1"), ("A.PY", "2")])
    result = inspect(quarantine, str(folded))
    assert result["rejection"]["reason"] == "duplicate_name"
    assert result["rejection"]["member"] == "A.PY"


def test_directory_versus_file_clashes_are_rejected(quarantine: Path) -> None:
    implicit = write_zip(quarantine / "implicit.zip", [("pkg", "file"), ("pkg/mod.py", "x")])
    assert inspect(quarantine, str(implicit))["rejection"]["reason"] == "dir_file_clash"
    explicit = write_zip(quarantine / "explicit.zip", [("pkg/", None), ("pkg", "file")])
    assert inspect(quarantine, str(explicit))["rejection"]["reason"] == "dir_file_clash"


def test_namespace_problem_helper_reports_case_duplicates() -> None:
    problems = pi._namespace_problems([("a.py", False), ("A.py", False)])
    assert [p["reason"] for p in problems] == ["duplicate_name"]


# ---------------------------------------------------------------------------
# Caps (declared sizes and bounded reads)
# ---------------------------------------------------------------------------


def test_total_cap_is_enforced(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [(f"f{i}.bin", b"x" * 100) for i in range(3)])
    result = inspect(quarantine, str(archive), max_total_bytes=250)
    assert result["status"] == "rejected"
    assert result["rejection"]["reason"] == "over_total_cap"


def test_per_member_cap_is_enforced(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("small.py", "x = 1\n"), ("big.bin", b"x" * 1000)])
    result = inspect(quarantine, str(archive), max_member_bytes=500)
    assert result["rejection"]["reason"] == "over_member_cap"
    assert result["rejection"]["member"] == "big.bin"


def test_member_count_cap_is_enforced(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [(f"f{i}.txt", b"x") for i in range(5)])
    result = inspect(quarantine, str(archive), max_members=3)
    assert result["rejection"]["reason"] == "over_count_cap"


def test_decompression_bomb_is_rejected(quarantine: Path) -> None:
    archive = write_zip(quarantine / "bomb.zip", [("zeros.bin", b"\x00" * (8 * 1024 * 1024))])
    assert archive.stat().st_size < 100 * 1024  # tiny on disk, large when expanded
    result = inspect(quarantine, str(archive), max_member_bytes=1024 * 1024)
    assert result["rejection"]["reason"] == "over_member_cap"
    assert result["rejection"]["member"] == "zeros.bin"


def _patch_declared_size(path: Path, size: int) -> None:
    raw = bytearray(path.read_bytes())
    local = raw.find(b"PK\x03\x04")
    central = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<I", raw, local + 22, size)
    struct.pack_into("<I", raw, central + 24, size)
    path.write_bytes(bytes(raw))


def test_lying_header_declaring_less_is_rejected_while_reading(quarantine: Path) -> None:
    archive = write_zip(quarantine / "lie.zip", [("big.bin", b"A" * 100_000)])
    _patch_declared_size(archive, 10)
    result = inspect(quarantine, str(archive), max_member_bytes=50_000)
    assert result["status"] == "rejected"
    assert result["rejection"]["member"] == "big.bin"
    assert result["rejection"]["reason"] in ("size_lie", "corrupt_member")


def test_lying_header_declaring_more_is_rejected_by_cap(quarantine: Path) -> None:
    archive = write_zip(quarantine / "lie.zip", [("big.bin", b"A" * 1000)])
    _patch_declared_size(archive, 2**31)
    result = inspect(quarantine, str(archive))
    assert result["rejection"]["reason"] == "over_member_cap"


def test_archive_size_cap_and_corrupt_archives(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("a.py", "x = 1\n")])
    assert inspect(quarantine, str(archive), max_archive_bytes=10)["rejection"]["reason"] == "archive_too_large"
    junk = quarantine / "junk.zip"
    junk.write_bytes(b"this is not a zip archive at all")
    assert inspect(quarantine, str(junk))["rejection"]["reason"] == "corrupt_archive"
    empty = quarantine / "empty.zip"
    empty.write_bytes(b"")
    assert inspect(quarantine, str(empty))["rejection"]["reason"] == "corrupt_archive"


def test_cap_arguments_must_be_positive_integers(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("a.py", "x = 1\n")])
    for bad in (0, -1, "10", True):
        with pytest.raises(pi.PluginInspectError) as caught:
            inspect(quarantine, str(archive), max_members=bad)
        assert caught.value.code == "limit_invalid"
    with pytest.raises(pi.PluginInspectError) as caught:
        closure(quarantine, [], max_wheel_bytes=0)
    assert caught.value.code == "limit_invalid"


def test_cap_rejections_carry_cap_exceeded_code(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [(f"f{i}.bin", b"x" * 100) for i in range(3)])
    for kwargs, reason in (
        ({"max_total_bytes": 250}, "over_total_cap"),
        ({"max_member_bytes": 50}, "over_member_cap"),
        ({"max_members": 2}, "over_count_cap"),
        ({"max_archive_bytes": 10}, "archive_too_large"),
    ):
        rejection = inspect(quarantine, str(archive), **kwargs)["rejection"]
        assert (rejection["reason"], rejection["code"]) == (reason, "cap_exceeded")
    junk = quarantine / "junk.zip"
    junk.write_bytes(b"not a zip")
    rejection = inspect(quarantine, str(junk))["rejection"]
    assert (rejection["reason"], rejection["code"]) == ("corrupt_archive", "archive_member_rejected")


# ---------------------------------------------------------------------------
# Directory inputs
# ---------------------------------------------------------------------------


def test_directory_caps(quarantine: Path) -> None:
    pkg = quarantine / "pkg"
    pkg.mkdir()
    for i in range(5):
        (pkg / f"f{i}.bin").write_bytes(b"x" * 100)
    assert inspect(quarantine, str(pkg), max_members=3)["rejection"]["reason"] == "over_count_cap"
    assert inspect(quarantine, str(pkg), max_total_bytes=250)["rejection"]["reason"] == "over_total_cap"
    result = inspect(quarantine, str(pkg), max_member_bytes=50)
    assert result["rejection"]["reason"] == "over_member_cap"
    assert result["rejection"]["member"].startswith("f")


def test_directory_symlink_member_is_rejected(tmp_path: Path, quarantine: Path) -> None:
    pkg = quarantine / "pkg"
    pkg.mkdir()
    (pkg / "real.py").write_text("x = 1\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    try_symlink(pkg / "link.txt", outside, False)
    result = inspect(quarantine, str(pkg))
    assert result["status"] == "rejected"
    assert result["rejection"] == {
        "member": "link.txt", "reason": "symlink_member", "code": "archive_member_rejected", "detail": ""}


def test_directory_junction_member_is_rejected(tmp_path: Path, quarantine: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions exist on Windows only")
    import _winapi

    pkg = quarantine / "pkg"
    pkg.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        _winapi.CreateJunction(str(target), str(pkg / "junction"))
    except OSError:
        pytest.skip("junction creation is not permitted here")
    result = inspect(quarantine, str(pkg))
    assert result["status"] == "rejected"
    assert result["rejection"]["member"] == "junction"
    assert result["rejection"]["reason"] in ("reparse_point_member", "symlink_member")


def test_directory_hardlink_member_is_rejected(quarantine: Path) -> None:
    pkg = quarantine / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    try:
        os.link(pkg / "a.py", pkg / "b.py")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlink creation is not permitted here")
    result = inspect(quarantine, str(pkg))
    assert result["status"] == "rejected"
    assert result["rejection"]["reason"] == "hardlink_member"


# ---------------------------------------------------------------------------
# Containment of the input path and of the quarantine root
# ---------------------------------------------------------------------------


def _code(call) -> str:
    with pytest.raises(pi.PluginInspectError) as caught:
        call()
    return caught.value.code


def test_absolute_drive_unc_and_rooted_paths_are_not_relative(tmp_path: Path, quarantine: Path) -> None:
    outside = write_zip(tmp_path / "outside.zip", [("a.py", "x = 1\n")])
    write_zip(quarantine / "pkg.zip", [("a.py", "x = 1\n")])
    for bad in (
        str(outside),
        str(quarantine / "pkg.zip"),  # absolute even when inside the root
        "C:/Windows",
        "c:evil.zip",
        "//server/share/x.zip",
        "\\\\server\\share\\x.zip",
        "/rooted.zip",
        "\\rooted.zip",
    ):
        assert _code(lambda bad=bad: pi.inspect_package(bad, str(quarantine))) == "path_not_relative", bad
        assert _code(lambda bad=bad: pi.closure_check(str(quarantine), [bad])) == "path_not_relative", bad


def test_traversal_segments_are_rejected(quarantine: Path) -> None:
    for bad in ("../outside.zip", "a/../../b", "..\\outside.zip", ".."):
        assert _code(lambda bad=bad: pi.inspect_package(bad, str(quarantine))) == "path_traversal", bad


def test_invalid_path_arguments(quarantine: Path) -> None:
    for bad in ("", "a\x00b", 42, None):
        assert _code(lambda bad=bad: pi.inspect_package(bad, str(quarantine))) == "path_invalid", bad


def test_quarantine_root_itself_missing_and_wrong_kind_paths(quarantine: Path) -> None:
    assert _code(lambda: pi.inspect_package(".", str(quarantine))) == "path_outside_quarantine"
    assert _code(lambda: pi.inspect_package("missing.zip", str(quarantine))) == "path_not_found"
    assert _code(lambda: pi.inspect_package("sub/missing.zip", str(quarantine))) == "path_not_found"


def test_quarantine_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    write_zip(tmp_path / "pkg.zip", [("a.py", "x = 1\n")])
    for root in (a_file, tmp_path / "missing", ""):
        assert _code(lambda root=root: pi.inspect_package("pkg.zip", str(root))) == "quarantine_root_invalid"
        assert _code(lambda root=root: pi.closure_check(str(root), [])) == "quarantine_root_invalid"


# --- resolve_quarantine_root: host-configured, task-owned root (design A3) ---------------------


def test_resolve_quarantine_root_prefers_env(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    env = {"KICAD_MCP_QUARANTINE_ROOT": str(configured), "LOCALAPPDATA": str(tmp_path / "lad")}
    root, source = pi.resolve_quarantine_root(env)
    assert (Path(root), source) == (configured, "KICAD_MCP_QUARANTINE_ROOT")
    assert configured.is_dir()  # created when absent
    assert not (tmp_path / "lad").exists()


def test_resolve_quarantine_root_default_under_localappdata(tmp_path: Path) -> None:
    for env in ({"LOCALAPPDATA": str(tmp_path / "lad")},
                {"LOCALAPPDATA": str(tmp_path / "lad"), "KICAD_MCP_QUARANTINE_ROOT": ""}):
        root, source = pi.resolve_quarantine_root(env)
        assert (Path(root), source) == (tmp_path / "lad" / "kicad-mcp" / "quarantine", "LOCALAPPDATA_default")
        assert Path(root).is_dir()


def test_resolve_quarantine_root_empty_localappdata_uses_the_known_folder_or_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi, "_known_local_appdata", lambda: str(tmp_path / "known-lad"))
    root, source = pi.resolve_quarantine_root({"LOCALAPPDATA": ""})
    assert (Path(root), source) == (tmp_path / "known-lad" / "kicad-mcp" / "quarantine", "LOCALAPPDATA_default")
    assert Path(root).is_dir()
    monkeypatch.setattr(pi, "_known_local_appdata", lambda: "")  # the OS lookup failed: refuse, never guess
    with pytest.raises(pi.PluginInspectError) as info:
        pi.resolve_quarantine_root({"LOCALAPPDATA": ""})
    assert info.value.code == "quarantine_root_invalid"


def test_resolve_quarantine_root_reads_os_environ_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_MCP_QUARANTINE_ROOT", str(tmp_path / "from-environ"))
    root, source = pi.resolve_quarantine_root()
    assert (Path(root), source) == (tmp_path / "from-environ", "KICAD_MCP_QUARANTINE_ROOT")


def test_resolve_quarantine_root_invalid(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    for configured in ("relative/root", str(a_file), str(a_file / "child"), "bad\x00root"):
        env = {"KICAD_MCP_QUARANTINE_ROOT": configured}
        assert _code(lambda env=env: pi.resolve_quarantine_root(env)) == "quarantine_root_invalid", configured


def test_relative_path_is_resolved_inside_the_quarantine(quarantine: Path) -> None:
    write_zip(quarantine / "pkg.zip", [("a.py", "x = 1\n")])
    assert pi.inspect_package("pkg.zip", str(quarantine))["status"] == "ok"


def test_symlink_escape_is_rejected(tmp_path: Path, quarantine: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write_zip(outside / "pkg.zip", [("a.py", "x = 1\n")])
    try_symlink(quarantine / "link", outside, True)
    assert _code(lambda: pi.inspect_package("link/pkg.zip", str(quarantine))) == "path_outside_quarantine"
    assert _code(lambda: pi.inspect_package("link", str(quarantine))) == "path_outside_quarantine"


def test_file_symlink_escape_is_rejected(tmp_path: Path, quarantine: Path) -> None:
    outside = write_zip(tmp_path / "outside.zip", [("a.py", "x = 1\n")])
    try_symlink(quarantine / "alias.zip", outside, False)
    assert _code(lambda: pi.inspect_package("alias.zip", str(quarantine))) == "path_outside_quarantine"


def test_junction_escape_is_rejected(tmp_path: Path, quarantine: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions exist on Windows only")
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    write_zip(outside / "pkg.zip", [("a.py", "x = 1\n")])
    try:
        _winapi.CreateJunction(str(outside), str(quarantine / "junction"))
    except OSError:
        pytest.skip("junction creation is not permitted here")
    assert _code(lambda: pi.inspect_package("junction/pkg.zip", str(quarantine))) == "path_outside_quarantine"
    assert _code(lambda: pi.inspect_package("junction", str(quarantine))) == "path_outside_quarantine"


# ---------------------------------------------------------------------------
# Recorded fields
# ---------------------------------------------------------------------------


def test_pcm_zip_record(quarantine: Path) -> None:
    members = pcm_members()
    path = write_zip(quarantine / "pcm.zip", list(members.items()))
    result = inspect(quarantine, str(path))
    assert result["status"] == "ok"
    assert result["executes_nothing"] is True
    assert result["kind"] == "pcm"
    assert result["ambiguities"] == []
    assert result["archive_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    by_path = {f["path"]: f for f in result["files"]}
    assert set(by_path) == set(members)
    for name, data in members.items():
        assert by_path[name]["sha256"] == sha(data)
        assert by_path[name]["size"] == len(_as_bytes(data))
    assert result["provenance"] == "unverified"
    assert result["claimed_publisher"] == "Ada Example"
    assert result["claimed_repository"] == "https://example.com/repo"
    assert result["claimed_identifier"] == "com.example.edge"
    assert result["claimed_resources"]["homepage"] == "https://example.com"
    assert result["license"] == "MIT"
    assert result["metadata"]["status"] == "ok"
    assert result["metadata"]["sha256"] == sha(members["metadata.json"])
    assert result["metadata"]["data"] == PCM_META
    assert result["compatibility"]["checked"] is True
    assert result["compatibility"]["compatible_with_pinned"] is True
    assert {"plugins", "resources"} <= set(result["directories"])
    limits = result["limits_applied"]
    for key in ("max_total_bytes", "max_member_bytes", "max_members", "bytes_read", "member_count"):
        assert key in limits
    assert limits["member_count"] == len(members)
    assert result["static_hints_note"] == "static hints, not a safety verdict"
    assert "static_hints" in result
    assert result["input"]["type"] == "zip"


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_ipc_record(quarantine: Path, mode: str) -> None:
    manifest = {
        "identifier": "com.example.ipc",
        "name": "Ipc Tool",
        "publisher": "Bob Example",
        "repository": "https://example.com/ipc",
        "license": "GPL-3.0-or-later",
        "runtime": {"type": "python", "min_version": "3.9", "entry": "main.py"},
        "actions": [{"identifier": "run", "name": "Run", "entrypoint": "main.py"}],
        "versions": [{"kicad_version": "9.0"}],
    }
    members = {"plugin.json": json.dumps(manifest), "main.py": "print('hi')\n"}
    result = inspect(quarantine, materialize(quarantine, "ipc", members, mode))
    assert result["kind"] == "ipc"
    assert result["claimed_publisher"] == "Bob Example"
    assert result["claimed_repository"] == "https://example.com/ipc"
    assert result["claimed_identifier"] == "com.example.ipc"
    assert result["provenance"] == "unverified"
    assert result["license"] == "GPL-3.0-or-later"
    assert result["entry_points"]["ipc_actions"] == manifest["actions"]
    assert result["entry_points"]["ipc_actions_status"] == "ok"
    assert result["declared_dependencies"]["plugin_json_runtime"] == manifest["runtime"]
    assert result["compatibility"] == {**result["compatibility"], "checked": True, "compatible_with_pinned": False}
    assert (result["archive_sha256"] is None) == (mode == "dir")


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_unknown_provenance_stays_unknown(quarantine: Path, mode: str) -> None:
    result = inspect(quarantine, materialize(quarantine, "pkg", {"tool.py": "x = 1\n"}, mode))
    assert result["kind"] == "legacy_scripting_module"
    assert result["claimed_publisher"] == "unknown"
    assert result["claimed_repository"] == "unknown"
    assert result["claimed_identifier"] == "unknown"
    assert result["provenance"] == "unverified"
    assert result["license"] == "unknown"
    assert result["metadata"]["status"] == "absent"
    assert result["compatibility"]["checked"] is False


def test_names_never_become_provenance(quarantine: Path) -> None:
    result = inspect(quarantine, materialize(quarantine, "kicad-official-plugin", {"tool.py": "x = 1\n"}, "zip"))
    assert result["claimed_publisher"] == "unknown"
    assert result["claimed_repository"] == "unknown"


def test_pcm_without_author_or_resources_is_unknown(quarantine: Path) -> None:
    members = pcm_members({"name": "n", "identifier": "i.d", "versions": []})
    result = inspect(quarantine, materialize(quarantine, "pcm", members, "zip"))
    assert result["claimed_publisher"] == "unknown"
    assert result["claimed_repository"] == "unknown"
    assert result["claimed_identifier"] == "i.d"
    assert result["license"] == "unknown"


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_kinds(quarantine: Path, mode: str) -> None:
    cases = {
        "legacy_scripting_package": {"__init__.py": "", "mod.py": "y = 2\n"},
        "legacy_scripting_module": {"tool.py": "x = 1\n"},
        "unknown": {"data.txt": "hello"},
        "pcm": {"metadata.json": "{}"},
        "ipc": {"plugin.json": "{}"},
    }
    for expected, members in cases.items():
        result = inspect(quarantine, materialize(quarantine, f"k-{expected}", members, mode))
        assert result["kind"] == expected, expected


def test_bare_py_file_is_a_legacy_module(quarantine: Path) -> None:
    tool = quarantine / "tool.py"
    tool.write_bytes(b"x = 1\n")  # bytes: text mode would translate to CRLF on Windows
    result = inspect(quarantine, str(tool))
    assert result["kind"] == "legacy_scripting_module"
    assert result["input"]["type"] == "py_file"
    assert result["archive_sha256"] is None
    assert [f["path"] for f in result["files"]] == ["tool.py"]
    assert result["files"][0]["sha256"] == sha("x = 1\n")


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_kind_ambiguities_are_listed(quarantine: Path, mode: str) -> None:
    both = inspect(quarantine, materialize(quarantine, "both", {"metadata.json": "{}", "plugin.json": "{}"}, mode))
    assert both["kind"] == "pcm"
    assert "metadata.json_and_plugin.json_both_at_root" in both["ambiguities"]
    nested = inspect(quarantine, materialize(quarantine, "nested", {"pkg/metadata.json": "{}"}, mode))
    assert nested["kind"] == "unknown"
    assert "marker_not_at_root:pkg/metadata.json" in nested["ambiguities"]
    many = inspect(quarantine, materialize(quarantine, "many", {"a.py": "", "b.py": ""}, mode))
    assert many["kind"] == "unknown"
    assert "multiple_top_level_py_modules" in many["ambiguities"]
    conflict = inspect(quarantine, materialize(quarantine, "conflict", {"metadata.json": "{}", "__init__.py": ""}, mode))
    assert conflict["kind"] == "pcm"
    assert "kind_markers_conflict:__init__.py_at_root" in conflict["ambiguities"]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        (b"\xff\xfe\x00bad", "UnicodeDecodeError"),
        (b"{not json", "JSONDecodeError"),
        (b"[1, 2, 3]", "not a JSON object"),
    ],
)
def test_invalid_metadata_is_reported_without_claims(quarantine: Path, payload: bytes, needle: str) -> None:
    result = inspect(quarantine, materialize(quarantine, "pcm", {"metadata.json": payload}, "zip"))
    assert result["kind"] == "pcm"
    assert result["metadata"]["status"] == "invalid_metadata"
    assert needle in result["metadata"]["error"]
    assert result["claimed_publisher"] == "unknown"
    assert result["license"] == "unknown"
    assert result["compatibility"]["checked"] is False


_ABSENT = object()


@pytest.mark.parametrize(
    ("versions", "checked", "compatible"),
    [
        (_ABSENT, False, None),
        ("10.0", False, None),
        ({"kicad_version": "10.0"}, False, None),
        ([], True, False),
        ([{"kicad_version": "9.0"}], True, False),
        ([{"kicad_version": "10.0"}], True, True),
        ([{"kicad_version": "10.0.5"}], True, True),
        ([{"kicad_version": "9.0"}, {"kicad_version": "10.0.1"}], True, True),
        ([{"kicad_version": "10.1"}], True, False),
        (["10.0"], True, False),
        ([{"kicad_version": 10}], True, False),
        ([{"other": "10.0"}], True, False),
    ],
)
def test_compatibility_prefix_heuristic(quarantine: Path, versions, checked: bool, compatible) -> None:
    meta = dict(PCM_META)
    if versions is _ABSENT:
        meta.pop("versions")
    else:
        meta["versions"] = versions
    result = inspect(quarantine, materialize(quarantine, "pcm", {"metadata.json": json.dumps(meta)}, "zip"))
    assert result["compatibility"]["checked"] is checked
    assert result["compatibility"]["compatible_with_pinned"] is compatible
    assert "not a compatibility determination" in result["compatibility"]["heuristic"]


REQUIREMENTS = (
    "# pinned and unpinned declarations\n"
    "requests==2.31.0\n"
    "Numpy>=1.20\n"
    "flask\n"
    "foo[extra]==1.0\n"
    "bar==1.*\n"
    'baz==1.0 ; python_version < "3.9"\n'
    "-r other.txt\n"
    "!!!bad line\n"
    "qux @ https://example.com/qux.whl\n"
)


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_requirements_pinned_vs_unpinned(quarantine: Path, mode: str) -> None:
    members = {"requirements.txt": REQUIREMENTS, "plugin.json": "{}"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, mode))["declared_dependencies"]
    assert deps["requirements_files"] == ["requirements.txt"]
    by_name = {r["name"]: r for r in deps["requirements"]}
    assert by_name["requests"]["classification"] == "pinned"
    assert by_name["requests"]["version"] == "2.31.0"
    assert by_name["baz"]["classification"] == "pinned"
    assert "marker_not_evaluated" in by_name["baz"]["notes"]
    expected_unpinned = {
        "numpy": "range",
        "flask": "no_pin",
        "foo": "extras_present",
        "bar": "wildcard",
        "qux": "direct_reference",
    }
    for name, note in expected_unpinned.items():
        assert by_name[name]["classification"] == "unpinned", name
        assert note in by_name[name]["notes"], name
    assert deps["pinned_count"] == 2
    assert deps["unpinned_count"] == 5
    assert deps["has_unpinned"] is True
    assert deps["blocks_execution"] is True
    assert deps["blocks_reasons"] == ["unpinned_requirement", "unverifiable_requirement_lines"]
    invalid = {(i["line"], i["reason"]) for i in deps["invalid_lines"]}
    assert invalid == {(8, "option_line_unsupported"), (9, "unparseable_requirement")}
    assert all(i["file"] == "requirements.txt" for i in deps["invalid_lines"])


def test_requirements_not_utf8_is_listed_as_invalid(quarantine: Path) -> None:
    members = {"requirements.txt": b"\xff\xfe\x00", "tool.py": "x = 1\n"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, "zip"))["declared_dependencies"]
    assert deps["requirements"] == []
    assert deps["invalid_lines"][0]["reason"] == "not_utf8"


def test_all_pinned_requirements_do_not_flag_unpinned(quarantine: Path) -> None:
    members = {"requirements.txt": "a==1.0\nb==2.0.1\n", "tool.py": "x = 1\n"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, "zip"))["declared_dependencies"]
    assert deps["has_unpinned"] is False
    assert deps["pinned_count"] == 2
    assert deps["blocks_execution"] is False
    assert deps["blocks_reasons"] == []


def test_only_unpinned_requirements_block_with_that_reason(quarantine: Path) -> None:
    members = {"requirements.txt": "a==1.0\nflask\n", "tool.py": "x = 1\n"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, "zip"))["declared_dependencies"]
    assert deps["has_unpinned"] is True and deps["invalid_lines"] == []
    assert deps["blocks_execution"] is True
    assert deps["blocks_reasons"] == ["unpinned_requirement"]


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_invalid_requirement_lines_alone_block(quarantine: Path, mode: str) -> None:
    text = (
        "-r other.txt\n"
        "--extra-index-url https://example.com/simple\n"
        "-e .\n"
        "pkg==1.0 \\\n"
        "!!!bad line\n"
    )
    members = {"requirements.txt": text, "tool.py": "x = 1\n"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, mode))["declared_dependencies"]
    assert deps["has_unpinned"] is False and deps["unpinned_count"] == 0
    assert len(deps["invalid_lines"]) == 5
    assert deps["blocks_execution"] is True
    assert deps["blocks_reasons"] == ["unverifiable_requirement_lines"]


def test_non_utf8_requirements_file_blocks(quarantine: Path) -> None:
    members = {"requirements.txt": b"\xff\xfe\x00", "tool.py": "x = 1\n"}
    deps = inspect(quarantine, materialize(quarantine, "reqs", members, "zip"))["declared_dependencies"]
    assert deps["blocks_execution"] is True
    assert deps["blocks_reasons"] == ["unverifiable_requirement_lines"]


# ---------------------------------------------------------------------------
# '.pyw' is Python source
# ---------------------------------------------------------------------------

PYW_SRC = "import subprocess\n\nclass WinPlugin(pcbnew.ActionPlugin):\n    pass\n"


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_pyw_is_python_source_in_packages(quarantine: Path, mode: str) -> None:
    members = {"__init__.py": "", "gui.pyw": PYW_SRC}
    result = inspect(quarantine, materialize(quarantine, "pkg", members, mode))
    assert result["kind"] == "legacy_scripting_package"
    assert result["importables"]["python_sources"] == ["__init__.py", "gui.pyw"]
    records = {r["path"]: r for r in result["entry_points"]["python_files"]}
    assert records["gui.pyw"]["parsed"] is True
    assert [(h["kind"], h["file"]) for h in result["static_hints"]] == [("subprocess_import", "gui.pyw")]
    assert [(c["class"], c["file"]) for c in result["entry_points"]["classes"]] == [("WinPlugin", "gui.pyw")]


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_lone_pyw_module_kind_and_bare_file_input(quarantine: Path, mode: str) -> None:
    result = inspect(quarantine, materialize(quarantine, "pkg", {"tool.pyw": PYW_SRC}, mode))
    assert result["kind"] == "legacy_scripting_module"
    assert result["importables"]["python_sources"] == ["tool.pyw"]
    many = inspect(quarantine, materialize(quarantine, "many", {"a.py": "", "b.pyw": ""}, mode))
    assert many["kind"] == "unknown" and "multiple_top_level_py_modules" in many["ambiguities"]


def test_bare_pyw_file_is_a_legacy_module(quarantine: Path) -> None:
    tool = quarantine / "tool.pyw"
    tool.write_bytes(PYW_SRC.encode())
    result = inspect(quarantine, str(tool))
    assert result["input"]["type"] == "py_file"
    assert result["kind"] == "legacy_scripting_module"
    assert result["importables"]["python_sources"] == ["tool.pyw"]
    assert result["entry_points"]["python_files"][0]["parsed"] is True
    assert [h["kind"] for h in result["static_hints"]] == ["subprocess_import"]


def test_pyw_sentinel_is_never_executed(tmp_path: Path, quarantine: Path) -> None:
    marker = tmp_path / "pyw.marker"
    members = {"sentinel_pyw_xyz.pyw": sentinel_source(marker)}
    result = inspect(quarantine, materialize(quarantine, "pkg", members, "zip"))
    assert result["importables"]["python_sources"] == ["sentinel_pyw_xyz.pyw"]
    assert not marker.exists()
    assert not [m for m in sys.modules if m.startswith("sentinel_pyw")]


# ---------------------------------------------------------------------------
# Entry points and parse errors
# ---------------------------------------------------------------------------

ENTRY_SRC = (
    "import pcbnew\n"
    "\n"
    "class Alpha(pcbnew.ActionPlugin):\n"
    "    pass\n"
    "\n"
    "class Beta(ActionPlugin):\n"
    "    pass\n"
    "\n"
    "class Gamma(pcbnew.FootprintWizardPlugin):\n"
    "    pass\n"
    "\n"
    "class Delta(MyCustomActionPlugin):\n"
    "    pass\n"
    "\n"
    "class Plain:\n"
    "    pass\n"
    "\n"
    "class Other(pcbnew.Something):\n"
    "    pass\n"
)


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_entry_point_classes(quarantine: Path, mode: str) -> None:
    result = inspect(quarantine, materialize(quarantine, "pkg", {"__init__.py": ENTRY_SRC}, mode))
    classes = result["entry_points"]["classes"]
    assert [(c["class"], c["plugin_type"], c["line"]) for c in classes] == [
        ("Alpha", "action_plugin", 3),
        ("Beta", "action_plugin", 6),
        ("Gamma", "footprint_wizard", 9),
        ("Delta", "action_plugin", 12),
    ]
    assert classes[0]["bases"] == ["pcbnew.ActionPlugin"]
    assert classes[0]["file"] == "__init__.py"
    assert result["entry_points"]["parse_errors"] == []
    assert result["entry_points"]["python_files"] == [{"path": "__init__.py", "parsed": True, "parse_error": None}]


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_parse_errors_are_recorded_not_raised(quarantine: Path, mode: str) -> None:
    members = {
        "good.py": ENTRY_SRC,
        "bad.py": "def (:\n",
        "latin.py": b"x = '\xe9'\n",
        "nullbyte.py": b"x = 1\x00\n",  # not "nul.py": that is a reserved device name and is rejected
    }
    result = inspect(quarantine, materialize(quarantine, "pkg", members, mode))
    assert result["status"] == "ok"
    records = {r["path"]: r for r in result["entry_points"]["python_files"]}
    assert records["good.py"]["parsed"] is True
    assert records["bad.py"]["parse_error"].startswith("SyntaxError")
    assert "UnicodeDecodeError" in records["latin.py"]["parse_error"]
    assert records["nullbyte.py"]["parse_error"] is not None
    assert {e["file"] for e in result["entry_points"]["parse_errors"]} == {"bad.py", "latin.py", "nullbyte.py"}
    assert len(result["entry_points"]["classes"]) == 4  # good.py still analysed


def test_oversized_source_is_hashed_but_not_parsed(quarantine: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "MAX_TEXT_BYTES", 10)
    source = "x = 1\n" * 20
    result = inspect(quarantine, materialize(quarantine, "pkg", {"big.py": source}, "zip"))
    record = result["entry_points"]["python_files"][0]
    assert record["parsed"] is False
    assert record["parse_skipped"] == "over_text_cap"
    assert result["files"][0]["sha256"] == sha(source)


# ---------------------------------------------------------------------------
# Static hints
# ---------------------------------------------------------------------------

HINT_LINES = [
    ("import urllib.request", ["network_import"]),
    ("import requests", ["network_import"]),
    ("import socket", ["network_import"]),
    ("import http.client", ["network_import"]),
    ("from http import server", ["network_import"]),
    ("from urllib.parse import quote", ["network_import"]),
    ("import subprocess", ["subprocess_import"]),
    ("import ctypes", ["ctypes_import"]),
    ("import os", []),
    ("os.system('echo')", ["os_system_call"]),
    ("os.popen('echo')", ["os_popen_call"]),
    ("from os import system, popen", ["os_system_import", "os_popen_import"]),
    ("eval('1')", ["eval_call"]),
    ("exec('x = 1')", ["exec_call"]),
    ("open('/etc/out.txt', 'w')", ["write_outside_package"]),
    ("open('../out.txt', mode='a')", ["write_outside_package"]),
    ("open('C:/out.txt', 'wb')", ["write_outside_package"]),
    ("open('local.txt', 'w')", []),
    ("open('readonly.txt')", []),
    ("open('readonly.txt', 'r')", []),
    ("open(some_path, 'w')", ["write_unknown_target"]),
    ("from pathlib import Path", []),
    ("Path('/tmp/x').write_text('y')", ["write_outside_package"]),
    ("Path('rel.txt').write_text('y')", []),
    ("Path(some_path).write_bytes(b'')", ["write_unknown_target"]),
    ("__import__('socket')", ["dynamic_import_call"]),
    ("import socketserver", []),
    ("import httpx", []),
    ("wx.FileDialog(None, 'Pick a file')", ["file_dialog"]),
    ("wx.DirDialog(None, 'Pick a folder')", ["file_dialog"]),
    ("dialog = wx.FileDialog(parent)", ["file_dialog"]),
    ("filedialog.askopenfilename()", ["file_dialog"]),
    ("tkinter.filedialog.asksaveasfilename(title='x')", ["file_dialog"]),
    ("tkinter.filedialog.askdirectory()", ["file_dialog"]),
    ("askopenfilenames()", ["file_dialog"]),
    ("QFileDialog.getOpenFileName(None)", ["file_dialog"]),
    ("QtWidgets.QFileDialog.getSaveFileName(None)", ["file_dialog"]),
    ("QFileDialog.getExistingDirectory(None)", ["file_dialog"]),
    ("pcbnew.GetOpenFileName()", ["file_dialog"]),
    ("pcbnew.FileDialog(None)", ["file_dialog"]),
    ("foo.FileDialog()", []),
    ("dialog.ShowModal()", []),
    ("askopenfilename_unrelated = 1", []),
]


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_every_static_hint_with_file_and_line(quarantine: Path, mode: str) -> None:
    source = "\n".join(line for line, _ in HINT_LINES) + "\n"
    result = inspect(quarantine, materialize(quarantine, "pkg", {"hints.py": source}, mode))
    assert result["static_hints_note"] == "static hints, not a safety verdict"
    hints = result["static_hints"]
    assert all(h["file"] == "hints.py" and isinstance(h["line"], int) for h in hints)
    seen: dict[int, list[str]] = {}
    for hint in hints:
        seen.setdefault(hint["line"], []).append(hint["kind"])
    for number, (line, expected) in enumerate(HINT_LINES, 1):
        assert sorted(seen.get(number, [])) == sorted(expected), f"line {number}: {line}"


def test_hints_cover_every_required_family(quarantine: Path) -> None:
    source = "\n".join(line for line, _ in HINT_LINES) + "\n"
    result = inspect(quarantine, materialize(quarantine, "pkg", {"hints.py": source}, "zip"))
    kinds = {h["kind"] for h in result["static_hints"]}
    assert {
        "network_import",
        "subprocess_import",
        "ctypes_import",
        "os_system_call",
        "os_popen_call",
        "eval_call",
        "exec_call",
        "write_outside_package",
        "file_dialog",
    } <= kinds


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_file_dialog_hint_has_file_line_and_detail(quarantine: Path, mode: str) -> None:
    source = "import wx\n\ndlg = wx.FileDialog(None, 'x')\nfrom tkinter import filedialog\nfiledialog.askdirectory()\n"
    result = inspect(quarantine, materialize(quarantine, "pkg", {"dialogs.py": source}, mode))
    dialogs = [h for h in result["static_hints"] if h["kind"] == "file_dialog"]
    assert [(h["file"], h["line"]) for h in dialogs] == [("dialogs.py", 3), ("dialogs.py", 5)]
    assert dialogs[0]["detail"] == "wx.FileDialog(...)"
    assert dialogs[1]["detail"] == "filedialog.askdirectory(...)"
    assert result["static_hints_note"] == "static hints, not a safety verdict"


def test_clean_source_has_no_hints(quarantine: Path) -> None:
    result = inspect(quarantine, materialize(quarantine, "pkg", {"clean.py": "import math\nx = math.pi\n"}, "zip"))
    assert result["static_hints"] == []


# ---------------------------------------------------------------------------
# Non-.py importables and build/hook files
# ---------------------------------------------------------------------------

IMPORTABLE_MEMBERS = {
    "__init__.py": "",
    "native.pyd": b"MZ",
    "lib.so": b"\x7fELF",
    "lib2.so.1": b"x",
    "legacy.pyc": b"\x00\x01",
    "__pycache__/mod.cpython-311.pyc": b"x",
    "ns/inner.py": "x = 1\n",
    "ns/deeper/mod.py": "y = 2\n",
    "pkgdir/__init__.py": "",
    "pkgdir/m.py": "z = 3\n",
    "docs/readme.txt": "text",
    "evil.pth": "import sentinel_pth\n/some/dir\n",
    "setup.py": "from setuptools import setup\nsetup()\n",
    "pyproject.toml": '[build-system]\nbuild-backend = "sentinel_backend"\n',
    "setup.cfg": "[metadata]\nname = x\n",
    "entry_points.txt": "[console_scripts]\nrun = sentinel_ep:main\n",
    "sitecustomize.py": "x = 1\n",
}


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_non_py_importables_and_hook_files(quarantine: Path, mode: str) -> None:
    result = inspect(quarantine, materialize(quarantine, "pkg", IMPORTABLE_MEMBERS, mode))
    assert result["kind"] == "legacy_scripting_package"
    imp = result["importables"]
    assert {p["path"] for p in imp["sourceless_bytecode"]} == {"legacy.pyc", "__pycache__/mod.cpython-311.pyc"}
    assert {p["path"] for p in imp["extension_modules"]} == {"native.pyd", "lib.so", "lib2.so.1"}
    for item in (*imp["sourceless_bytecode"], *imp["extension_modules"]):
        assert item["status"] == NOT_EXECUTED
        assert item["sha256"] == sha(IMPORTABLE_MEMBERS[item["path"]])
    namespaces = {d["path"]: d["top_level"] for d in imp["namespace_dirs"]}
    assert namespaces == {"ns": True, "ns/deeper": False}
    assert imp["pycache_dirs"] == [
        {"path": "__pycache__", "top_level": True, "files": ["__pycache__/mod.cpython-311.pyc"]}
    ]
    hooks = {h["type"]: h for h in imp["build_hook_files"]}
    assert set(hooks) == {"pth_file", "setup.py", "pyproject.toml", "setup.cfg", "entry_points.txt", "sitecustomize.py"}
    assert all(h["status"] == NOT_EXECUTED for h in hooks.values())
    assert hooks["pth_file"]["import_lines"] == [1]
    assert hooks["pyproject.toml"]["build_backend"] == "sentinel_backend"
    assert hooks["entry_points.txt"]["entry_points"] == {"console_scripts": {"run": "sentinel_ep:main"}}
    assert hooks["setup.py"]["sha256"] == sha(IMPORTABLE_MEMBERS["setup.py"])
    assert "setup.py" in imp["python_sources"]


def test_explicit_directory_entries_are_recorded(quarantine: Path) -> None:
    archive = write_zip(quarantine / "pkg.zip", [("pkg/", None), ("empty/", None), ("pkg/a.py", "x = 1\n")])
    assert inspect(quarantine, str(archive))["directories"] == ["empty", "pkg"]


# ---------------------------------------------------------------------------
# Nothing is imported, compiled or executed
# ---------------------------------------------------------------------------

SENTINEL_NAMES = [
    "sentinel_mod_xyz",
    "sentinel_pkg_xyz",
    "sentinel_setup_xyz",
    "sentinel_pth_xyz",
    "sentinel_backend_xyz",
    "sentinel_ep_xyz",
    "sentinel_runtime_xyz",
]


def sentinel_members(markers: Path) -> dict:
    def src(name: str) -> str:
        return sentinel_source(markers / name)

    plugin = {
        "identifier": "com.example.sentinel",
        "runtime": {"type": "python", "entry": "sentinel_runtime_xyz", "command": ["python", "-c", "boom"]},
        "actions": [{"identifier": "go", "entrypoint": "sentinel_runtime_xyz:main"}],
    }
    return {
        "sentinel_mod_xyz.py": src("sentinel_mod_xyz"),
        "sentinel_pkg_xyz/__init__.py": src("sentinel_pkg_xyz"),
        "setup.py": src("sentinel_setup_xyz"),
        "evil.pth": "import sentinel_pth_xyz\n",
        "sentinel_pth_xyz.py": src("sentinel_pth_xyz"),
        "pyproject.toml": '[build-system]\nrequires = []\nbuild-backend = "sentinel_backend_xyz"\n',
        "sentinel_backend_xyz.py": src("sentinel_backend_xyz"),
        "entry_points.txt": "[console_scripts]\nrun = sentinel_ep_xyz:main\n",
        "sentinel_ep_xyz.py": src("sentinel_ep_xyz"),
        "plugin.json": json.dumps(plugin),
        "sentinel_runtime_xyz.py": src("sentinel_runtime_xyz"),
    }


@pytest.mark.parametrize("mode", ["zip", "dir"])
def test_nothing_from_the_package_is_imported_or_executed(tmp_path: Path, quarantine: Path, mode: str) -> None:
    markers = tmp_path / "markers"
    markers.mkdir()
    target = materialize(quarantine, "sentinel", sentinel_members(markers), mode)
    before_files = snapshot(quarantine)
    before_modules = set(sys.modules)
    imported: list[str] = []
    real_import = builtins.__import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = recording_import
    try:
        result = inspect(quarantine, target)
    finally:
        builtins.__import__ = real_import
    assert result["status"] == "ok"
    assert result["kind"] == "ipc"
    for name in SENTINEL_NAMES:
        assert not (markers / name).exists(), f"{name} marker exists: its code ran"
    assert list(markers.iterdir()) == []
    assert not [n for n in imported if n.startswith("sentinel_")]
    assert not [m for m in set(sys.modules) - before_modules if m.startswith("sentinel_")]
    assert not [m for m in sys.modules if m.startswith("sentinel_")]
    assert snapshot(quarantine) == before_files  # nothing written, no bytecode cache
    if mode == "dir":
        assert not list((quarantine / "sentinel").rglob("__pycache__"))
    # The hook files were discovered and recorded as data only.
    hooks = {h["type"]: h for h in result["importables"]["build_hook_files"]}
    assert hooks["pyproject.toml"]["build_backend"] == "sentinel_backend_xyz"
    assert hooks["pth_file"]["import_lines"] == [1]
    assert hooks["entry_points.txt"]["entry_points"] == {"console_scripts": {"run": "sentinel_ep_xyz:main"}}
    assert hooks["setup.py"]["status"] == NOT_EXECUTED
    runtime = result["declared_dependencies"]["plugin_json_runtime"]
    assert runtime["entry"] == "sentinel_runtime_xyz"
    assert result["entry_points"]["ipc_actions"][0]["entrypoint"] == "sentinel_runtime_xyz:main"


def test_inspection_does_not_write_or_change_the_quarantine(quarantine: Path) -> None:
    write_zip(quarantine / "a.zip", list(pcm_members().items()))
    materialize(quarantine, "b", IMPORTABLE_MEMBERS, "dir")
    before = snapshot(quarantine)
    first = inspect(quarantine, str(quarantine / "a.zip"))
    second = inspect(quarantine, str(quarantine / "b"))
    again = inspect(quarantine, str(quarantine / "a.zip"))
    assert first == again
    assert second["status"] == "ok"
    assert snapshot(quarantine) == before


# ---------------------------------------------------------------------------
# D1 offline closure check
# ---------------------------------------------------------------------------


def make_wheel(root: Path, name: str, version: str, requires=(), extra_files=None) -> Path:
    root.mkdir(exist_ok=True)
    dist = name.replace("-", "_")
    path = root / f"{dist}-{version}-py3-none-any.whl"
    headers = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    headers += [f"Requires-Dist: {item}" for item in requires]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{dist}-{version}.dist-info/METADATA", "\n".join(headers) + "\n\nDescription body\n")
        archive.writestr(f"{dist}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(f"{dist}/__init__.py", "")
        for member, data in (extra_files or {}).items():
            archive.writestr(member, data)
    return path


def closure(root: Path, wheels, **kwargs) -> dict:
    return pi.closure_check(str(root), [rel(root, w) for w in wheels], **kwargs)


def test_clean_closure(tmp_path: Path, quarantine: Path) -> None:
    markers = tmp_path / "markers"
    markers.mkdir()
    wheels = quarantine / "wheels"
    sentinel_files = {
        "sentinel_wheel_xyz.py": sentinel_source(markers / "wheel"),
        "evil.pth": "import sentinel_wheel_xyz\n",
    }
    app = make_wheel(
        wheels,
        "app",
        "1.0",
        ["lib==2.0", 'docs-tool==1.0; extra == "docs"'],
        extra_files=sentinel_files,
    )
    lib = make_wheel(wheels, "lib", "2.0", ['leaf==3.1; python_version < "3.12"'])
    leaf = make_wheel(wheels, "leaf", "3.1")
    result = closure(quarantine, [app, lib, leaf])
    assert result["blocks"] is False
    assert result["blocks_reasons"] == []
    assert result["conflicts"] == result["missing"] == result["unpinned"] == result["wheel_errors"] == []
    assert result["executes_nothing"] is True
    assert result["network_used"] is False
    members = {m["name"]: m for m in result["members"]}
    assert list(members) == ["app", "leaf", "lib"]
    assert members["lib"]["version"] == "2.0"
    assert members["lib"]["wheel_filename"] == lib.name
    assert members["lib"]["wheel_sha256"] == hashlib.sha256(lib.read_bytes()).hexdigest()
    assert members["app"]["root"] is True
    assert members["lib"]["required_by"] == ["app==1.0"]
    assert members["leaf"]["required_by"] == ["lib==2.0"]
    assert [e["requirement"] for e in result["skipped_extras"]] == ['docs-tool==1.0; extra == "docs"']
    assert result["skipped_extras"][0]["required_by"] == "app==1.0"
    assert [e["status"] for e in result["marker_not_evaluated"]] == ["marker_not_evaluated"]
    assert result["marker_not_evaluated"][0]["marker"] == 'python_version < "3.12"'
    assert list(markers.iterdir()) == []  # wheel code never ran
    assert not [m for m in sys.modules if m.startswith("sentinel_")]


def test_conflicting_pins_and_wheel_version_mismatch(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0", ["lib==1.0"])
    other = make_wheel(wheels, "other", "1.0", ["lib==2.0"])
    lib = make_wheel(wheels, "lib", "2.0")
    result = closure(quarantine, [app, other, lib])
    assert result["blocks"] is True
    assert "conflict" in result["blocks_reasons"]
    types = {c["type"] for c in result["conflicts"]}
    assert types == {"present_wheel_version_differs_from_pin", "conflicting_pins"}
    mismatch = next(c for c in result["conflicts"] if c["type"] == "present_wheel_version_differs_from_pin")
    assert mismatch["name"] == "lib"
    assert mismatch["pinned"] == "1.0"
    assert mismatch["present"] == ["2.0"]
    pins = next(c for c in result["conflicts"] if c["type"] == "conflicting_pins")
    assert {p["version"] for p in pins["pins"]} == {"1.0", "2.0"}


def test_two_wheels_for_one_name_conflict(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    first = make_wheel(wheels, "lib", "1.0")
    second = make_wheel(wheels, "lib", "2.0")
    result = closure(quarantine, [first, second])
    assert result["blocks"] is True
    assert [c["type"] for c in result["conflicts"]] == ["multiple_wheel_versions"]
    assert result["conflicts"][0]["versions"] == ["1.0", "2.0"]


def test_missing_closure_member(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0", ["ghost==1.0"])
    result = closure(quarantine, [app])
    assert result["blocks"] is True
    assert result["blocks_reasons"] == ["missing"]
    assert result["missing"] == [{"name": "ghost", "required_by": ["app==1.0"], "pinned_versions": ["1.0"]}]


def test_unpinned_requirements_block(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0", ["lib>=1.0", "other", "third (>=1,<2)"])
    lib = make_wheel(wheels, "lib", "1.5")
    other = make_wheel(wheels, "other", "1.0")
    third = make_wheel(wheels, "third", "1.2")
    result = closure(quarantine, [app, lib, other, third])
    assert result["blocks"] is True
    assert result["blocks_reasons"] == ["unpinned"]
    assert {u["name"] for u in result["unpinned"]} == {"lib", "other", "third"}
    assert all(u["status"] == "unpinned" for u in result["unpinned"])
    notes = {u["name"]: u["notes"] for u in result["unpinned"]}
    assert "range" in notes["lib"]
    assert "no_pin" in notes["other"]
    assert result["conflicts"] == [] and result["missing"] == []


def test_cycle_terminates_without_conflict(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    first = make_wheel(wheels, "cyc-a", "1.0", ["cyc-b==1.0"])
    second = make_wheel(wheels, "cyc-b", "1.0", ["cyc-a==1.0"])
    result = closure(quarantine, [first, second])
    assert result["blocks"] is False
    assert {m["name"] for m in result["members"]} == {"cyc-a", "cyc-b"}
    assert all(m["root"] is False for m in result["members"])


def test_names_are_normalised_and_versions_compared_canonically(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0", ["My.Pkg_Name==1.0.0"])
    lib = make_wheel(wheels, "My_Pkg.Name", "1.0")
    result = closure(quarantine, [app, lib])
    assert result["blocks"] is False
    assert {m["name"] for m in result["members"]} == {"app", "my-pkg-name"}


def test_unreadable_wheels_are_reported_and_block(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    wheels.mkdir()
    no_metadata = wheels / "nometa-1.0-py3-none-any.whl"
    with zipfile.ZipFile(no_metadata, "w") as archive:
        archive.writestr("nometa/__init__.py", "")
    junk = wheels / "junk-1.0-py3-none-any.whl"
    junk.write_bytes(b"not a zip")
    good = make_wheel(wheels, "good", "1.0")
    result = closure(quarantine, [no_metadata, junk, good])
    assert result["blocks"] is True
    assert result["blocks_reasons"] == ["wheel_error"]
    reasons = {e["wheel_filename"]: e["reason"] for e in result["wheel_errors"]}
    assert reasons == {no_metadata.name: "no_metadata", junk.name: "corrupt_archive"}
    assert [m["name"] for m in result["members"]] == ["good"]


def test_invalid_requirement_in_metadata_is_reported(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0", ["!!!nonsense"])
    result = closure(quarantine, [app])
    assert result["blocks"] is True
    assert result["wheel_errors"][0]["reason"] == "invalid_requirement"


def test_wheel_size_override_and_duplicate_paths(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    app = make_wheel(wheels, "app", "1.0")
    result = closure(quarantine, [app], max_wheel_bytes=10)
    assert result["wheel_errors"][0]["reason"] == "archive_too_large"
    twice = closure(quarantine, [app, app])
    assert len(twice["members"]) == 1


def test_wheel_outside_quarantine_raises(tmp_path: Path, quarantine: Path) -> None:
    outside = make_wheel(tmp_path / "outside", "app", "1.0")
    make_wheel(quarantine / "wheels", "ok", "1.0")
    # absolute or traversing names are refused before anything is read
    assert _code(lambda: pi.closure_check(str(quarantine), [str(outside)])) == "path_not_relative"
    assert _code(lambda: pi.closure_check(str(quarantine), ["../outside/" + outside.name])) == "path_traversal"
    # one bad path refuses the whole request
    assert _code(lambda: pi.closure_check(
        str(quarantine), ["wheels/ok-1.0-py3-none-any.whl", "../outside/" + outside.name])) == "path_traversal"
    assert _code(lambda: pi.closure_check(str(quarantine), ["wheels/missing.whl"])) == "path_not_found"


def test_wheel_argument_validation(quarantine: Path) -> None:
    wheels = quarantine / "wheels"
    wheel = make_wheel(wheels, "app", "1.0")
    assert _code(lambda: pi.closure_check(str(quarantine), str(wheel))) == "wheels_invalid"  # bare string
    assert _code(lambda: pi.closure_check(str(quarantine), None)) == "wheels_invalid"
    assert _code(lambda: pi.closure_check(str(quarantine), ["wheels"])) == "wheel_invalid"  # a directory
    plain = quarantine / "notes.txt"
    plain.write_text("x")
    assert _code(lambda: pi.closure_check(str(quarantine), ["notes.txt"])) == "wheel_invalid"  # wrong suffix
    assert pi.closure_check(str(quarantine), [])["members"] == []
    assert pi.closure_check(str(quarantine), [])["blocks"] is False


def test_wheel_symlink_escape_raises(tmp_path: Path, quarantine: Path) -> None:
    outside = make_wheel(tmp_path / "outside", "app", "1.0")
    try_symlink(quarantine / outside.name, outside, False)
    assert _code(lambda: pi.closure_check(str(quarantine), [outside.name])) == "wheel_outside_quarantine"
