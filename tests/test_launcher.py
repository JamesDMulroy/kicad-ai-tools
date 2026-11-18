"""On-demand launcher, end to end through the real pythonw entry point: the handshake and the tool list
are served without a server process; the first tool call starts one; it stops after the idle period."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kicad_mcp" / "adaptive_editor" / "live"))

pytestmark = pytest.mark.skipif(os.name != "nt", reason="pythonw entry point is Windows-only")


def _pythonw() -> Path:
    exe = Path(sys.executable)
    return exe.with_name("pythonw.exe")


def _children(pid: int) -> set[int]:
    from hidden_kicad import tree_pids
    return tree_pids([pid]) - {pid}


class Session:
    def __init__(self, env, log: Path):
        self.log = open(log, "ab")  # diagnostics are kept for the test log; a NUL stderr is also accepted
        self.p = subprocess.Popen([str(_pythonw()), str(ROOT / "silent_stdio.pyw")], cwd=str(ROOT), env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                  creationflags=0x08000000)
        self.n = 0

    def request(self, method, params=None, timeout=180):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        self.p.stdin.write((json.dumps(msg) + "\n").encode()); self.p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                raise AssertionError("launcher closed its output")
            reply = json.loads(line)
            if reply.get("id") == self.n:
                return reply
        raise AssertionError(f"no reply to {method}")

    def notify(self, method):
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode()); self.p.stdin.flush()

    def close(self):
        self.p.stdin.close()
        self.p.wait(30)
        self.log.close()


INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}


@pytest.mark.skipif(not _pythonw().is_file(), reason="no pythonw.exe beside this interpreter")
def test_server_runs_only_while_used(tmp_path):
    env = dict(os.environ, KICAD_AI_TOOLS_DATA_DIR=str(tmp_path / "data"), KICAD_AI_TOOLS_SERVER_IDLE_SECONDS="6",
               KICAD_AI_TOOLS_CONFIG=str(tmp_path / "config.json"), KICAD_MCP_JOB_DIR=str(tmp_path / "jobs"),
               KICAD_AI_TOOLS_SERVER_PYTHON=str(Path(sys.executable).with_name("python.exe")))
    first = Session(env, tmp_path / "launcher.log")  # first session builds the manifest (one short server start)
    try:
        assert first.request("initialize", INIT)["result"]["serverInfo"]["name"]
    finally:
        first.close()
    assert (tmp_path / "data" / "mcp-manifest.json").is_file()

    s = Session(env, tmp_path / "launcher.log")
    try:
        init = s.request("initialize", INIT)
        s.notify("notifications/initialized")
        tools = s.request("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"adaptive_plan", "l4_run", "kicad_cli_run"} <= names
        assert init["result"]["serverInfo"]["name"]
        # A venv pythonw.exe is a redirector that runs the base interpreter as its child, so the
        # launcher may itself be one child; the server process is anything beyond that baseline.
        baseline = _children(s.p.pid)
        assert len(baseline) <= 1, f"handshake and tools/list must not start the server: {baseline}"
        out = s.request("tools/call", {"name": "kicad_capability_report", "arguments": {}})
        assert "result" in out, out
        assert _children(s.p.pid) - baseline, "a tool call must start the server"
        deadline = time.time() + 60
        while time.time() < deadline and _children(s.p.pid) - baseline:
            time.sleep(1)
        assert _children(s.p.pid) == baseline, "the idle server must stop"
        again = s.request("tools/call", {"name": "kicad_capability_report", "arguments": {}})
        assert "result" in again  # restarted transparently
    finally:
        s.close()


@pytest.mark.skipif(not _pythonw().is_file(), reason="no pythonw.exe beside this interpreter")
def test_a_client_that_discards_stderr_still_gets_a_working_launcher(tmp_path):
    env = dict(os.environ, KICAD_AI_TOOLS_DATA_DIR=str(tmp_path / "data"),
               KICAD_AI_TOOLS_CONFIG=str(tmp_path / "config.json"), KICAD_MCP_JOB_DIR=str(tmp_path / "jobs"))
    p = subprocess.Popen([str(_pythonw()), str(ROOT / "silent_stdio.pyw")], cwd=str(ROOT), env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT}
        p.stdin.write((json.dumps(msg) + "\n").encode()); p.stdin.flush()
        reply = json.loads(p.stdout.readline() or b"{}")
        assert reply.get("result", {}).get("serverInfo"), reply
    finally:
        p.stdin.close()
        p.wait(120)



def test_live_tripwires_fail_closed_when_the_owner_desktop_cannot_be_observed(monkeypatch):
    import hidden_kicad as hk
    monkeypatch.setattr(hk, "owner_desktop_scan", lambda roots: None)  # e.g. a locked workstation
    with pytest.raises(hk.OwnerDesktopUnobservable):
        hk.owner_windows_of_tree([os.getpid()])
    started = time.time()
    with pytest.raises(hk.OwnerDesktopUnobservable):  # run_hidden stops its task at once
        hk.run_hidden([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=60)
    assert time.time() - started < 30
    kicad = object.__new__(hk.HiddenKiCad)
    killed = []
    kicad.proc = type("Proc", (), {"pid": os.getpid(), "pids": lambda self: [os.getpid()]})()
    kicad.kill = lambda: killed.append(True)
    with pytest.raises(hk.OwnerDesktopUnobservable):
        kicad.check_tree()
    assert killed == [True]


def test_a_single_blind_sample_makes_a_live_run_unobserved(monkeypatch):
    sys.path.insert(0, str(ROOT / "kicad_mcp" / "adaptive_editor" / "tests_live"))
    import _harness
    answers = iter([[], None])  # one clean sample, then one where the desktop could not be opened
    monkeypatch.setattr(_harness, "owner_desktop_scan", lambda roots: next(answers, []))
    watch = _harness.Watch(interval=3600)  # no background samples during the test
    watch.check()
    watch.check()
    record = watch.close()
    assert record["unavailable_samples"] == 1 and record["observed"] is False


def test_the_review_script_and_the_live_harness_compute_the_same_package_hash():
    import importlib.util
    sys.path.insert(0, str(ROOT / "kicad_mcp" / "adaptive_editor" / "tests_live"))
    import _harness
    spec = importlib.util.spec_from_file_location("source_digest_script", ROOT / "scripts" / "source_digest.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.source_digest() == _harness.source_digest()


def test_the_live_tree_walk_ignores_a_stranger_with_a_reused_parent_pid(monkeypatch):
    import hidden_kicad as hk
    monkeypatch.setattr(hk, "process_table", lambda: [(10, 1), (11, 10), (12, 10)])
    assert hk.tree_pids([10], created={10: 100, 11: 120, 12: 50}.get) == {10, 11}


ORPHAN_MAKER = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
                "creationflags=0x08000000); print(p.pid, flush=True)")  # starts a grandchild, then exits at once


def _alive(pid: int) -> bool:
    import ctypes
    kernel = ctypes.WinDLL("kernel32")
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel.CloseHandle(handle)


def test_an_orphan_never_outlives_the_engine_runner():
    from kicad_mcp.adaptive_editor.adaptive_engine import session
    done = session.run_hidden([sys.executable, "-c", ORPHAN_MAKER], timeout=60)
    grandchild = int(done.stdout.strip())
    deadline = time.time() + 15
    while _alive(grandchild) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild)  # its Job Object closed with the call


def test_the_live_harness_owns_and_ends_children_of_an_exited_root():
    import hidden_kicad as hk
    with hk.HiddenDesktop("orphans") as desk:
        proc = hk.HiddenProcess([sys.executable, "-c", ORPHAN_MAKER], desk, stdout="pipe")
        grandchild = int(proc.stdout.readline())
        proc.wait(30)  # the root has exited
        assert grandchild in proc.pids()  # still owned (and watched) through the job
        assert grandchild in hk.owned_job_pids()
        assert grandchild in proc.kill()  # ended although its parent is gone
        assert not _alive(grandchild)
        proc.close()


def test_l4_descendant_kill_follows_links_only_with_creation_times(monkeypatch):
    from kicad_mcp.l4 import hidden_desktop as hd
    from kicad_mcp.l4 import monitor
    calls = []
    monkeypatch.setattr(monitor, "descendant_pids", lambda roots, rows, created=None: calls.append(created) or set(roots))
    owned = object.__new__(hd.OwnedProcess)
    owned.pid, owned.creation_time = os.getpid(), 1
    owned.job = type("NoMembers", (), {"pids": lambda self: []})()
    owned.kill_descendants()
    assert calls and calls[0] is hd.pid_creation_time


def test_the_live_watch_counts_a_failing_sample_as_blind_and_kills_on_a_hit(monkeypatch):
    sys.path.insert(0, str(ROOT / "kicad_mcp" / "adaptive_editor" / "tests_live"))
    import _harness
    killed = []
    monkeypatch.setattr(_harness, "kill_tree", lambda pid: killed.append(pid))
    answers = iter(["raise", [{"pid": 999999, "hwnd": 1, "title": "x", "visible": True}]])

    def scan(roots):
        answer = next(answers, [])
        if answer == "raise":
            raise RuntimeError("Set changed size during iteration")
        return answer

    monkeypatch.setattr(_harness, "owner_desktop_scan", scan)
    watch = _harness.Watch(interval=3600)
    watch._sample()  # the scan raised: counted as blind, never as clean, and the thread would survive
    watch._sample()  # a window: its owner is stopped at once
    record = watch.close()
    assert record["unavailable_samples"] == 1 and record["observed"] is False
    assert killed == [999999] and record["hits"]


def test_the_broker_watches_every_job_member_of_its_processes():
    from kicad_mcp.l4 import monitor
    pids = monitor.TaskPids()
    pids.add(10)
    pids.add_owner(type("Owned", (), {"pids": lambda self: [10, 11, 12]})())
    assert pids.snapshot() == {10, 11, 12}


def test_an_l4_launch_owns_children_of_its_exited_root(tmp_path):
    from kicad_mcp.l4 import hidden_desktop as hd
    marker = tmp_path / "grandchild.txt"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], creationflags=0x08000000)\n"
        f"open(r'{marker}', 'w').write(str(p.pid))\n", encoding="utf-8")
    with hd.PrivateDesktop() as desk:
        owned = hd.spawn_windowless_on_desktop(f'-I "{script}"', desk.name, str(tmp_path), "orphan-test")
        try:
            assert owned.wait(30000)  # the root has exited
            grandchild = int(marker.read_text())
            assert grandchild in owned.pids()  # still owned through the Job Object
            assert grandchild in owned.kill_descendants()
            assert not _alive(grandchild)
        finally:
            owned.close()
