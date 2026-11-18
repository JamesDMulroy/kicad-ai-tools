"""Regression coverage for tool-call scratch, failed owners, and conservative retention."""
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import pytest

from kicad_mcp.utils import temp_cleanup as cleanup


def test_call_and_exception_remove_only_their_own_scratch(tmp_path):
    other = tmp_path / "other-session"
    other.mkdir()
    allocated = []

    @cleanup.cleanup_after_call
    def tool(fail=False):
        folder = Path(cleanup.scoped_mkdtemp(prefix="kicad-ai-tools-test-", dir=tmp_path))
        assert list(folder.iterdir()) == []
        if os.name == "nt":
            assert cleanup.directory_in_use(folder)
        allocated.append(folder)
        (folder / "data").write_bytes(b"complete")
        if fail:
            raise ValueError("tool failed")
        return (folder / "data").read_bytes()

    assert tool() == b"complete"
    with pytest.raises(ValueError, match="tool failed"):
        tool(True)
    assert all(not p.exists() for p in allocated)
    assert other.is_dir()


def test_owner_keeps_scratch_until_close_and_cleans_failed_init(tmp_path):
    allocated = []

    @cleanup.temporary_owner()
    class Owner:
        def __init__(self, fail=False):
            self.folder = Path(cleanup.scoped_mkdtemp(dir=tmp_path))
            allocated.append(self.folder)
            if fail:
                raise ValueError("start failed")

        def close(self):
            assert self.folder.is_dir()

    owner = Owner()
    assert owner.folder.exists()
    owner.close()
    assert not owner.folder.exists()
    with pytest.raises(ValueError):
        Owner(True)
    assert not allocated[-1].exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_cli_tool_call_leaves_no_new_temp_dirs(tmp_path, monkeypatch, returncode):
    from kicad_mcp.tools import cli_tools
    monkeypatch.setenv("KICAD_AI_TOOLS_DATA_DIR", str(tmp_path / "app-data"))
    import tempfile
    before = set(Path(tempfile.gettempdir()).glob("kicad_cli_run_*"))

    def native(argv, **kwargs):
        evidence = Path(kwargs["evidence_dir"])
        evidence.mkdir()
        (evidence / "stdout.bin").write_bytes(b"10.0.5\n")
        (evidence / "stderr.bin").write_bytes(b"diagnostic" if returncode else b"")
        return subprocess.CompletedProcess(argv, returncode)

    with patch.object(cli_tools, "get_kicad_cli_path", return_value=Path("kicad-cli.exe")), \
            patch.object(cli_tools.secure_subprocess, "run_contained", side_effect=native):
        result = cli_tools.run_cli("version")
    assert result["ok"] is (returncode == 0)
    assert not set(Path(tempfile.gettempdir()).glob("kicad_cli_run_*")) - before
    if returncode:
        evidence = Path(result["evidence_dir"])
        assert evidence.is_relative_to(tmp_path / "app-data")
        assert (evidence / "stderr.bin").read_bytes() == b"diagnostic"
    else:
        assert not list((tmp_path / "app-data/cli-evidence").iterdir())


def age(folder, days):
    stamp = time.time() - days * 24 * 60 * 60
    for p in [*folder.rglob("*"), folder]:
        os.utime(p, (stamp, stamp))


def test_sweep_preserves_live_recent_and_unrelated_dirs(tmp_path):
    old = tmp_path / "kicad-ai-tools-old"
    held = tmp_path / "kicad_cli_run_held"
    recent = tmp_path / "kicad-ai-tools-recent"
    unrelated = tmp_path / "other-application"
    for p in [old, held, recent, unrelated]:
        p.mkdir()
        (p / "data").write_text("evidence")
    for p in [old, held, unrelated]:
        age(p, 2)
    with patch.object(cleanup, "directory_in_use", side_effect=lambda p: p == held):
        receipt = cleanup.sweep_stale(temp_root=tmp_path, jobs_root=tmp_path / "jobs")
    assert receipt["removed"] == [str(old)]
    assert all(p.exists() for p in [held, recent, unrelated])


def test_workflow_pruning_requires_finished_and_dead_owner(tmp_path):
    import json
    from kicad_mcp.utils import winproc
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    done = jobs / "done"
    running = jobs / "running"
    live = jobs / "live"
    for folder, state in [(done, {"status": "completed"}),
                           (running, {"status": "running"}),
                           (live, {"status": "completed", "owner": {"pid": 55, "created": 1}})]:
        folder.mkdir()
        (folder / "state.json").write_text(json.dumps(state))
        age(folder, 8)
    with patch.object(cleanup, "directory_in_use", return_value=False), \
            patch.object(winproc, "identity", return_value={"pid": 55, "created": 1}):
        receipt = cleanup.sweep_stale(temp_root=tmp_path / "empty", jobs_root=jobs)
    assert receipt["removed"] == [str(done)]
    assert live.exists() and running.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership")
def test_windows_locked_file_is_preserved(tmp_path):
    import ctypes
    from ctypes import wintypes as w
    folder = tmp_path / "kicad-ai-tools-held"
    folder.mkdir()
    file = folder / "open.bin"
    file.write_bytes(b"held")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.CreateFileW(str(file), 0x80000000, 0, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert cleanup.directory_in_use(folder)
    finally:
        k.CloseHandle(handle)
    assert not cleanup.directory_in_use(folder)
