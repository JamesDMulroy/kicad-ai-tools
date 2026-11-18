"""Offline tests for plugin root resolution and the D0 inventory (KiCad 10.0.5 PATHS mirror).

Env vars and the known-folder resolver are monkeypatched and everything lives
under tmp_path; the real user profile is never read or written. No test imports,
compiles or executes any fixture file: sentinel modules write a marker if they
are ever imported, and the markers must stay absent.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from kicad_mcp.utils import plugin_catalog

LINK = "link_entry_not_followed"

ROOT_KEYS = ("legacy_scripting_plugins", "user_scripting_plugins", "pcm_third_party_packages",
             "ipc_addon_plugins", "settings_root")
NEW_ROOT_KEYS = ("legacy_scripting_root_modules", "user_scripting_root_modules",
                 "settings_scripting_root_modules", "settings_scripting_plugins",
                 "pcm_installed_package_code", "installed_packages_index")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate every root under tmp_path; the Documents known folder resolves to <tmp>/home/Documents."""
    for name in ("KICAD_DOCUMENTS_HOME", "KICAD10_3RD_PARTY", "KICAD_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "app"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(plugin_catalog.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(plugin_catalog, "_known_documents_folder", lambda: tmp_path / "home" / "Documents")
    return tmp_path


def _docs(base: Path) -> Path:
    return base / "KiCad" / "10.0"


def _settings(base: Path) -> Path:
    return base / "appdata" / "kicad" / "10.0"


def _app_scripting(base: Path) -> Path:
    return base / "app" / "share" / "kicad" / "scripting"


def _put(path: Path, data: bytes | str = b"x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")


def _by_name(section: dict) -> dict:
    return {e.get("filename") or e["package_dir"]: e for e in section["entries"]}


def test_documents_home_env_takes_precedence(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "envdocs"))
    monkeypatch.setattr(plugin_catalog, "_known_documents_folder", lambda: env / "known")
    result = plugin_catalog.inventory_plugins()
    ipc = result["ipc_addon_plugins"]
    assert Path(ipc["path"]) == _docs(env / "envdocs") / "plugins"
    assert ipc["resolved_from"] == "KICAD_DOCUMENTS_HOME"


EMPTY = "kicad_documents_home_empty"


def test_empty_documents_home_is_reported_not_scanned_and_has_no_fallback(env, monkeypatch):
    """KiCad treats a set-but-empty KICAD_DOCUMENTS_HOME as set; so does the inventory."""
    known = env / "known"
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", "")
    monkeypatch.setattr(plugin_catalog, "_known_documents_folder", lambda: known)
    # content at every fallback location must stay unseen
    _put(_docs(known) / "plugins" / "x" / "plugin.json", "{}")
    _put(_docs(known) / "scripting" / "plugins" / "fallback.py", b"x\n")
    _put(_docs(env / "home" / "Documents") / "plugins" / "x" / "plugin.json", "{}")
    result = plugin_catalog.inventory_plugins()
    for section in (result["user_scripting_plugins"], result["user_scripting_root_modules"],
                    result["ipc_addon_plugins"], result["pcm_third_party_packages"],
                    result["pcm_installed_package_code"],
                    result["ipc_plugin_json_discovery"]["user_plugins"],
                    result["ipc_plugin_json_discovery"]["third_party"]):
        assert section["status"] == EMPTY
        assert section["path"] is None
        assert section["resolved_from"] == "KICAD_DOCUMENTS_HOME"
        assert "KICAD_DOCUMENTS_HOME" in section["reason"]
    for section in (result["user_scripting_plugins"], result["user_scripting_root_modules"],
                    result["ipc_addon_plugins"], result["pcm_third_party_packages"],
                    result["ipc_plugin_json_discovery"]["user_plugins"],
                    result["ipc_plugin_json_discovery"]["third_party"]):
        assert section["entries"] == []
    assert result["pcm_installed_package_code"]["categories"] == []


def test_empty_documents_home_leaves_non_documents_roots_unaffected(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", "")
    _put(_app_scripting(env) / "plugins" / "stock.py", b"stock\n")
    _put(_app_scripting(env) / "rootmod.py", b"rootmod\n")
    _put(env / "app" / "share" / "kicad" / "plugins" / "sp" / "plugin.json", "{}")
    _put(_settings(env) / "scripting" / "plugins" / "settingsplug.py", b"sp\n")
    _put(_settings(env) / "installed_packages.json", json.dumps({"packages": {}}))
    result = plugin_catalog.inventory_plugins()
    assert set(_by_name(result["legacy_scripting_plugins"])) == {"stock.py"}
    assert set(_by_name(result["legacy_scripting_root_modules"])) == {"rootmod.py"}
    assert [e["relative_path"] for e in
            result["ipc_plugin_json_discovery"]["stock_plugins"]["entries"]] == ["sp/plugin.json"]
    assert set(_by_name(result["settings_scripting_plugins"])) == {"settingsplug.py"}
    assert result["installed_packages_index"]["status"] == "ok"
    assert result["settings_root"]["status"] == "present"
    assert result["settings_root"]["resolved_from"] == "APPDATA"


@pytest.mark.parametrize("source", ["env", "saved"])
def test_third_party_root_still_scanned_when_documents_home_is_empty(env, monkeypatch, source):
    third = env / "3p"
    _put(third / "pkg" / "metadata.json", "{}")
    if source == "env":
        monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    else:
        _write_common(env, {"KICAD10_3RD_PARTY": str(third)})
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", "")
    result = plugin_catalog.inventory_plugins()
    pcm = result["pcm_third_party_packages"]
    assert pcm["status"] == "present" and Path(pcm["path"]) == third
    assert pcm["resolved_from"] == ("env" if source == "env" else "kicad_common_saved")
    assert [e["package_dir"] for e in pcm["entries"]] == ["pkg"]
    assert result["pcm_installed_package_code"]["status"] == "present"
    assert result["ipc_plugin_json_discovery"]["third_party"]["status"] == "present"
    # the documents-derived roots are still reported as empty
    assert result["ipc_addon_plugins"]["status"] == EMPTY


def test_known_folder_used_via_fake_resolver(env, monkeypatch):
    monkeypatch.setattr(plugin_catalog, "_known_documents_folder", lambda: env / "OneDrive" / "Documents")
    result = plugin_catalog.inventory_plugins()
    ipc = result["ipc_addon_plugins"]
    assert Path(ipc["path"]) == _docs(env / "OneDrive" / "Documents") / "plugins"
    assert ipc["resolved_from"] == "known_folder"
    assert result["user_scripting_plugins"]["resolved_from"] == "known_folder"
    assert result["user_scripting_root_modules"]["resolved_from"] == "known_folder"


@pytest.mark.parametrize("failure", ["none", "raise"])
def test_unresolved_documents_are_reported_not_guessed(env, monkeypatch, failure):
    def boom():
        raise OSError("no known folder")

    monkeypatch.setattr(plugin_catalog, "_known_documents_folder", boom if failure == "raise" else (lambda: None))
    ipc = plugin_catalog.inventory_plugins()["ipc_addon_plugins"]
    if sys.platform == "win32":
        assert ipc["status"] == "documents_unresolved" and ipc["path"] is None
        assert "KICAD_DOCUMENTS_HOME" in ipc["reason"]
    else:
        assert Path(ipc["path"]) == _docs(env / "home" / "Documents") / "plugins"
        assert ipc["resolved_from"] == "os_standard_documents"


def test_kicad_root_status_is_explicit(env, monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "not-kicad"))
    root = plugin_catalog.inventory_plugins()["kicad_root"]
    assert root["status"] == "absent" and root["resolved_from"] == "KICAD_APP_PATH"
    assert "not a KiCad installation" in root["error"]


def test_third_party_env_override(env, monkeypatch):
    target = env / "custom3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(target))
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == target
    assert pcm["resolved_from"] == "env"


def test_third_party_empty_string_ignored(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    monkeypatch.setenv("KICAD10_3RD_PARTY", "")
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == _docs(env / "docs") / "3rdparty"
    assert pcm["resolved_from"] == "default"


# --- KICAD10_3RD_PARTY precedence: process env, saved kicad_common.json value, <docs>/3rdparty --------

def _write_common(base: Path, variables, *, config_home: Path | None = None) -> Path:
    """Write kicad_common.json at <settings root> with environment.vars = variables."""
    root = (config_home / "10.0") if config_home else _settings(base)
    return _put(root / "kicad_common.json", json.dumps({"environment": {"vars": variables}}))


def test_third_party_saved_value_used_when_env_absent(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    saved = env / "saved3p"
    _put(saved / "pkg" / "metadata.json", "{}")
    _write_common(env, {"KICAD10_3RD_PARTY": str(saved)})
    result = plugin_catalog.inventory_plugins()
    for section in (result["pcm_third_party_packages"], result["pcm_installed_package_code"],
                    result["ipc_plugin_json_discovery"]["third_party"]):
        assert Path(section["path"]) == saved
        assert section["resolved_from"] == "kicad_common_saved"
        assert section["status"] == "present"
    assert [e["package_dir"] for e in result["pcm_third_party_packages"]["entries"]] == ["pkg"]


def test_third_party_env_beats_saved_value(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    _write_common(env, {"KICAD10_3RD_PARTY": str(env / "saved3p")})
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(env / "envp"))
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == env / "envp" and pcm["resolved_from"] == "env"


def test_third_party_default_when_neither_env_nor_saved(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == _docs(env / "docs") / "3rdparty"
    assert pcm["resolved_from"] == "default"


def test_third_party_saved_value_follows_kicad_config_home(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    cfg = env / "cfg"
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))
    _write_common(env, {"KICAD10_3RD_PARTY": str(env / "from_cfg")}, config_home=cfg)
    _write_common(env, {"KICAD10_3RD_PARTY": str(env / "from_appdata")})  # decoy under APPDATA
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == env / "from_cfg" and pcm["resolved_from"] == "kicad_common_saved"


@pytest.mark.parametrize("payload", [
    b"{not json", b"\xff\xfe\x00", b"[]", b'"text"', b"null", b"{}",
    b'{"environment": []}', b'{"environment": {}}', b'{"environment": {"vars": []}}',
    b'{"environment": {"vars": {}}}',
    b'{"environment": {"vars": {"KICAD10_3RD_PARTY": 123}}}',
    b'{"environment": {"vars": {"KICAD10_3RD_PARTY": null}}}',
    b'{"environment": {"vars": {"KICAD10_3RD_PARTY": ["a"]}}}',
    b'{"environment": {"vars": {"KICAD10_3RD_PARTY": ""}}}',
    b'{"environment": {"vars": {"KICAD10_3RD_PARTY": "   "}}}',
])
def test_third_party_invalid_or_empty_saved_file_falls_back_to_default(env, monkeypatch, payload):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    _put(_settings(env) / "kicad_common.json", payload)
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert Path(pcm["path"]) == _docs(env / "docs") / "3rdparty"
    assert pcm["resolved_from"] == "default"


def test_third_party_saved_file_size_capped(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    _write_common(env, {"KICAD10_3RD_PARTY": str(env / "saved3p")})
    monkeypatch.setattr(plugin_catalog, "MAX_COMMON_JSON_BYTES", 10)
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert pcm["resolved_from"] == "default"


def test_third_party_saved_file_link_outside_is_not_read(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    elsewhere = _put(env / "elsewhere.json", json.dumps(
        {"environment": {"vars": {"KICAD10_3RD_PARTY": str(env / "saved3p")}}}))
    _symlink(_settings(env) / "kicad_common.json", elsewhere, directory=False)
    pcm = plugin_catalog.inventory_plugins()["pcm_third_party_packages"]
    assert pcm["resolved_from"] == "default"


def test_default_third_party_under_docs_not_appdata(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    pkg = _docs(env / "docs") / "3rdparty" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_text("{}", encoding="utf-8")
    result = plugin_catalog.inventory_plugins()
    pcm = result["pcm_third_party_packages"]
    assert pcm["status"] == "present"
    assert "appdata" not in Path(pcm["path"]).parts
    assert pcm["entries"][0]["package_dir"] == "pkg"
    assert result["settings_root"]["resolved_from"] == "APPDATA"


def _make_user_plugins(env, monkeypatch) -> Path:
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    root = _docs(env / "docs") / "scripting" / "plugins"
    root.mkdir(parents=True)
    return root


def test_user_scripting_files_and_packages_hashed(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    (root / "single.py").write_bytes(b"print('single')\n")
    (root / "__init__.py").write_bytes(b"# top-level init is skipped\n")
    (root / "notes.txt").write_bytes(b"not python")
    pkg = root / "edge_plating"
    pkg.mkdir()
    (pkg / "__init__.py").write_bytes(b"from .core import X\n")
    (pkg / "core.py").write_bytes(b"X = 1\n")
    (pkg / "README.md").write_bytes(b"readme\n")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "core.cpython-312.pyc").write_bytes(b"\x00\x01")
    loose = root / "not_a_package"
    loose.mkdir()
    (loose / "x.py").write_bytes(b"x = 1\n")

    section = plugin_catalog.inventory_plugins()["user_scripting_plugins"]
    assert section["status"] == "present"
    by_name = _by_name(section)
    assert set(by_name) == {"single.py", "edge_plating", "not_a_package"}

    assert by_name["single.py"]["sha256"] == _sha(b"print('single')\n")
    assert "not imported, parsed, or executed" in by_name["single.py"]["note"]

    files = {f["filename"]: f["sha256"] for f in by_name["edge_plating"]["files"]}
    assert files == {"__init__.py": _sha(b"from .core import X\n"),
                     "core.py": _sha(b"X = 1\n"),
                     "README.md": _sha(b"readme\n")}
    assert "status" not in by_name["edge_plating"]
    # the package-level __pycache__ is reported separately, not mixed into the file list
    assert by_name["edge_plating"]["pycache_dirs"] == [
        {"directory": "__pycache__",
         "files": [{"filename": "__pycache__/core.cpython-312.pyc", "sha256": _sha(b"\x00\x01")}]}]

    # a directory without __init__.py keeps its status and now also lists/hashes its contents
    assert by_name["not_a_package"]["status"] == "not_loaded_by_kicad"
    assert by_name["not_a_package"]["importable_namespace_dir"] is True
    assert {f["filename"]: f["sha256"] for f in by_name["not_a_package"]["files"]} == {
        "x.py": _sha(b"x = 1\n")}
    assert "importable_namespace_dir" not in by_name["edge_plating"]


def test_namespace_dir_contents_are_listed_and_hashed_recursively(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    ns = root / "ns_pkg"
    _put(ns / "direct.py", b"direct\n")
    _put(ns / "native.pyd", b"MZ")
    _put(ns / "data.txt", b"data\n")
    _put(ns / "inner" / "mod.py", b"inner\n")
    _put(ns / "inner" / "deeper" / "x.py", b"deeper\n")
    _put(ns / "__pycache__" / "direct.cpython-312.pyc", b"pyc")
    _put(root / "real_pkg" / "__init__.py", b"init\n")
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["ns_pkg"]
    assert entry["type"] == "user_scripting_plugin_package"
    assert entry["status"] == "not_loaded_by_kicad"
    assert entry["importable_namespace_dir"] is True
    assert {f["filename"]: f["sha256"] for f in entry["files"]} == {
        "direct.py": _sha(b"direct\n"), "native.pyd": _sha(b"MZ"), "data.txt": _sha(b"data\n"),
        "inner/mod.py": _sha(b"inner\n"), "inner/deeper/x.py": _sha(b"deeper\n")}
    assert entry["non_py_importables"] == ["native.pyd"]
    assert set(entry["subdirectories"]) == {"inner", "inner/deeper"}
    assert [d["directory"] for d in entry["pycache_dirs"]] == ["__pycache__"]
    assert entry["truncated"] is False
    assert "not imported, parsed, or executed" in entry["note"]
    # a real package gets no namespace marker
    assert "importable_namespace_dir" not in _by_name(
        plugin_catalog.inventory_plugins()["user_scripting_plugins"])["real_pkg"]


def test_namespace_dir_walk_respects_caps_and_links(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    ns = root / "ns_pkg"
    _put(ns / "a.py", b"a\n")
    _put(ns / "b.py", b"b\n")
    _put(env / "secret.py", b"secret\n")
    _symlink(ns / "leak.py", env / "secret.py", directory=False)
    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_FILES", 2)
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["ns_pkg"]
    assert entry["truncated"] is True and len(entry["files"]) == 2
    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_FILES", 2000)
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["ns_pkg"]
    files = {f["filename"]: f for f in entry["files"]}
    assert files["leak.py"] == {"filename": "leak.py", "status": LINK, "reason": "resolves_outside_root"}
    assert files["a.py"]["sha256"] == _sha(b"a\n")


# --- a 'plugins' subdirectory that is a link is never followed outside its scripting root ----------

def _directory_link(link: Path, target: Path, kind: str) -> None:
    if kind == "symlink":
        _symlink(link, target, directory=True)
        return
    if sys.platform != "win32":
        pytest.skip("junctions exist on Windows only")
    import _winapi

    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError as exc:
        pytest.skip(f"junction creation refused by the OS: {exc}")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_plugins_directory_link_escaping_root_is_reported_not_followed(env, monkeypatch, kind):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    base = _docs(env / "docs") / "scripting"
    _put(base / "rootmod.py", b"rootmod\n")
    outside = env / "outside_plugins"
    _put(outside / "evil.py", b"evil\n")
    _put(outside / "evil_pkg" / "__init__.py", b"evil pkg\n")
    _directory_link(base / "plugins", outside, kind)
    result = plugin_catalog.inventory_plugins()

    root_section = result["user_scripting_root_modules"]
    by_name = _by_name(root_section)
    assert by_name["plugins"] == {"type": "user_scripting_root_module_package", "package_dir": "plugins",
                                  "status": LINK, "reason": "resolves_outside_root"}
    assert "plugins_subdirectory" not in root_section  # no scanned_separately note
    assert by_name["rootmod.py"]["sha256"] == _sha(b"rootmod\n")

    plugins_section = result["user_scripting_plugins"]
    assert plugins_section["status"] == LINK
    assert plugins_section["reason"] == "resolves_outside_root"
    assert plugins_section["resolved_from"] == "KICAD_DOCUMENTS_HOME"
    assert Path(plugins_section["path"]) == base / "plugins"
    assert plugins_section["entries"] == []  # nothing outside the root is listed or hashed
    assert "evil.py" not in json.dumps(plugins_section)


def test_settings_plugins_directory_link_escaping_root_is_not_followed(env):
    base = _settings(env) / "scripting"
    outside = env / "outside_plugins"
    _put(outside / "evil.py", b"evil\n")
    _directory_link(base / "plugins", outside, "symlink")
    result = plugin_catalog.inventory_plugins()
    assert "plugins_subdirectory" not in result["settings_scripting_root_modules"]
    assert _by_name(result["settings_scripting_root_modules"])["plugins"]["status"] == LINK
    section = result["settings_scripting_plugins"]
    assert section["status"] == LINK and section["entries"] == []


def test_plugins_directory_link_without_symlink_permission(env, monkeypatch):
    """Same contract with a stand-in classifier, so it runs where links cannot be created."""
    root = _make_user_plugins(env, monkeypatch)
    _put(root / "plug.py", b"plug\n")
    real_classify = plugin_catalog._classify_child

    def fake(parent, name):
        if name == "plugins":
            return None, "resolves_outside_root"
        return real_classify(parent, name)

    monkeypatch.setattr(plugin_catalog, "_classify_child", fake)
    result = plugin_catalog.inventory_plugins()
    assert "plugins_subdirectory" not in result["user_scripting_root_modules"]
    assert _by_name(result["user_scripting_root_modules"])["plugins"]["status"] == LINK
    assert result["user_scripting_plugins"]["status"] == LINK
    assert result["user_scripting_plugins"]["entries"] == []


# --- link entries are reported, never silently dropped --------------------------------------------

def test_user_scripting_symlink_escape_reported_as_link_entry(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    outside = env / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_bytes(b"")
    (env / "secret.py").write_bytes(b"secret")
    _symlink(root / "evil", outside, directory=True)
    _symlink(root / "leak.py", env / "secret.py", directory=False)
    section = plugin_catalog.inventory_plugins()["user_scripting_plugins"]
    assert _by_name(section) == {
        "evil": {"type": "user_scripting_plugin_package", "package_dir": "evil", "status": LINK,
                 "reason": "resolves_outside_root"},
        "leak.py": {"type": "user_scripting_plugin_file", "filename": "leak.py", "status": LINK,
                    "reason": "resolves_outside_root"},
    }


def test_scripting_link_entries_do_not_need_symlink_permission(env, monkeypatch):
    """Same contract with a stand-in classifier, so it runs where symlinks cannot be created."""
    root = _make_user_plugins(env, monkeypatch)
    (root / "ghost.py").write_bytes(b"ghost\n")
    (root / "ghostdir").mkdir()
    (root / "real.py").write_bytes(b"real\n")
    real_classify = plugin_catalog._classify_child

    def fake(parent, name):
        if name in ("ghost.py", "ghostdir"):
            return None, "dangling_or_unresolvable_link: simulated"
        return real_classify(parent, name)

    monkeypatch.setattr(plugin_catalog, "_classify_child", fake)
    by_name = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])
    assert by_name["ghost.py"] == {"type": "user_scripting_plugin_file", "filename": "ghost.py",
                                   "status": LINK, "reason": "dangling_or_unresolvable_link: simulated"}
    assert by_name["ghostdir"]["status"] == LINK and by_name["ghostdir"]["package_dir"] == "ghostdir"
    assert "sha256" not in by_name["ghost.py"] and "sha256" not in by_name["ghostdir"]
    assert by_name["real.py"]["sha256"] == _sha(b"real\n")


def test_pcm_ipc_and_discovery_report_link_entries(env, monkeypatch):
    third = env / "3p"
    third.mkdir()
    outside = env / "outside"
    outside.mkdir()
    _put(outside / "plugin.json", "{}")
    _symlink(third / "evil", outside, directory=True)
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    ipc_root = _docs(env / "docs") / "plugins"
    _symlink(ipc_root / "evilipc", outside, directory=True)

    result = plugin_catalog.inventory_plugins()
    assert result["pcm_third_party_packages"]["entries"] == [
        {"type": "pcm_package", "package_dir": "evil", "status": LINK, "reason": "resolves_outside_root"}]
    assert result["ipc_addon_plugins"]["entries"] == [
        {"type": "ipc_addon", "package_dir": "evilipc", "status": LINK, "reason": "resolves_outside_root"}]
    assert result["pcm_installed_package_code"]["categories"] == [
        {"category": "evil", "status": LINK, "reason": "resolves_outside_root"}]
    # the manifest inside the outside directory is never read
    third_found = result["ipc_plugin_json_discovery"]["third_party"]["entries"]
    assert third_found == [{"type": "ipc_plugin_json", "relative_path": "evil", "status": LINK,
                            "reason": "resolves_outside_tree"}]
    user_found = result["ipc_plugin_json_discovery"]["user_plugins"]["entries"]
    assert user_found == [{"type": "ipc_plugin_json", "relative_path": "evilipc", "status": LINK,
                           "reason": "resolves_outside_tree"}]


def test_package_internal_links_are_reported(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    pkg = root / "pkg"
    _put(pkg / "__init__.py", b"init\n")
    _put(env / "secret.py", b"secret")
    _symlink(pkg / "leak.py", env / "secret.py", directory=False)
    files = {f["filename"]: f for f in _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]["files"]}
    assert files["leak.py"] == {"filename": "leak.py", "status": LINK, "reason": "resolves_outside_root"}
    assert files["__init__.py"]["sha256"] == _sha(b"init\n")


def test_package_internal_links_without_symlink_permission(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    pkg = root / "pkg"
    _put(pkg / "__init__.py", b"init\n")
    _put(pkg / "sub" / "bad.py", b"bad\n")
    real_classify = plugin_catalog._classify_child

    def fake(parent, name):
        if name == "bad.py":
            return None, "resolves_outside_root"
        return real_classify(parent, name)

    monkeypatch.setattr(plugin_catalog, "_classify_child", fake)
    files = {f["filename"]: f for f in _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]["files"]}
    assert files["sub/bad.py"] == {"filename": "sub/bad.py", "status": LINK, "reason": "resolves_outside_root"}


# --- packages: subpackages, non-.py importables, caps --------------------------------------------

def test_package_files_are_recursive_with_relative_names(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    pkg = root / "pkg"
    _put(pkg / "__init__.py", b"init\n")
    _put(pkg / "sub" / "__init__.py", b"sub\n")
    _put(pkg / "sub" / "deep" / "mod.py", b"deep\n")
    _put(pkg / "native.pyd", b"MZ")
    _put(pkg / "__pycache__" / "a.cpython-312.pyc", b"pyc")
    _put(pkg / "sub" / "__pycache__" / "b.cpython-312.pyc", b"pyc2")
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]
    files = {f["filename"]: f["sha256"] for f in entry["files"]}
    assert files == {"__init__.py": _sha(b"init\n"), "native.pyd": _sha(b"MZ"),
                     "sub/__init__.py": _sha(b"sub\n"), "sub/deep/mod.py": _sha(b"deep\n")}
    assert entry["non_py_importables"] == ["native.pyd"]
    assert set(entry["subdirectories"]) == {"sub", "sub/deep"}
    assert {d["directory"] for d in entry["pycache_dirs"]} == {"__pycache__", "sub/__pycache__"}
    assert entry["subdirectories_not_descended"] == [] and entry["truncated"] is False


def test_package_walk_depth_file_and_byte_caps(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    pkg = root / "pkg"
    _put(pkg / "__init__.py", b"x=1\n")
    _put(pkg / "a.py", b"x=1\n")
    _put(pkg / "sub" / "deep" / "mod.py", b"deep\n")

    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_DEPTH", 1)
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]
    assert entry["subdirectories_not_descended"] == ["sub/deep"]
    assert "sub/deep/mod.py" not in {f["filename"] for f in entry["files"]}
    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_DEPTH", 8)

    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_FILES", 2)
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]
    assert entry["truncated"] is True and len(entry["files"]) == 2
    monkeypatch.setattr(plugin_catalog, "MAX_PACKAGE_FILES", 2000)

    monkeypatch.setattr(plugin_catalog, "MAX_TREE_BYTES", 5)
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]
    by_file = {f["filename"]: f for f in entry["files"]}
    assert by_file["__init__.py"]["sha256"] == _sha(b"x=1\n")
    assert by_file["a.py"] == {"filename": "a.py", "sha256": None, "size": 4, "status": "size_budget_exceeded"}


def test_top_level_non_py_importables_and_pycache_are_reported(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    _put(root / "top.py", b"top\n")
    _put(root / "__init__.py", b"skipped\n")
    _put(root / "a.pyc", b"A")
    _put(root / "b.pyd", b"B")
    _put(root / "c.cp312-win_amd64.pyd", b"C")
    _put(root / "d.so", b"D")
    _put(root / "readme.txt", b"ignored")
    _put(root / "__pycache__" / "top.cpython-312.pyc", b"PYC")
    by_name = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])
    assert set(by_name) == {"top.py", "a.pyc", "b.pyd", "c.cp312-win_amd64.pyd", "d.so", "__pycache__"}
    assert "status" not in by_name["top.py"]
    for name, data in (("a.pyc", b"A"), ("b.pyd", b"B"), ("c.cp312-win_amd64.pyd", b"C"), ("d.so", b"D")):
        assert by_name[name] == {"type": "user_scripting_plugin_file", "filename": name,
                                 "status": "non_py_importable", "sha256": _sha(data),
                                 "note": plugin_catalog._NOT_IMPORTED_NOTE}
    pycache = by_name["__pycache__"]
    assert pycache["status"] == "pycache_present"
    assert pycache["files"] == [{"filename": "top.cpython-312.pyc", "sha256": _sha(b"PYC")}]


# --- '.pyw' is Python source: hashed like '.py', never 'non_py_importable' --------------------------

def test_pyw_user_scripting_file_is_source(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    _put(root / "gui.pyw", b"gui\n")
    _put(root / "UPPER.PYW", b"upper\n")
    by_name = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])
    # case-sensitive like '.py': an upper-case suffix is not a source module
    assert set(by_name) == {"gui.pyw"}
    assert by_name["gui.pyw"] == {"type": "user_scripting_plugin_file", "filename": "gui.pyw",
                                  "sha256": _sha(b"gui\n"), "note": plugin_catalog._NOT_IMPORTED_NOTE}


def test_pyw_inside_package_is_hashed_without_non_py_flag(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    _put(root / "pkg" / "__init__.py", b"init\n")
    _put(root / "pkg" / "sub" / "tool.pyw", b"tool\n")
    entry = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])["pkg"]
    assert {f["filename"]: f["sha256"] for f in entry["files"]}["sub/tool.pyw"] == _sha(b"tool\n")
    assert entry["non_py_importables"] == []


def test_pyw_pcm_loose_file_and_link_entry(env, monkeypatch):
    third = env / "3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    _put(third / "plugins" / "loose.pyw", b"loose\n")
    _put(third / "plugins" / "ext.so", b"ELF")
    section = plugin_catalog.inventory_plugins()["pcm_installed_package_code"]
    loose = {f["filename"]: f for f in section["categories"][0]["loose_files"]}
    assert loose["loose.pyw"] == {"filename": "loose.pyw", "sha256": _sha(b"loose\n")}
    assert loose["ext.so"]["non_py_importable"] is True
    assert plugin_catalog._link_entry("x.pyw", "r", "file_t", "pkg_t") == {
        "type": "file_t", "filename": "x.pyw", "status": LINK, "reason": "r"}


def test_pyw_sentinel_writes_no_marker(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    marker = env / "IMPORTED_pyw.marker"
    body = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    (root / "sentinel_pyw_plug.pyw").write_text(body, encoding="utf-8")
    result = plugin_catalog.inventory_plugins()
    assert [e["filename"] for e in result["user_scripting_plugins"]["entries"]] == ["sentinel_pyw_plug.pyw"]
    assert not marker.exists()
    assert not [name for name in sys.modules if name.startswith("sentinel_pyw")]


# --- an unreadable PCM/IPC root is a structured error, not an exception ------------------------------

@pytest.mark.parametrize("which", ["third_party", "ipc"])
def test_unreadable_addon_root_returns_structured_error(env, monkeypatch, which):
    third = env / "3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    ipc_root = _docs(env / "docs") / "plugins"
    _put(third / "pkg" / "metadata.json", "{}")
    _put(ipc_root / "x" / "plugin.json", "{}")
    target = third if which == "third_party" else ipc_root
    real_listdir = plugin_catalog.os.listdir

    def guarded(path="."):
        if Path(path) == target:
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(plugin_catalog.os, "listdir", guarded)
    result = plugin_catalog.inventory_plugins()  # must not raise
    section = result["pcm_third_party_packages" if which == "third_party" else "ipc_addon_plugins"]
    assert section["status"] == "present" and Path(section["path"]) == target
    assert section["entries"] == [] and section["error"].startswith("PermissionError")
    other = result["ipc_addon_plugins" if which == "third_party" else "pcm_third_party_packages"]
    assert "error" not in other and other["entries"]


# --- hashing: streamed and capped -----------------------------------------------------------------

def test_hash_file_streams_in_blocks_and_keeps_digest_shape(env, monkeypatch):
    data = b"0123456789"
    path = _put(env / "f.bin", data)
    monkeypatch.setattr(plugin_catalog, "HASH_BLOCK_BYTES", 3)
    assert plugin_catalog._hash_file(path) == _sha(data)
    assert plugin_catalog._hash_record(path) == {"sha256": _sha(data)}
    assert plugin_catalog._hash_file(env / "missing.bin") is None


def test_oversize_file_is_marked_and_not_opened(env, monkeypatch):
    big = _put(env / "big.bin", b"x" * 20)
    monkeypatch.setattr(plugin_catalog, "MAX_HASH_BYTES", 8)
    with monkeypatch.context() as patch:
        def forbidden(self, *args, **kwargs):
            raise AssertionError("an oversize file must not be opened")

        patch.setattr(plugin_catalog.Path, "open", forbidden)
        assert plugin_catalog._hash_record(big) == {"sha256": None, "size": 20, "status": "oversize"}
        assert plugin_catalog._hash_file(big) is None


def test_oversize_entries_in_scans(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    _put(root / "single.py", b"x" * 20)
    _put(root / "small.py", b"abc")
    _put(root / "native.pyd", b"y" * 20)
    pkg = root / "pkg"
    _put(pkg / "__init__.py", b"abc")
    _put(pkg / "big.bin", b"z" * 20)
    monkeypatch.setattr(plugin_catalog, "MAX_HASH_BYTES", 8)
    by_name = _by_name(plugin_catalog.inventory_plugins()["user_scripting_plugins"])
    assert by_name["single.py"]["sha256"] is None
    assert by_name["single.py"]["size"] == 20 and by_name["single.py"]["status"] == "oversize"
    assert by_name["small.py"]["sha256"] == _sha(b"abc")
    # a non-.py importable keeps its own status and records the hash status separately
    assert by_name["native.pyd"]["status"] == "non_py_importable"
    assert by_name["native.pyd"]["hash_status"] == "oversize" and by_name["native.pyd"]["sha256"] is None
    files = {f["filename"]: f for f in by_name["pkg"]["files"]}
    assert files["big.bin"] == {"filename": "big.bin", "sha256": None, "size": 20, "status": "oversize"}
    assert files["__init__.py"]["sha256"] == _sha(b"abc")


# --- scripting roots that LoadPlugins also imports from ---------------------------------------------

def test_user_scripting_root_modules_scanned_and_plugins_noted(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    base = _docs(env / "docs") / "scripting"
    _put(base / "rootmod.py", b"rootmod\n")
    _put(base / "rootpkg" / "__init__.py", b"rootpkg\n")
    _put(base / "plugins" / "plug.py", b"plug\n")
    section = plugin_catalog.inventory_plugins()["user_scripting_root_modules"]
    assert Path(section["path"]) == base and section["resolved_from"] == "KICAD_DOCUMENTS_HOME"
    by_name = _by_name(section)
    assert set(by_name) == {"rootmod.py", "rootpkg"}  # 'plugins' is not listed as a package
    assert by_name["rootmod.py"]["type"] == "user_scripting_root_module_file"
    assert by_name["rootmod.py"]["sha256"] == _sha(b"rootmod\n")
    assert by_name["rootpkg"]["type"] == "user_scripting_root_module_package"
    assert section["plugins_subdirectory"] == {"status": "scanned_separately",
                                              "inventory_key": "user_scripting_plugins"}
    plugins = plugin_catalog.inventory_plugins()["user_scripting_plugins"]
    assert set(_by_name(plugins)) == {"plug.py"}


def test_settings_scripting_roots_scanned(env):
    base = _settings(env) / "scripting"
    _put(base / "settingsmod.py", b"settingsmod\n")
    _put(base / "plugins" / "settingsplug.py", b"settingsplug\n")
    _put(base / "plugins" / "settingspkg" / "__init__.py", b"pkg\n")
    result = plugin_catalog.inventory_plugins()
    root_section = result["settings_scripting_root_modules"]
    assert Path(root_section["path"]) == base and root_section["resolved_from"] == "APPDATA"
    assert set(_by_name(root_section)) == {"settingsmod.py"}
    assert root_section["plugins_subdirectory"]["inventory_key"] == "settings_scripting_plugins"
    plugins = result["settings_scripting_plugins"]
    assert Path(plugins["path"]) == base / "plugins"
    by_name = _by_name(plugins)
    assert set(by_name) == {"settingsplug.py", "settingspkg"}
    assert by_name["settingsplug.py"]["type"] == "settings_scripting_plugin_file"
    assert by_name["settingspkg"]["type"] == "settings_scripting_plugin_package"


def test_legacy_scripting_root_modules_scanned(env):
    base = _app_scripting(env)
    _put(base / "kicad_mod.py", b"mod\n")
    _put(base / "kicad_pkg" / "__init__.py", b"pkg\n")
    _put(base / "plugins" / "p.py", b"p\n")
    section = plugin_catalog.inventory_plugins()["legacy_scripting_root_modules"]
    assert section["resolved_from"] == "KICAD_APP_PATH"
    by_name = _by_name(section)
    assert set(by_name) == {"kicad_mod.py", "kicad_pkg"}
    assert by_name["kicad_mod.py"]["type"] == "legacy_scripting_root_module_file"
    assert section["plugins_subdirectory"]["inventory_key"] == "legacy_scripting_plugins"


# --- settings root honours KICAD_CONFIG_HOME ---------------------------------------------------------

def test_settings_root_prefers_kicad_config_home(env, monkeypatch):
    cfg = env / "cfg"
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))
    _put(cfg / "10.0" / "scripting" / "plugins" / "cfgplug.py", b"cfg\n")
    result = plugin_catalog.inventory_plugins()
    settings = result["settings_root"]
    assert Path(settings["path"]) == cfg / "10.0"
    assert settings["resolved_from"] == "KICAD_CONFIG_HOME" and settings["status"] == "present"
    plugins = result["settings_scripting_plugins"]
    assert plugins["resolved_from"] == "KICAD_CONFIG_HOME"
    assert set(_by_name(plugins)) == {"cfgplug.py"}


def test_settings_root_empty_config_home_falls_back_to_appdata_then_known_folder(env, monkeypatch):
    monkeypatch.setenv("KICAD_CONFIG_HOME", "")
    settings = plugin_catalog.inventory_plugins()["settings_root"]
    assert settings["resolved_from"] == "APPDATA" and Path(settings["path"]) == _settings(env)
    monkeypatch.delenv("APPDATA")
    monkeypatch.setattr(plugin_catalog, "_known_roaming_folder", lambda: env / "roaming-known")
    settings = plugin_catalog.inventory_plugins()["settings_root"]
    assert settings["resolved_from"] == "known_folder_RoamingAppData"
    assert Path(settings["path"]) == env / "roaming-known" / "kicad" / "10.0"
    monkeypatch.setattr(plugin_catalog, "_known_roaming_folder", lambda: None)
    settings = plugin_catalog.inventory_plugins()["settings_root"]
    assert settings["resolved_from"] == "unresolved" and settings["status"] == "absent"  # never a guessed folder


# --- installed_packages.json ---------------------------------------------------------------------

def test_installed_packages_index_absent(env):
    index = plugin_catalog.inventory_plugins()["installed_packages_index"]
    assert index["status"] == "absent"
    assert Path(index["path"]) == _settings(env) / "installed_packages.json"
    assert index["resolved_from"] == "APPDATA"


def test_installed_packages_index_ok_is_parsed_data(env):
    payload = {"packages": {"com.example.plug": {"version": "1.0.0"}}}
    raw = json.dumps(payload).encode("utf-8")
    _put(_settings(env) / "installed_packages.json", raw)
    index = plugin_catalog.inventory_plugins()["installed_packages_index"]
    assert index["status"] == "ok" and index["sha256"] == _sha(raw)
    assert index["data"] == payload and index["size"] == len(raw)


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe"])
def test_installed_packages_index_invalid(env, raw):
    _put(_settings(env) / "installed_packages.json", raw)
    index = plugin_catalog.inventory_plugins()["installed_packages_index"]
    assert index["status"] == "invalid" and index["sha256"] == _sha(raw)
    assert index["error"] and "data" not in index


def test_installed_packages_index_size_capped(env, monkeypatch):
    _put(_settings(env) / "installed_packages.json", b'{"a": "' + b"x" * 50 + b'"}')
    monkeypatch.setattr(plugin_catalog, "MAX_INDEX_BYTES", 10)
    index = plugin_catalog.inventory_plugins()["installed_packages_index"]
    assert index["status"] == "invalid" and index["sha256"] is None and "data" not in index
    assert "not read" in index["error"]


def test_installed_packages_index_link_outside_is_not_followed(env):
    _put(env / "elsewhere.json", b"{}")
    _symlink(_settings(env) / "installed_packages.json", env / "elsewhere.json", directory=False)
    index = plugin_catalog.inventory_plugins()["installed_packages_index"]
    assert index["status"] == LINK and index["reason"] == "resolves_outside_root"


# --- installed PCM package code under <3rdparty>/<category>/<pkgid> ------------------------------------

def test_pcm_installed_package_code_enumerated_per_category(env, monkeypatch):
    third = env / "3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    _put(third / "plugins" / "pkg_a" / "__init__.py", b"a\n")
    _put(third / "plugins" / "pkg_a" / "sub" / "native.pyd", b"MZ")
    _put(third / "plugins" / "pkg_a" / "__pycache__" / "x.cpython-312.pyc", b"pyc")
    _put(third / "plugins" / "loose.py", b"loose\n")
    _put(third / "plugins" / "ext.so", b"ELF")
    _put(third / "plugins" / "notes.txt", b"ignored")
    _put(third / "footprints" / "pkg_b" / "lib.pretty" / "a.kicad_mod", b"(footprint)")
    _put(third / "stray.txt", b"root-level file is not a category")
    result = plugin_catalog.inventory_plugins()
    section = result["pcm_installed_package_code"]
    assert section["status"] == "present" and section["resolved_from"] == "env"
    categories = {c["category"]: c for c in section["categories"]}
    assert set(categories) == {"plugins", "footprints"}

    packages = {p["package_id"]: p for p in categories["plugins"]["packages"]}
    pkg_a = packages["pkg_a"]
    assert {f["filename"]: f["sha256"] for f in pkg_a["files"]} == {
        "__init__.py": _sha(b"a\n"), "sub/native.pyd": _sha(b"MZ")}
    assert pkg_a["non_py_importables"] == ["sub/native.pyd"]
    assert [d["directory"] for d in pkg_a["pycache_dirs"]] == ["__pycache__"]
    loose = {f["filename"]: f for f in categories["plugins"]["loose_files"]}
    assert set(loose) == {"loose.py", "ext.so"}
    assert loose["loose.py"]["sha256"] == _sha(b"loose\n") and "non_py_importable" not in loose["loose.py"]
    assert loose["ext.so"]["non_py_importable"] is True

    footprint_files = {f["filename"] for f in
                       {p["package_id"]: p for p in categories["footprints"]["packages"]}["pkg_b"]["files"]}
    assert footprint_files == {"lib.pretty/a.kicad_mod"}

    # the immediate-children PCM scan is unchanged: category directories carry no manifest
    pcm = result["pcm_third_party_packages"]
    assert {e["package_dir"]: e["status"] for e in pcm["entries"]} == {
        "plugins": "no_manifest_found", "footprints": "no_manifest_found"}


def test_pcm_installed_package_code_absent_root(env):
    section = plugin_catalog.inventory_plugins()["pcm_installed_package_code"]
    assert section["status"] == "absent" and section["path"] and section["resolved_from"]


# --- recursive IPC plugin.json discovery in the three trees --------------------------------------------

def _ipc_fixture(env, monkeypatch) -> Path:
    third = env / "3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    stock = env / "app" / "share" / "kicad" / "plugins"
    manifest = json.dumps({"name": "Stock", "versions": [{"kicad_version": "10.0"}]})
    _put(stock / "a" / "b" / "plugin.json", manifest)
    _put(third / "plugins" / "p" / "plugin.json", "{bad")
    docs_plugins = _docs(env / "docs") / "plugins"
    _put(docs_plugins / "x" / "plugin.json", json.dumps({"name": "UserX"}))
    _put(docs_plugins / "x" / "y" / "z" / "plugin.json", json.dumps({"name": "UserZ"}))
    _put(docs_plugins / "x" / "other.json", "{}")
    return docs_plugins


def test_ipc_plugin_json_discovered_recursively_in_all_three_trees(env, monkeypatch):
    _ipc_fixture(env, monkeypatch)
    result = plugin_catalog.inventory_plugins()
    discovery = result["ipc_plugin_json_discovery"]
    assert set(discovery) == {"stock_plugins", "third_party", "user_plugins"}
    for tree in discovery.values():
        assert tree["status"] == "present" and tree["path"] and tree["resolved_from"]
        assert tree["truncated"] is False

    stock = discovery["stock_plugins"]["entries"]
    assert [e["relative_path"] for e in stock] == ["a/b/plugin.json"]
    assert stock[0]["status"] == "ok" and stock[0]["metadata"]["name"] == "Stock"
    assert stock[0]["compatibility"]["compatible_with_pinned"] is True
    assert stock[0]["manifest_sha256"] == _sha(json.dumps(
        {"name": "Stock", "versions": [{"kicad_version": "10.0"}]}).encode("utf-8"))

    third = discovery["third_party"]["entries"]
    assert [e["relative_path"] for e in third] == ["plugins/p/plugin.json"]
    assert third[0]["status"] == "invalid_metadata" and third[0]["error"] and "metadata" not in third[0]

    user = discovery["user_plugins"]["entries"]
    assert [e["relative_path"] for e in user] == ["x/plugin.json", "x/y/z/plugin.json"]
    assert all(e["status"] == "ok" and e["compatibility"]["checked"] is False for e in user)

    # the existing one-level scan is unchanged: only the immediate child 'x' with its own manifest
    ipc = result["ipc_addon_plugins"]
    assert [(e["package_dir"], e["status"]) for e in ipc["entries"]] == [("x", "ok")]


def test_ipc_discovery_absent_tree_and_caps(env, monkeypatch):
    discovery = plugin_catalog.inventory_plugins()["ipc_plugin_json_discovery"]
    for tree in discovery.values():
        assert tree["status"] == "absent" and tree["entries"] == [] and tree["path"]
    _ipc_fixture(env, monkeypatch)

    monkeypatch.setattr(plugin_catalog, "MAX_DISCOVERY_DEPTH", 1)
    user = plugin_catalog.inventory_plugins()["ipc_plugin_json_discovery"]["user_plugins"]
    assert [e["relative_path"] for e in user["entries"]] == ["x/plugin.json"]
    assert user["directories_not_descended"] == ["x/y"]
    monkeypatch.setattr(plugin_catalog, "MAX_DISCOVERY_DEPTH", 8)

    monkeypatch.setattr(plugin_catalog, "MAX_DISCOVERY_ENTRIES", 1)
    user = plugin_catalog.inventory_plugins()["ipc_plugin_json_discovery"]["user_plugins"]
    assert user["truncated"] is True


def test_ipc_discovery_reports_directory_loop(env, monkeypatch):
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    docs_plugins = _docs(env / "docs") / "plugins"
    _put(docs_plugins / "x" / "plugin.json", "{}")
    _symlink(docs_plugins / "x" / "loop", docs_plugins, directory=True)
    user = plugin_catalog.inventory_plugins()["ipc_plugin_json_discovery"]["user_plugins"]
    statuses = {e["relative_path"]: e["status"] for e in user["entries"]}
    assert statuses == {"x/loop": LINK, "x/plugin.json": "ok"}


# --- every scan root reports path/status/resolved_from ----------------------------------------------

def test_every_root_has_path_status_resolved_from(env):
    result = plugin_catalog.inventory_plugins()
    for key in ROOT_KEYS + NEW_ROOT_KEYS:
        section = result[key]
        assert section["path"]
        assert section["status"] in ("present", "absent")
        assert section["resolved_from"]
    # all absent here: nothing exists under tmp_path
    assert all(result[key]["status"] == "absent" for key in ROOT_KEYS + NEW_ROOT_KEYS)


def test_bundled_legacy_scan_reports_packages(env):
    legacy = env / "app" / "share" / "kicad" / "scripting" / "plugins"
    legacy.mkdir(parents=True)
    (legacy / "wizard.py").write_bytes(b"w = 1\n")
    (legacy / "__init__.py").write_bytes(b"")
    pkg = legacy / "bundled_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_bytes(b"init\n")
    section = plugin_catalog.inventory_plugins()["legacy_scripting_plugins"]
    assert section["resolved_from"] == "KICAD_APP_PATH"
    by_name = _by_name(section)
    assert set(by_name) == {"wizard.py", "bundled_pkg"}
    assert by_name["wizard.py"]["type"] == "legacy_scripting_plugin_file"
    assert by_name["bundled_pkg"]["files"] == [{"filename": "__init__.py", "sha256": _sha(b"init\n")}]


# --- nothing is ever imported ----------------------------------------------------------------------------

def test_inventory_never_imports_sentinel_plugin(env, monkeypatch):
    root = _make_user_plugins(env, monkeypatch)
    marker = env / "IMPORTED.marker"
    body = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    (root / "sentinel.py").write_text(body, encoding="utf-8")
    pkg = root / "sentinel_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    result = plugin_catalog.inventory_plugins()
    assert len(result["user_scripting_plugins"]["entries"]) == 2
    assert not marker.exists()


def test_inventory_never_imports_sentinels_in_any_scanned_location(env, monkeypatch):
    third = env / "3p"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(env / "docs"))
    docs = _docs(env / "docs")
    names: list[str] = []

    def sentinel(path: Path) -> None:
        stem = path.parent.name if path.name == "__init__.py" else path.stem
        marker = env / f"IMPORTED_{len(names)}_{stem}.marker"
        body = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        _put(path, body)
        names.append(stem)

    locations = [
        docs / "scripting" / "plugins" / "sentinel_user_plug.py",
        docs / "scripting" / "plugins" / "sentinel_user_plug_pkg" / "__init__.py",
        docs / "scripting" / "sentinel_user_root.py",
        docs / "scripting" / "sentinel_user_root_pkg" / "__init__.py",
        _settings(env) / "scripting" / "sentinel_settings_root.py",
        _settings(env) / "scripting" / "sentinel_settings_root_pkg" / "__init__.py",
        _settings(env) / "scripting" / "plugins" / "sentinel_settings_plug.py",
        _settings(env) / "scripting" / "plugins" / "sentinel_settings_plug_pkg" / "__init__.py",
        _app_scripting(env) / "sentinel_legacy_root.py",
        _app_scripting(env) / "sentinel_legacy_root_pkg" / "__init__.py",
        _app_scripting(env) / "plugins" / "sentinel_legacy_plug.py",
        third / "plugins" / "sentinel_pcm_loose.py",
        third / "plugins" / "sentinel_pcm_pkg" / "__init__.py",
        third / "plugins" / "sentinel_pcm_pkg" / "sub" / "sentinel_pcm_sub.py",
        third / "plugins" / "sentinel_pcm_pkg" / "setup.py",
        third / "plugins" / "sentinel_pcm_ipc" / "main.py",
        _app_scripting(env).parent / "plugins" / "sentinel_ipc_stock" / "main.py",
        docs / "plugins" / "sentinel_ipc_user" / "__init__.py",
        docs / "plugins" / "sentinel_ipc_user" / "sitecustomize.py",
    ]
    for location in locations:
        sentinel(location)
    # manifests, a .pth hook and a sourceless module next to the sentinels
    _put(third / "plugins" / "sentinel_pcm_ipc" / "plugin.json", json.dumps({"name": "pcm-ipc"}))
    _put(_app_scripting(env).parent / "plugins" / "sentinel_ipc_stock" / "plugin.json", "{}")
    _put(docs / "plugins" / "sentinel_ipc_user" / "plugin.json", "{}")
    pth_marker = env / "IMPORTED_pth.marker"
    _put(third / "plugins" / "sentinel_pcm_pkg" / "hook.pth",
         f"import pathlib; pathlib.Path({str(pth_marker)!r}).write_text('x')\n")
    _put(docs / "scripting" / "sentinel_user_root_bin.pyc", b"\x00\x01")
    _put(_settings(env) / "installed_packages.json", json.dumps({"packages": {}}))

    result = plugin_catalog.inventory_plugins()

    # every new scan actually saw its sentinels (so the markers are absent because nothing ran)
    assert len(result["user_scripting_plugins"]["entries"]) == 2
    assert len(result["user_scripting_root_modules"]["entries"]) == 3  # .py, package, .pyc
    assert len(result["settings_scripting_root_modules"]["entries"]) == 2
    assert len(result["settings_scripting_plugins"]["entries"]) == 2
    assert len(result["legacy_scripting_root_modules"]["entries"]) == 2
    assert len(result["legacy_scripting_plugins"]["entries"]) == 1
    plugin_dirs = {p["package_id"]: p for p in
                   {c["category"]: c for c in result["pcm_installed_package_code"]["categories"]}
                   ["plugins"]["packages"]}
    assert {"sentinel_pcm_pkg", "sentinel_pcm_ipc"} <= set(plugin_dirs)
    assert "sub/sentinel_pcm_sub.py" in {f["filename"] for f in plugin_dirs["sentinel_pcm_pkg"]["files"]}
    discovery = result["ipc_plugin_json_discovery"]
    assert len(discovery["stock_plugins"]["entries"]) == 1
    assert len(discovery["third_party"]["entries"]) == 1
    assert len(discovery["user_plugins"]["entries"]) == 1
    assert result["installed_packages_index"]["status"] == "ok"

    assert not list(env.glob("IMPORTED*.marker"))
    assert not pth_marker.exists()
    assert not [name for name in sys.modules if name.startswith("sentinel_")]
