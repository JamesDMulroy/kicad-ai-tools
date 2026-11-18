"""Launcher rules without a real server: jobs keep a server alive, server exit fails in-flight calls,
waiters are per server, a failed start leaves nothing behind, and the manifest cache follows data files."""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import threading

import pytest

from kicad_mcp import launcher as L


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Never read the real per-user config or data directory."""
    monkeypatch.setenv("KICAD_AI_TOOLS_CONFIG", str(tmp_path / "isolated-config.json"))
    monkeypatch.setenv("KICAD_AI_TOOLS_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setenv("KICAD_MCP_JOB_DIR", str(tmp_path / "isolated-jobs"))
    for var in ("KICAD_APP_PATH", "KICAD_CLI_PATH", "KICAD_DOCUMENTS_HOME"):  # no ambient KiCad overrides
        monkeypatch.delenv(var, raising=False)


class StubBackend:
    def __init__(self, *_args, **_kwargs):
        self.stopped = False
        self.sent = []
        self.tree = None
        self.pid = 4242

    def send(self, message):
        self.sent.append(message)

    def alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


def _launcher():
    return L.Launcher(io.StringIO(), io.StringIO())


def _emitted(launcher):
    return [json.loads(line) for line in launcher.stdout.getvalue().splitlines() if line.strip()]


def _journal(root, job_id, *statuses, broken_tail=False):
    folder = root / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "state-history.jsonl").open("a", encoding="utf-8") as f:
        for status in statuses:
            f.write("\n" + json.dumps({"job_id": job_id, "status": status}) + "\n")
        if broken_tail:
            f.write('\n{"status": "comple')  # an interrupted append
    return folder


def test_job_active_reads_the_authoritative_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_MCP_JOB_DIR", str(tmp_path))
    _journal(tmp_path, "j1", "queued", "running", broken_tail=True)
    (tmp_path / "j1" / "state.json").write_text('{"status": "completed"}', encoding="utf-8")  # stale mirror
    assert L.job_active("j1") is True
    _journal(tmp_path, "j1", "completed")
    assert L.job_active("j1") is False
    assert L.job_active("missing") is False


def test_job_id_is_read_from_structured_or_text_results():
    structured = {"result": {"structuredContent": {"result": {"job_id": "abc"}}}}
    text = {"result": {"content": [{"type": "text", "text": json.dumps({"job_id": "def"})}]}}
    assert L._job_id_from(structured) == "abc" and L._job_id_from(text) == "def"
    assert L._job_id_from({"result": {"content": [{"type": "text", "text": "not json"}]}}) is None


def test_a_running_job_keeps_the_server_until_it_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_MCP_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", "5")
    la = _launcher()
    backend = StubBackend()
    la.backend = backend
    la.last_activity -= 3600
    # a start_* tool result carrying a job id registers the job
    la.inflight[7] = ("tools/call", "start_board_bundle_job", backend)
    la._from_backend(backend, {"jsonrpc": "2.0", "id": 7,
                               "result": {"content": [{"type": "text", "text": json.dumps({"job_id": "j9"})}]}})
    assert la.jobs == {"j9"} and la.job_owner["j9"] is backend  # registered by the reply path itself
    _journal(tmp_path, "j9", "queued", "running")
    la.last_activity -= 3600
    la.stop_backend_if_idle()
    assert backend.stopped is False and la.backend is backend
    _journal(tmp_path, "j9", "completed")
    la.stop_backend_if_idle()
    assert backend.stopped is True and la.backend is None


def test_server_exit_fails_in_flight_calls():
    la = _launcher()
    backend = StubBackend()
    la.backend = backend
    la.inflight[5] = ("tools/call", "kicad_cli_run", backend)
    la._from_backend(backend, None)
    errors = [m for m in _emitted(la) if m.get("id") == 5]
    assert errors and "exited" in errors[0]["error"]["message"]
    assert la.backend is None and la.inflight == {}


def test_an_old_servers_exit_does_not_wake_a_new_servers_waiter():
    la = _launcher()
    old, new = StubBackend(), StubBackend()
    event = threading.Event()
    la.pending["kicad-ai-tools-launcher:1"] = (new, event)
    la._from_backend(old, None)
    assert not event.is_set()
    la._from_backend(new, None)
    assert event.is_set()


def test_messages_from_a_non_current_server_never_reach_the_client():
    la = _launcher()
    la.backend = StubBackend()
    la._from_backend(StubBackend(), {"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
    assert _emitted(la) == []


def test_a_failed_start_stops_the_new_server(monkeypatch):
    la = _launcher()
    made = []

    def make(*args, **kwargs):
        made.append(StubBackend())
        return made[-1]

    def no_answer(*args, **kwargs):
        raise TimeoutError("server did not answer initialize")

    monkeypatch.setattr(L, "Backend", make)
    monkeypatch.setattr(la, "_own_request", no_answer)
    with pytest.raises(TimeoutError):
        la.ensure_backend()
    assert made and made[0].stopped is True and la.backend is None


def test_manifest_digest_follows_packaged_data(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    (pkg / "l4" / "registry").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    data = pkg / "l4" / "registry" / "APPROVED.sha256"
    data.write_text("aa  r.json\n", encoding="utf-8")
    monkeypatch.setattr(L, "PACKAGE", pkg)
    before = L.source_digest()
    data.write_text("bb  r.json\n", encoding="utf-8")
    assert L.source_digest() != before


def test_a_crashed_server_takes_its_jobs_with_it():
    la = _launcher()
    backend = StubBackend()
    la.backend, la.jobs = backend, {"j1", "j2"}
    la.job_owner = {"j1": backend, "j2": backend}
    la._from_backend(backend, None)
    assert la.jobs == set() and la.job_owner == {}  # the next server can idle-stop


def test_a_request_is_registered_before_its_server_starts(monkeypatch):
    la = _launcher()
    seen = {}

    def start():
        seen["registered"] = 3 in la.inflight
        la.backend = StubBackend()
        return la.backend

    monkeypatch.setattr(la, "ensure_backend", start)
    la.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kicad_cli_run"}})
    assert seen["registered"] is True


def test_catalogue_and_forwarding_failures_answer_instead_of_ending_the_session(monkeypatch):
    la = _launcher()

    def broken():
        raise RuntimeError("manifest build failed")

    monkeypatch.setattr(la, "load_manifest", broken)
    la.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert _emitted(la)[-1]["error"]["message"] == "manifest build failed"

    class Broken(StubBackend):
        def send(self, message):
            raise BrokenPipeError("server pipe closed")

    la.backend = Broken()
    la.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}})
    la.handle({"jsonrpc": "2.0", "id": "srv-1", "result": {}})  # a reply to a server-initiated request


class AnsweringServer(StubBackend):
    """Answers the launcher's own handshake and catalogue requests, like a real server would."""
    launcher = None
    started = 0

    def __init__(self, *args, **kwargs):
        super().__init__()
        AnsweringServer.started += 1

    def send(self, message):
        super().send(message)
        mid = message.get("id")
        if mid is None:
            return
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "stub"}}
        else:
            result = {"tools": [{"name": "rebuilt"}]} if message["method"] == "tools/list" else {}
        AnsweringServer.launcher._from_backend(self, {"jsonrpc": "2.0", "id": mid, "result": result})


def test_the_manifest_is_rebuilt_when_the_digest_no_longer_matches(monkeypatch, tmp_path):
    la = _launcher()
    AnsweringServer.launcher, AnsweringServer.started = la, 0
    monkeypatch.setattr(L, "Backend", AnsweringServer)
    path = L.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"digest": "stale", "initialize": {}, "lists": {}}), encoding="utf-8")
    manifest = la.load_manifest()
    assert AnsweringServer.started == 1 and manifest["digest"] == L.source_digest()
    assert manifest["lists"]["tools/list"]["result"]["tools"] == [{"name": "rebuilt"}]
    fresh = _launcher()
    AnsweringServer.launcher = fresh
    fresh.load_manifest()  # digest matches now: served from the file, no server start
    assert AnsweringServer.started == 1


def test_config_module_imports_with_a_malformed_config(tmp_path):
    import os
    import subprocess
    import sys
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    env = dict(os.environ, KICAD_AI_TOOLS_CONFIG=str(bad))
    done = subprocess.run([sys.executable, "-c", "import kicad_mcp.config as c; print(c.KICAD_CONFIG_ERROR[:14])"],
                          capture_output=True, text=True, env=env, timeout=120,
                          creationflags=0x08000000 if os.name == "nt" else 0)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "config_invalid"


def test_kicad_cli_never_falls_back_after_a_wrong_explicit_setting(monkeypatch, tmp_path):
    from kicad_mcp.adaptive_editor import kicad_env
    from kicad_mcp.utils.kicad_cli import KiCadCLIManager
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setenv("KICAD_APP_PATH", str(tmp_path / "wrong-root"))
    kicad_env.kicad_app_root.cache_clear()
    try:
        assert KiCadCLIManager().find_kicad_cli(force_refresh=True) is None
    finally:
        kicad_env.kicad_app_root.cache_clear()


def test_session_end_waits_for_a_job_start_still_in_flight(tmp_path, monkeypatch):
    import time as _time
    la = _launcher()  # stdin is empty: run() reaches session end at once
    backend = StubBackend()
    la.backend = backend
    la.inflight[11] = ("tools/call", "start_board_gate_job", backend)

    def server_side():
        _time.sleep(0.5)  # the start_* reply arrives after the client has gone
        la._from_backend(backend, {"jsonrpc": "2.0", "id": 11,
                                   "result": {"content": [{"type": "text", "text": json.dumps({"job_id": "late"})}]}})
        _journal(tmp_path / "isolated-jobs", "late", "queued", "running")
        _time.sleep(1.5)
        _journal(tmp_path / "isolated-jobs", "late", "completed")

    worker = threading.Thread(target=server_side)
    worker.start()
    started = _time.monotonic()
    la.run()
    worker.join()
    assert backend.stopped is True
    assert _time.monotonic() - started >= 1.5  # it waited for the late job to finish


def test_writing_to_a_closed_client_is_harmless():
    out = io.StringIO()
    la = L.Launcher(io.StringIO(), out)
    out.close()
    la.emit({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert la.client_gone is True


def test_cli_resolution_reports_config_errors_without_fallback(monkeypatch, tmp_path):
    from kicad_mcp.adaptive_editor import kicad_env
    from kicad_mcp.tools.drc_impl import cli_drc
    from kicad_mcp.utils import kicad_api_detection
    from kicad_mcp.utils.kicad_cli import KiCadCLIManager
    bad = tmp_path / "isolated-config.json"
    bad.write_text("{not json", encoding="utf-8")
    kicad_env.kicad_app_root.cache_clear()
    try:
        cli, error = kicad_env.resolve_kicad_cli()
        assert cli is None and error.startswith("config_invalid")
        assert kicad_api_detection.check_for_cli_api() is False
        ok, reason = kicad_api_detection.cli_api_status()
        assert ok is False and reason.startswith("config_invalid")  # the actual cause, not "install KiCad"
        assert cli_drc.find_kicad_cli() is None
        drc = asyncio.run(cli_drc.run_drc_via_cli(str(tmp_path / "board.kicad_pcb")))
        assert drc["success"] is False and "config_invalid" in drc["error"] and "Install KiCad" not in drc["error"]
        assert KiCadCLIManager().find_kicad_cli(force_refresh=True) is None
    finally:
        kicad_env.kicad_app_root.cache_clear()


def test_setup_stores_only_explicit_choices_and_can_repair(monkeypatch, tmp_path):
    from kicad_mcp import setup
    from kicad_mcp.adaptive_editor import kicad_env
    monkeypatch.setattr(setup, "client_entry", lambda: {"command": "stub", "args": [], "env": {}})
    cfg = tmp_path / "isolated-config.json"
    cfg.write_text("{broken", encoding="utf-8")
    kicad_env.kicad_app_root.cache_clear()
    setup.main(["--idle-seconds", "120"])
    stored = json.loads(cfg.read_text(encoding="utf-8"))
    assert stored == {"server_idle_seconds": 120.0}  # no discovered root or version is pinned
    assert list(tmp_path.glob("isolated-config.json.broken-*"))  # the malformed file was kept aside
    cfg.write_text(json.dumps({"kicad_app_root": str(tmp_path / "moved-away"), "server_idle_seconds": 120}),
                   encoding="utf-8")
    kicad_env.kicad_app_root.cache_clear()
    assert setup.main(["--check"]) == 2  # a stale pinned root is reported...
    setup.main(["--forget", "kicad_app_root"])  # ...and can be removed
    assert "kicad_app_root" not in json.loads(cfg.read_text(encoding="utf-8"))
    kicad_env.kicad_app_root.cache_clear()


def test_client_ids_in_the_launcher_namespace_are_refused():
    la = _launcher()
    la.handle({"jsonrpc": "2.0", "id": "kicad-ai-tools-launcher:1", "method": "tools/call", "params": {}})
    reply = _emitted(la)[-1]
    assert reply["error"]["code"] == -32600 and la.inflight == {}


def test_a_late_reply_to_a_timed_out_request_is_dropped():
    la = _launcher()
    backend = StubBackend()
    la._from_backend(backend, {"jsonrpc": "2.0", "id": "kicad-ai-tools-launcher:99", "result": {}})
    assert la.replies == {}


def test_setup_entry_carries_every_location_variable(monkeypatch, tmp_path):
    from kicad_mcp import setup
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "docs"))
    monkeypatch.setenv("KICAD_USER_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", "90")
    env = setup.client_entry()["env"]
    assert env["KICAD_DOCUMENTS_HOME"] == str(tmp_path / "docs")
    assert env["KICAD_USER_DIR"] == str(tmp_path / "user")
    assert env["KICAD_AI_TOOLS_SERVER_IDLE_SECONDS"] == "90"
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(tmp_path / "pcm"))
    assert setup.client_entry()["env"]["KICAD10_3RD_PARTY"] == str(tmp_path / "pcm")


class _Lines:
    def __init__(self, *lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


def test_a_malformed_server_line_never_stops_the_reader():
    seen = []

    def on_message(_backend, message):
        seen.append(message)
        if message == {"boom": 1}:
            raise RuntimeError("handler failed")

    backend = object.__new__(L.Backend)
    backend.on_message = on_message
    lines = _Lines(b"not json\n", b"[1, 2]\n", b'{"boom": 1}\n', b'{"id": 7}\n')
    backend.proc = type("P", (), {"stdout": lines})()
    backend._read()
    assert seen == [{"boom": 1}, {"id": 7}, None]  # bad lines skipped, a failing handler survived, EOF reported


def test_a_cached_in_memory_manifest_is_dropped_when_the_package_changes(monkeypatch):
    la = _launcher()
    AnsweringServer.launcher, AnsweringServer.started = la, 0
    monkeypatch.setattr(L, "Backend", AnsweringServer)
    la.manifest = {"digest": "from-an-older-package", "initialize": {}, "lists": {}}
    assert la.load_manifest()["digest"] == L.source_digest() and AnsweringServer.started == 1


@pytest.mark.parametrize("line", ["5", '"x"', "null", "[1]", "[]"])
def test_any_client_json_value_is_answered_and_the_session_continues(line):
    ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    stdin = io.StringIO("".join(f"{text}\n" for text in (line, ping)))
    la = L.Launcher(stdin, io.StringIO())
    la.run()
    replies = _emitted(la)
    assert replies[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}  # the session survived the bad line
    assert any("error" in r for r in replies[:-1])


def test_drc_history_imports_without_a_data_folder():
    code = "\n".join((
        "from kicad_mcp.adaptive_editor import kicad_env as k",
        "k.windows_known_folder = lambda _folder: None",
        "import kicad_mcp.utils.drc_history as d  # must import even with no data folder",
        "try:",
        "    d.history_dir()",
        "except k.ConfigError as e:",
        "    print('refused:', k.DATA_DIR_ENV in str(e))",
    ))
    env = {k: v for k, v in os.environ.items() if k not in ("LOCALAPPDATA", "KICAD_AI_TOOLS_DATA_DIR")}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120,
                          cwd=str(L.PACKAGE.parent))
    assert done.returncode == 0, done.stderr
    if os.name == "nt":
        assert "refused: True" in done.stdout


def test_setup_check_reports_a_missing_pythonw_instead_of_exiting(monkeypatch, capsys, tmp_path):
    from kicad_mcp import setup
    def no_pythonw():
        raise setup.EntryError("pythonw.exe not found beside the interpreter")
    monkeypatch.setattr(setup, "client_entry", no_pythonw)
    code = setup.main(["--check"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["mcp_server_entry"] == {"error": "pythonw.exe not found beside the interpreter"}


def test_a_client_on_another_protocol_version_is_negotiated_by_the_real_server(monkeypatch):
    la = _launcher()
    la.manifest = {"digest": L.source_digest(), "lists": {},
                   "initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "cached"}}}
    started = type("B", (), {"init_result": {"protocolVersion": "2025-03-26", "serverInfo": {"name": "real"}}})()
    monkeypatch.setattr(la, "ensure_backend", lambda: started)
    same = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "a", "version": "1"}}
    la.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": same})
    assert _emitted(la)[-1]["result"]["serverInfo"]["name"] == "cached"  # same version: no server start
    other = dict(same, protocolVersion="2025-03-26")
    la.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": other})
    assert _emitted(la)[-1]["result"] == started.init_result  # the server's own negotiation, never the cache's


def test_the_idle_period_survives_a_config_broken_mid_session(tmp_path):
    (tmp_path / "isolated-config.json").write_text("{broken", encoding="utf-8")
    assert L.idle_seconds() == float(L.DEFAULT_IDLE_SECONDS)


def test_describe_and_setup_report_an_unresolvable_config_folder(monkeypatch, capsys):
    from kicad_mcp import setup
    from kicad_mcp.adaptive_editor import kicad_env

    def no_folder():
        raise kicad_env.ConfigError("Windows reported no AppData folder; set KICAD_AI_TOOLS_CONFIG")

    monkeypatch.setattr(kicad_env, "config_path", no_folder)
    described = kicad_env.describe()  # documented never to raise
    assert described["config_path"] is None and "KICAD_AI_TOOLS_CONFIG" in described["error"]
    for argv in (["--check"], []):
        assert setup.main(argv) == 2
        report = json.loads(capsys.readouterr().out)
        assert "KICAD_AI_TOOLS_CONFIG" in report["fix"]


def test_a_found_but_failing_kicad_cli_is_not_reported_as_missing(monkeypatch, tmp_path):
    from kicad_mcp.utils.kicad_cli import KiCadCLIError, KiCadCLIManager
    manager = KiCadCLIManager()
    monkeypatch.setattr(manager, "_detect_cli_path", lambda: str(tmp_path / "kicad-cli.exe"))
    monkeypatch.setattr(manager, "_validate_cli_path", lambda _path: False)
    with pytest.raises(KiCadCLIError) as caught:
        manager.get_cli_path()
    assert "failed to run '--version'" in str(caught.value) and "Install KiCad" not in str(caught.value)


def test_a_start_reply_never_leaves_its_job_unregistered_between_locked_steps():
    la = _launcher()
    backend = StubBackend()
    la.backend = backend
    real = la.state_lock
    seen = []

    class Watching:  # records, at every unlock, whether the start_* call is gone but its job unknown
        def __enter__(self):
            real.acquire()

        def __exit__(self, *exc):
            seen.append(7 not in la.inflight and "j7" not in la.jobs)
            real.release()

    la.state_lock = Watching()
    la.inflight[7] = ("tools/call", "start_board_bundle_job", backend)
    la._from_backend(backend, {"jsonrpc": "2.0", "id": 7,
                               "result": {"content": [{"type": "text", "text": json.dumps({"job_id": "j7"})}]}})
    assert la.jobs == {"j7"} and not any(seen)


def test_a_manifest_that_cannot_be_cached_is_still_served(monkeypatch):
    la = _launcher()
    AnsweringServer.launcher, AnsweringServer.started = la, 0
    monkeypatch.setattr(L, "Backend", AnsweringServer)

    def locked(*_args):
        raise PermissionError(13, "another launcher holds the file")

    monkeypatch.setattr(L.os, "replace", locked)
    la.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    reply = _emitted(la)[-1]
    assert reply["result"]["serverInfo"] == {"name": "stub"}  # the handshake succeeds from memory
    assert not list(L.manifest_path().parent.glob("*.tmp"))  # no temporary file left behind


@pytest.mark.parametrize("value", ["1" + "0" * 400, "NaN", "\"soon\""])
def test_an_unusable_idle_value_falls_back_to_the_default(tmp_path, value):
    (tmp_path / "isolated-config.json").write_text('{"server_idle_seconds": ' + value + "}", encoding="utf-8")
    assert L.idle_seconds() == float(L.DEFAULT_IDLE_SECONDS)


def test_setup_check_reports_a_wrong_kicad_root(tmp_path, capsys):
    from kicad_mcp import setup
    from kicad_mcp.adaptive_editor import kicad_env
    try:
        assert setup.main(["--check", "--kicad-root", str(tmp_path / "no-kicad-here")]) == 2
        report = json.loads(capsys.readouterr().out)
        assert "error" in report["kicad"]
        assert not (tmp_path / "isolated-config.json").exists()  # --check writes nothing
        assert kicad_env.KICAD_ROOT_ENV not in os.environ  # the probe is not left behind...
        if "claude_code_add_json" in report:  # ...nor carried into the printed client entry
            assert kicad_env.KICAD_ROOT_ENV not in json.loads(report["claude_code_add_json"])["env"]
    finally:
        os.environ.pop(kicad_env.KICAD_ROOT_ENV, None)
        kicad_env.kicad_app_root.cache_clear()


def test_a_server_found_dead_before_its_eof_still_fails_its_calls_and_frees_its_jobs(monkeypatch):
    la = _launcher()
    AnsweringServer.launcher, AnsweringServer.started = la, 0
    monkeypatch.setattr(L, "Backend", AnsweringServer)
    old = StubBackend()
    la.backend = old
    la.inflight["A"] = ("tools/call", "kicad_cli_run", old)
    la.jobs.add("old-job")
    la.job_owner["old-job"] = old
    old.stopped = True  # crashed; its reader has not reported EOF yet
    new = la.ensure_backend()  # call B arrives first and starts a replacement
    assert new is not old and la.backend is new
    failed = [m for m in _emitted(la) if m.get("id") == "A"]
    assert failed and "exited" in failed[0]["error"]["message"]  # call A is answered, not left hanging
    assert "A" not in la.inflight and "old-job" not in la.jobs  # the old server's job cannot pin the new one
    la.inflight["B"] = ("tools/call", "kicad_cli_run", new)
    la._from_backend(old, None)  # the late EOF changes nothing more
    assert [m for m in _emitted(la) if m.get("id") == "A"] == failed and "B" in la.inflight
    assert la.backend is new


@pytest.mark.skipif(os.name != "nt", reason="process identity check is Windows-only")
def test_a_job_whose_server_is_gone_is_not_active(tmp_path, monkeypatch):
    from kicad_mcp.utils import winproc
    monkeypatch.setenv("KICAD_MCP_JOB_DIR", str(tmp_path))

    def journal(job_id, owner):
        folder = tmp_path / job_id
        folder.mkdir()
        (folder / "state-history.jsonl").write_text(
            "\n" + json.dumps({"job_id": job_id, "status": "running", "owner": owner}) + "\n", encoding="utf-8")

    journal("live", winproc.identity(os.getpid()))
    me = winproc.identity(os.getpid())
    journal("reused-pid", {"pid": me["pid"], "created": me["created"] - 1})  # same pid, another process
    assert L.job_active("live") is True
    assert L.job_active("reused-pid") is False


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "soon"])
def test_an_unusable_idle_environment_value_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", value)  # float("inf") parses: isfinite rejects it
    assert L.idle_seconds() == float(L.DEFAULT_IDLE_SECONDS)


def test_the_idle_loop_stops_an_unused_server_and_survives_a_failing_check(monkeypatch):
    monkeypatch.setenv("KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", "5")  # checked every 5/3 s
    la = _launcher()
    backend = StubBackend()
    la.backend = backend
    la.last_activity -= 3600
    calls = []
    real = la.stop_backend_if_idle

    def first_fails():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        real()

    la.stop_backend_if_idle = first_fails
    loop = threading.Thread(target=la._idle_loop, daemon=True)
    loop.start()
    deadline = threading.Event()
    for _ in range(100):
        if backend.stopped:
            break
        deadline.wait(0.1)
    la.stop.set()
    loop.join(5)
    assert backend.stopped and len(calls) >= 2 and not loop.is_alive()


def test_a_reply_to_a_retired_call_is_never_sent_twice():
    la = _launcher()
    backend = StubBackend()
    la.backend = backend
    la.inflight["A"] = ("tools/call", "kicad_cli_run", backend)
    la._retire(backend)  # the client gets the "server exited" error for A
    la._from_backend(backend, {"jsonrpc": "2.0", "id": "A", "result": {}})  # the real reply arrives late
    assert [m for m in _emitted(la) if m.get("id") == "A"] == [
        {"jsonrpc": "2.0", "id": "A", "error": {"code": -32603, "message": "kicad-ai-tools server exited; retry the call"}}]


def test_a_failed_send_leaves_no_pending_request():
    la = _launcher()

    class Broken(StubBackend):
        def send(self, message):
            raise BrokenPipeError("server gone")

    with pytest.raises(BrokenPipeError):
        la._own_request(Broken(), "tools/list", {})
    assert la.pending == {}


def test_setup_prints_an_entry_claude_mcp_add_json_accepts(monkeypatch, capsys):
    from kicad_mcp import setup
    monkeypatch.setattr(setup, "client_entry", lambda: {"type": "stdio", "command": "pythonw", "args": [], "env": {}})
    setup.main(["--check"])
    report = json.loads(capsys.readouterr().out)
    entry = json.loads(report["claude_code_add_json"])
    assert entry["type"] == "stdio" and entry["command"] == "pythonw"  # a server definition, not a name map
    assert report["mcp_server_entry"] == {setup.SERVER_NAME: entry}


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows-only")
def test_a_job_object_closed_from_two_threads_at_once_is_closed_once():
    from kicad_mcp.utils import winproc
    for _ in range(10):
        tree = winproc.Tree()
        proc = tree.spawn([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        errors = []
        gate = threading.Barrier(2)

        def close():
            gate.wait()
            try:
                tree.close()
            except Exception as exc:  # any error here is the race
                errors.append(exc)

        threads = [threading.Thread(target=close) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        assert errors == [] and tree.handle is None
        assert proc.wait(10) is not None  # the contained process ended with the tree


def test_the_digest_follows_the_base_interpreter_and_installed_packages(tmp_path):
    base = tmp_path / "base"
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    base.mkdir()
    redirector = venv / "Scripts" / "python.exe"
    redirector.write_bytes(b"redirector")
    (base / "python.exe").write_bytes(b"python 3.12.1")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nversion = 3.12.1\n", encoding="utf-8")
    site = venv / "Lib" / "site-packages"
    (site / "mcp-1.0.0.dist-info").mkdir(parents=True)
    before = L.interpreter_fingerprint(redirector)
    (base / "python.exe").write_bytes(b"python 3.12.10 upgraded")  # in-place base upgrade, redirector untouched
    after_upgrade = L.interpreter_fingerprint(redirector)
    assert after_upgrade != before
    (site / "mcp-1.0.0.dist-info").rename(site / "mcp-1.1.0.dist-info")  # pip install -U mcp
    assert L.interpreter_fingerprint(redirector) != after_upgrade
