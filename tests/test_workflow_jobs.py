"""Owned asynchronous execution on disposable bundles and waiting fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kicad_mcp.utils.workflow_jobs import WorkflowJobs
from kicad_mcp.utils import boardtooling as bt
from kicad_mcp.tools.workflow_tools import register_workflow_tools
from mcp.server.fastmcp import FastMCP
import asyncio


def until(predicate):
    # Test-only hang detector; 30 s was exceeded under concurrent native regression.
    # This does not impose a product job lifetime.
    deadline = time.monotonic() + 180
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Local fixture did not reach its synchronization point")
        time.sleep(0.05)



def _boardtooling_configured() -> bool:
    from kicad_mcp.utils import boardtooling
    return boardtooling.SCRIPTS.is_dir()


# These jobs run real BoardTooling operations; BoardTooling is optional and configured per machine.
requires_boardtooling = unittest.skipUnless(_boardtooling_configured(),
                                            "BoardTooling is not configured (KICAD_AI_TOOLS_BOARDTOOLING_ROOT)")

class WorkflowJobTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-job-test-", dir=scratch)
        self.root = Path(self.temp.name)
        # Spawned workers inherit the environment: keep them off the real per-user config and data.
        self._saved_env = {k: os.environ.get(k) for k in ("KICAD_AI_TOOLS_CONFIG", "KICAD_AI_TOOLS_DATA_DIR",
                                                          "KICAD_MCP_JOB_DIR", "KICAD_AI_TOOLS_BOARDTOOLING_ROOT")}
        if _boardtooling_configured():  # the BoardTooling the skip guard saw (env or config) reaches the workers
            from kicad_mcp.utils import boardtooling
            os.environ["KICAD_AI_TOOLS_BOARDTOOLING_ROOT"] = str(boardtooling.ROOT)
        os.environ["KICAD_AI_TOOLS_CONFIG"] = str(self.root / "isolated-config.json")
        os.environ["KICAD_AI_TOOLS_DATA_DIR"] = str(self.root / "isolated-data")
        os.environ["KICAD_MCP_JOB_DIR"] = str(self.root / "journal")
        self.manager = WorkflowJobs(self.root / "journal")

    def tearDown(self):
        self.manager.close()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _start_native_output_fixture(self, label):
        """Run the actual worker dispatch and contained command behind a fixture scene."""
        marker = self.root / (label + "-native-child.json")
        native_code = (
            "import subprocess,sys,pathlib,json,threading; "
            "p=subprocess.Popen([sys.executable,'-B','-c',"
            "'import threading;threading.Event().wait()'],"
            "creationflags=subprocess.CREATE_NO_WINDOW,stdout=sys.stdout,stderr=sys.stderr); "
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}})); "
            "print('retained native output',flush=True);"
            "print('retained native error',file=sys.stderr,flush=True);"
            "threading.Event().wait()"
        )
        worker_code = (
            "import sys; from kicad_mcp.utils import boardtooling as bt; "
            "from kicad_mcp import workflow_job_worker as worker; "
            f"bt.query_scene=lambda **kw: bt._run([sys.executable,'-B','-c',{native_code!r}]); "
            "sys.argv=['worker',sys.argv[1],sys.argv[2]]; "
            "raise SystemExit(worker.main())"
        )
        actual_tree = self.manager.native.Tree
        class FixtureTree(actual_tree):
            def spawn(self, argv, **kwargs):
                return super().spawn([sys.executable, "-B", "-c", worker_code,
                                      argv[-2], argv[-1]], **kwargs)
        self.manager.native.Tree = FixtureTree
        project = self.root / (label + ".kicad_pro")
        project.write_text("{}", encoding="utf-8")
        expected = "a" * 64
        started = self.manager.start("query_scene", {
            "project": str(project), "expected_digest": expected,
            "bbox_mm": [0.0, 0.0, 1.0, 1.0]})
        jid = started["job_id"]
        folder = Path(started["evidence_directory"])
        def ready():
            commands = list((folder / "native-commands").glob("*")) if (folder / "native-commands").is_dir() else []
            return (marker.is_file() and len(commands) == 1 and
                    (commands[0] / "stdout.bin").is_file() and
                    (commands[0] / "stderr.bin").is_file() and
                    b"retained native output" in (commands[0] / "stdout.bin").read_bytes() and
                    b"retained native error" in (commands[0] / "stderr.bin").read_bytes())
        until(ready)
        evidence = next((folder / "native-commands").iterdir())
        events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
        native_pid = next(item["pid"] for item in events if item["event"] == "running")
        descendant_pid = json.loads(marker.read_text())["pid"]
        identities = {pid: self.manager.native.identity(pid) for pid in (native_pid, descendant_pid)}
        self.assertTrue(all(identities.values()))
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "running", state)
        self.assertIsNotNone(state.get("process"))
        command = json.loads((evidence / "command.json").read_text())
        self.assertEqual(command["context"]["job_request"]["arguments"]["expected_digest"], expected)
        return jid, evidence, identities

    def _assert_native_fixture_reaped(self, jid, evidence, identities):
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "cancelled", state)
        self.assertTrue(state["owned_tree_stopped"], state)
        self.assertIn(str(evidence), state["native_evidence"])
        self.assertIn(b"retained native output", (evidence / "stdout.bin").read_bytes())
        self.assertIn(b"retained native error", (evidence / "stderr.bin").read_bytes())
        events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "parent_owned_tree_stopped")
        self.assertTrue(events[-1]["owned_tree_stopped"])
        self.assertGreater(events[-1]["stdout_bytes_retained"], 0)
        self.assertGreater(events[-1]["stderr_bytes_retained"], 0)
        for pid, identity in identities.items():
            self.assertNotEqual(self.manager.native.identity(pid), identity)
        self.assertTrue((Path(state["evidence_directory"]) / "result.json").is_file())
        self.assertEqual(json.loads((Path(state["evidence_directory"]) / "request.json").read_text())["operation"], "query_scene")

    def test_cancel_running_worker_retains_contained_native_output(self):
        jid, evidence, identities = self._start_native_output_fixture("cancel")
        self.assertTrue(self.manager.cancel(jid)["cancel_requested"])
        self._assert_native_fixture_reaped(jid, evidence, identities)

    def test_shutdown_running_worker_retains_contained_native_output(self):
        jid, evidence, identities = self._start_native_output_fixture("shutdown")
        self.manager.close()
        self._assert_native_fixture_reaped(jid, evidence, identities)

    def test_cleanup_failure_never_claims_verified_tree_stop(self):
        actual_tree = self.manager.native.Tree
        class UnverifiedTree(actual_tree):
            def close(self):
                super().close()
                raise OSError("fixture close verification failed")
        self.manager.native.Tree = UnverifiedTree
        project = self.root / "cleanup.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        jid = self.manager.start("inspect_bundle", {"project": str(project)})["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "cleanup_unverified", state)
        self.assertFalse(state["owned_tree_stopped"])
        self.assertIn("fixture close verification failed", state["cleanup_error"])
        self.assertFalse(state["operation_ok"])
        self.assertFalse(state["design_accepted"])

    @requires_boardtooling
    def test_concurrent_state_readers_cannot_break_durable_publication(self):
        project = self.root / "concurrent.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        entered, proceed, stop = threading.Event(), threading.Event(), threading.Event()
        actual_tree = self.manager.native.Tree
        class PausedTree(actual_tree):
            def spawn(self, argv, **kwargs):
                entered.set()
                if not proceed.wait(180):  # Test synchronization watchdog only.
                    raise AssertionError("Fixture spawn was never released")
                return super().spawn(argv, **kwargs)
        self.manager.native.Tree = PausedTree
        jid = self.manager.start("inspect_bundle", {"project": str(project)})["job_id"]
        folder = self.manager._folder(jid)
        until(entered.is_set)
        errors = []
        def poll():
            while not stop.is_set():
                try:
                    self.manager.status(jid)
                    try:
                        json.loads((folder / "state.json").read_text(encoding="utf-8"))
                    except OSError:
                        pass  # A Windows rename may briefly deny an open.
                    except ValueError as exc:
                        errors.append(exc)  # The atomic mirror must stay valid JSON.
                        return
                except BaseException as exc:
                    errors.append(exc)
                    return
        reader = threading.Thread(target=poll)
        reader.start()
        try:
            with (folder / "state.json").open("r", encoding="utf-8") as held_reader:
                self.assertTrue(held_reader.read(1))
                proceed.set()
                until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
            state = self.manager.status(jid)
            self.assertEqual(state["status"], "completed", state)
            self.assertFalse(errors, errors)
            history = [json.loads(line) for line in (folder / "state-history.jsonl").read_text().splitlines()
                       if line.strip()]
            self.assertIn("queued", [item["status"] for item in history])
            self.assertIn("running", [item["status"] for item in history])
            self.assertEqual(history[-1]["status"], "completed")
            self.assertTrue((folder / "result.json").is_file())
        finally:
            proceed.set()
            stop.set()
            reader.join(timeout=180)
            self.assertFalse(reader.is_alive())

    @requires_boardtooling
    def test_state_mirror_failure_is_reported_and_journal_failure_raises(self):
        project = self.root / "publication.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        jid = self.manager.start("inspect_bundle", {"project": str(project)})["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        folder = self.manager._folder(jid)
        real_replace = os.replace
        def deny_mirror(source, target):
            if target == folder / "state.json":
                raise PermissionError("permanent mirror write refusal")
            return real_replace(source, target)
        with patch("kicad_mcp.utils.workflow_jobs.os.replace", side_effect=deny_mirror):
            self.manager._save_state(folder, self.manager._read_state(folder))
        self.assertIn("permanent mirror write refusal",
                      self.manager.status(jid)["state_mirror_error"])
        self.assertEqual(json.loads((folder / "state.json").read_text())["status"], "completed")
        original_open = Path.open
        def deny_journal(path, *args, **kwargs):
            if path == folder / "state-history.jsonl" and args and args[0] == "a":
                raise PermissionError("permanent journal write refusal")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", autospec=True, side_effect=deny_journal):
            with self.assertRaisesRegex(PermissionError, "permanent journal write refusal"):
                self.manager._save_state(folder, self.manager._read_state(folder))

    def test_interrupted_multibyte_append_preserves_and_recovers_state(self):
        project = self.root / "fragment.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        jid = self.manager.start("inspect_bundle", {"project": str(project)})["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        folder = self.manager._folder(jid)
        before = self.manager._read_state(folder)
        journal = folder / "state-history.jsonl"
        fragment = b'{"interrupted":"\xc3'
        with journal.open("ab") as stream:
            stream.write(fragment)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual(self.manager._read_state(folder)["status"], before["status"])
        after = dict(before, recovery_marker="after \u00e9 fragment")
        self.manager._save_state(folder, after)
        self.assertEqual(self.manager._read_state(folder)["recovery_marker"], "after \u00e9 fragment")
        self.assertIn(fragment, journal.read_bytes())  # Recovery must retain damaged evidence.

    @requires_boardtooling
    def test_complete_and_failure_retain_request_result_and_logs(self):
        project = self.root / "fixture.kicad_pro"
        project.write_text("{}")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)")
        submitted = self.manager.start("inspect_bundle", {"project": str(project)})
        jid = submitted["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "completed", state)
        self.assertTrue(state["owned_tree_stopped"])
        self.assertIn("result", state)
        self.assertTrue((Path(state["evidence_directory"]) / "request.json").is_file())
        self.assertTrue((Path(state["evidence_directory"]) / "result.json").is_file())
        self.assertTrue((Path(state["evidence_directory"]) / "stdout.txt").is_file())
        self.assertTrue((Path(state["evidence_directory"]) / "stderr.txt").is_file())
        self.assertTrue(state["worker_completed"])
        self.assertTrue(state["operation_ok"])
        self.assertFalse(state["design_accepted"])
        failed = self.manager.start("inspect_bundle", {"project": str(self.root / "absent.kicad_pro")})
        until(lambda: self.manager.status(failed["job_id"])["status"] not in {"queued", "running"})
        failure = self.manager.status(failed["job_id"])
        self.assertEqual(failure["status"], "failed")
        self.assertTrue(failure["error"])
        self.assertTrue((Path(failure["evidence_directory"]) / "stderr.txt").is_file())
        self.assertFalse(failure["worker_completed"])
        self.assertTrue((Path(failure["evidence_directory"]) / "result.json").is_file())
        self.assertFalse(self.manager.cancel(jid)["cancel_requested"])

    @requires_boardtooling
    def test_stale_request_checked_at_execution_and_typed_dispatch(self):
        source = self.root / "source"
        source.mkdir()
        project = source / "stale.kicad_pro"
        project.write_text("{}")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)")
        rules = self.root / "rules.json"
        rules.write_text("{}")
        server = FastMCP("job-fixture")
        register_workflow_tools(server, self.manager)
        schemas = {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}
        self.assertIn("expected_bundle_digest", schemas["start_board_gate_job"]["required"])
        self.assertNotIn("evidence_dir", schemas["start_board_gate_job"]["properties"])
        self.assertIn("attestation_paths", schemas["start_board_contract_evaluation_job"]["properties"])
        self.assertIn("attestation_paths", schemas["start_board_release_preview_job"]["required"])
        self.assertNotIn("trust_config", schemas["start_board_release_preview_job"]["properties"])
        _, started = asyncio.run(server.call_tool("start_board_gate_job", {
            "project_path": str(project), "expected_bundle_digest": "a" * 64,
            "variant": "V1", "rules_path": str(rules)}))
        jid = started["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "completed", state)
        self.assertTrue(state["worker_completed"])
        self.assertFalse(state["operation_ok"])
        self.assertFalse(state["design_accepted"])
        self.assertIn("Expected base mismatch", state["result"]["error"])
        evidence = Path(state["evidence_directory"]) / "gate-evidence"
        self.assertTrue((evidence / "adapter-run.json").is_file())
        self.assertEqual(json.loads((Path(state["evidence_directory"]) / "request.json").read_text())["arguments"]["expected_digest"], "a" * 64)

    @requires_boardtooling
    def test_typed_async_attestation_paths_reach_actual_contract_adapter(self):
        source = self.root / "signed-source"
        source.mkdir()
        project = source / "signed.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        gate = self.root / "external-gate.json"
        gate.write_text("{}", encoding="utf-8")
        signed = self.root / "external-signed.json"
        signed.write_text("{}", encoding="utf-8")
        digest = bt.inspect_bundle(str(project))["digest"]
        server = FastMCP("signed-job-fixture")
        register_workflow_tools(server, self.manager)
        _, response = asyncio.run(server.call_tool("start_board_contract_evaluation_job", {
            "project_path": str(project), "expected_bundle_digest": digest,
            "gate_report_path": str(gate), "attestation_paths": [str(signed)]}))
        jid = response["job_id"]
        until(lambda: self.manager.status(jid)["status"] not in {"queued", "running"})
        state = self.manager.status(jid)
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["result"]["evaluation"]["contract_state"], "legacy")
        self.assertEqual(state["result"]["evidence_selection"]["attestations"][str(signed)],
                         bt._digest(signed))
        self.assertEqual(json.loads((Path(state["evidence_directory"]) / "request.json").read_text())[
            "arguments"]["attestation_paths"], [str(signed)])
        _, preview_response = asyncio.run(server.call_tool("start_board_release_preview_job", {
            "project_path": str(project), "expected_bundle_digest": digest,
            "variant": "V1", "fab": "generic", "gate_report_path": str(gate),
            "attestation_paths": [str(signed)]}))
        preview_id = preview_response["job_id"]
        until(lambda: self.manager.status(preview_id)["status"] not in {"queued", "running"})
        preview_state = self.manager.status(preview_id)
        self.assertEqual(preview_state["status"], "completed", preview_state)
        self.assertFalse(preview_state["operation_ok"])
        self.assertEqual(preview_state["result"]["request"]["attestation_paths"], [str(signed)])
        self.assertTrue((Path(preview_state["evidence_directory"]) / "release-preview" /
                         "adapter-status.json").is_file())

    def test_failed_launch_retains_complete_journal(self):
        actual_tree = self.manager.native.Tree
        class BrokenTree(actual_tree):
            def spawn(self, argv, **kwargs):
                raise OSError("fixture launch refused")
        self.manager.native.Tree = BrokenTree
        started = self.manager.start("inspect_bundle", {"project": "fixture"})
        until(lambda: self.manager.status(started["job_id"])["status"] not in {"queued", "running"})
        state = self.manager.status(started["job_id"])
        self.assertEqual(state["status"], "failed")
        self.assertIn("fixture launch refused", state["error"])
        self.assertIsNone(state["result"])
        folder = Path(state["evidence_directory"])
        for name in ("request.json", "result.json", "state.json", "stdout.txt", "stderr.txt"):
            self.assertTrue((folder / name).is_file(), name)

    @requires_boardtooling
    def test_async_placement_argument_mapping_reaches_shared_contract(self):
        source = self.root / "source"
        source.mkdir()
        project = source / "placement.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        digest = bt.inspect_bundle(str(project))["digest"]
        started = self.manager.start("plan_placement", {
            "project": str(project), "expected_digest": digest,
            "contract_sha256": "b" * 64, "variant": "V1",
            "operation": "plan_placement", "moves": [{
                "footprint_uuid": "12345678-1234-4234-8234-123456789abc",
                "dx_mm": 0.1, "dy_mm": 0.2}]})
        until(lambda: self.manager.status(started["job_id"])["status"] not in {"queued", "running"})
        state = self.manager.status(started["job_id"])
        self.assertEqual(state["status"], "failed", state)
        self.assertIn("selected board contract", state["error"].lower())
        self.assertNotIn("unexpected keyword", state["error"])

    @requires_boardtooling
    def test_async_draft_is_source_bound_and_remains_unapproved(self):
        source = self.root / "draft-source"
        source.mkdir()
        project = source / "draft.kicad_pro"
        project.write_text(json.dumps({"schematic": {"variants": ["V1"]}}), encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        for name, content in {"draft.kicad_dru": "(rules)",
                              "house-audit-config.json": "{}",
                              "draft.rule-slice.json": "{}"}.items():
            (source / name).write_text(content, encoding="utf-8")
        expected = bt.inspect_bundle(str(project))["digest"]
        started = self.manager.start("create_contract_draft", {
            "project": str(project), "expected_digest": expected,
            "profile": "general_house_baseline", "variant": "V1"})
        until(lambda: self.manager.status(started["job_id"])["status"] not in {"queued", "running"})
        state = self.manager.status(started["job_id"])
        self.assertEqual(state["status"], "completed", state)
        self.assertTrue(state["operation_ok"])
        self.assertFalse(state["design_accepted"])
        self.assertEqual(state["result"]["state"], "draft")
        self.assertEqual(state["result"]["previous_bundle_digest"], expected)
        self.assertTrue((source / "board-contract.json").is_file())

    def test_cancel_stops_waiting_descendant_without_waiting_for_eof(self):
        pid_file = self.root / "descendant.json"
        actual_tree = self.manager.native.Tree
        code = ("import subprocess,sys,pathlib,json,threading; "
                "p=subprocess.Popen([sys.executable,'-c','import threading;threading.Event().wait()'],"
                "creationflags=subprocess.CREATE_NO_WINDOW); "
                "pathlib.Path(" + repr(str(pid_file)) + ").write_text(json.dumps({'pid':p.pid})); "
                "print('partial fixture output',flush=True);threading.Event().wait()")

        class FixtureTree(actual_tree):
            def spawn(self, argv, **kwargs):
                return super().spawn([sys.executable, "-c", code], **kwargs)

        self.manager.native.Tree = FixtureTree
        started = self.manager.start("inspect_bundle", {"project": "unused-fixture"})
        until(pid_file.is_file)
        pid = json.loads(pid_file.read_text())["pid"]
        expected = self.manager.native.identity(pid)
        self.assertIsNotNone(expected)
        other = WorkflowJobs(self.root / "journal")
        try:
            self.assertEqual(other.status(started["job_id"])["status"], "running")
            with self.assertRaisesRegex(ValueError, "Only this server"):
                other.cancel(started["job_id"])
            self.assertEqual(self.manager.native.identity(pid), expected)
        finally:
            other.close()
        self.manager.cancel(started["job_id"])
        until(lambda: self.manager.status(started["job_id"])["status"] not in {"queued", "running"})
        self.assertEqual(self.manager.status(started["job_id"])["status"], "cancelled", self.manager.status(started["job_id"]))
        self.assertNotEqual(self.manager.native.identity(pid), expected)
        state = self.manager.status(started["job_id"])
        self.assertTrue(state["owned_tree_stopped"])
        self.assertIn("partial fixture output", (Path(state["evidence_directory"]) / "stdout.txt").read_text())
        self.assertTrue((Path(state["evidence_directory"]) / "request.json").is_file())
        self.assertTrue((Path(state["evidence_directory"]) / "result.json").is_file())
        other = WorkflowJobs(self.root / "journal")
        try:
            cross = other.status(started["job_id"])
            self.assertEqual(cross["status"], "cancelled")
            self.assertFalse(other.cancel(started["job_id"])["cancel_requested"])
        finally:
            other.close()

    def test_server_shutdown_cancels_waiting_descendant(self):
        pid_file = self.root / "shutdown-descendant.json"
        actual_tree = self.manager.native.Tree
        code = ("import subprocess,sys,pathlib,json,threading; "
                "p=subprocess.Popen([sys.executable,'-c','import threading;threading.Event().wait()'],"
                "creationflags=subprocess.CREATE_NO_WINDOW); "
                "print('shutdown fixture output',flush=True); "  # output first, then the pid file
                "tmp=pathlib.Path(" + repr(str(pid_file) + ".tmp") + "); tmp.write_text(json.dumps({'pid':p.pid})); "
                "tmp.replace(" + repr(str(pid_file)) + "); threading.Event().wait()")  # atomic: never half-read
        class FixtureTree(actual_tree):
            def spawn(self, argv, **kwargs):
                return super().spawn([sys.executable, "-c", code], **kwargs)
        self.manager.native.Tree = FixtureTree
        started = self.manager.start("inspect_bundle", {"project": "shutdown-fixture"})
        until(pid_file.is_file)
        pid = json.loads(pid_file.read_text())["pid"]
        expected = self.manager.native.identity(pid)
        self.assertIsNotNone(expected)
        self.manager.close()
        state = self.manager.status(started["job_id"])
        self.assertEqual(state["status"], "cancelled", state)
        self.assertTrue(state["owned_tree_stopped"])
        self.assertNotEqual(self.manager.native.identity(pid), expected)
        self.assertTrue((Path(state["evidence_directory"]) / "result.json").is_file())
        self.assertIn("shutdown fixture output", (Path(state["evidence_directory"]) / "stdout.txt").read_text())


if __name__ == "__main__":
    unittest.main()
