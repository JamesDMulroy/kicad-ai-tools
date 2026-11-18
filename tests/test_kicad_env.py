"""kicad_env: explicit settings are honoured or refused loudly (never silently replaced), the per-machine
config is typed, and setup's client entry carries relocations the server must also see."""
from __future__ import annotations

import os

import pytest

from kicad_mcp.adaptive_editor import kicad_env


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(kicad_env.CONFIG_ENV, str(tmp_path / "config.json"))
    kicad_env.kicad_app_root.cache_clear()
    kicad_env.kicad_version.cache_clear()
    yield
    kicad_env.kicad_app_root.cache_clear()
    kicad_env.kicad_version.cache_clear()


def test_a_wrong_explicit_root_is_refused_not_replaced(monkeypatch, tmp_path):
    monkeypatch.setenv(kicad_env.KICAD_ROOT_ENV, str(tmp_path / "not-kicad"))
    with pytest.raises(kicad_env.ConfiguredPathError) as info:
        kicad_env.kicad_app_root()
    assert "configured KiCad root" in str(info.value)


def test_a_wrong_explicit_cli_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CLI_PATH", str(tmp_path / "kicad-cli.exe"))
    with pytest.raises(kicad_env.ConfiguredPathError):
        kicad_env.kicad_cli()


def test_config_values_apply_and_env_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", raising=False)
    (tmp_path / "config.json").write_text('{"server_idle_seconds": 42}', encoding="utf-8")
    assert kicad_env.setting("server_idle_seconds", "KICAD_AI_TOOLS_SERVER_IDLE_SECONDS") == 42
    monkeypatch.setenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", "7")
    assert kicad_env.setting("server_idle_seconds", "KICAD_AI_TOOLS_SERVER_IDLE_SECONDS") == "7"


def test_a_malformed_config_is_a_typed_actionable_error(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(kicad_env.ConfigError) as info:
        kicad_env.load_config()
    assert "kicad_mcp.setup" in str(info.value)


def test_describe_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(kicad_env.KICAD_ROOT_ENV, str(tmp_path / "not-kicad"))
    out = kicad_env.describe()
    assert "error" in out and "configured_path_invalid" in out["error"]


def test_setup_entry_carries_relocations(monkeypatch, tmp_path):
    from kicad_mcp import setup
    monkeypatch.setenv(kicad_env.DATA_DIR_ENV, str(tmp_path / "data"))
    entry = setup.client_entry()
    assert entry["env"][kicad_env.DATA_DIR_ENV] == str(tmp_path / "data")
    assert entry["env"][kicad_env.CONFIG_ENV] == str(tmp_path / "config.json")


@pytest.mark.skipif(os.name != "nt", reason="Windows known-folder fallback")
def test_windows_user_folders_are_never_guessed(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv(kicad_env.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(kicad_env, "windows_known_folder", lambda _folder: None)
    monkeypatch.setattr(kicad_env, "load_config", lambda: {})
    with pytest.raises(kicad_env.ConfigError, match=kicad_env.CONFIG_ENV):
        kicad_env.user_config_dir()
    with pytest.raises(kicad_env.ConfigError, match=kicad_env.DATA_DIR_ENV):
        kicad_env.user_data_dir()


def test_an_absurdly_nested_config_is_a_config_error(tmp_path):
    kicad_env.config_path().write_text("[" * 200000, encoding="utf-8")
    with pytest.raises(kicad_env.ConfigError) as caught:
        kicad_env.load_config()
    assert isinstance(caught.value.__cause__, RecursionError)  # the nesting path, not any other parse error


def test_a_found_kicad_cli_that_fails_is_not_reported_as_missing(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(kicad_env, "kicad_cli", lambda: tmp_path / "kicad-cli.exe")
    monkeypatch.setattr(kicad_env.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="missing DLL"))
    with pytest.raises(kicad_env.KiCadCliFailed) as caught:
        kicad_env.kicad_version()
    assert caught.value.code == "kicad_cli_failed" and "was found" in str(caught.value)
