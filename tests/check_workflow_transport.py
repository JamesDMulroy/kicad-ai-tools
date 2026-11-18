"""Real local MCP stdio smoke check; no owner board or GUI operation."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kicad_mcp.utils.winproc import Tree, identity  # noqa: E402
EVIDENCE = ROOT / ".tmp" / ("mcp-transport-" + uuid.uuid4().hex)
EVIDENCE.mkdir(parents=True)
GUI_PYTHON = Path(sys.executable).with_name("pythonw.exe")  # the interpreter running this check
env = dict(os.environ, KICAD_MCP_LOG_DIR=str(EVIDENCE / "server-logs"),
           KICAD_MCP_JOB_DIR=str(EVIDENCE / "jobs"))
responses = queue.Queue()
receipt = {"purpose": "initialize/list/call/EOF smoke; no board operation",
           "watchdog_basis": "180-second per-response test hang detector, not a product limit",
           "source_hashes": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
                             for f in ["silent_stdio.pyw", "workflow_server.py", "kicad_mcp/__init__.py", "kicad_mcp/tools/workflow_tools.py", "kicad_mcp/utils/boardtooling.py", "kicad_mcp/utils/secure_subprocess.py", "kicad_mcp/utils/workflow_jobs.py", "kicad_mcp/workflow_job_worker.py", "tests/check_workflow_transport.py"]}}
atexit.register(lambda: (EVIDENCE / "receipt.json").write_text(
    json.dumps(receipt, indent=2, default=str), encoding="utf-8"))


def pe_subsystem(executable):
    """PE optional-header subsystem: 2=GUI, 3=console."""
    data = Path(executable).read_bytes()
    if data[:2] != b"MZ":
        raise AssertionError("Outer launcher is not a PE executable")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise AssertionError("Outer launcher has no PE header")
    return struct.unpack_from("<H", data, offset + 24 + 68)[0]


receipt["outer_executable"] = str(GUI_PYTHON)
receipt["outer_subsystem"] = pe_subsystem(GUI_PYTHON)
assert receipt["outer_subsystem"] == 2, "Outer launcher must be GUI subsystem"
old_pythonw = ROOT / ".venv310" / "Scripts" / "pythonw.exe"
if old_pythonw.is_file():
    receipt["old_venv_subsystem"] = pe_subsystem(old_pythonw)
    assert old_pythonw.resolve() != GUI_PYTHON.resolve()


def read_stdout(stream, log):
    for line in stream:
        log.write(line)
        log.flush()
        try:
            responses.put(json.loads(line))
        except ValueError:
            responses.put({"protocol_error": line})
    responses.put({"eof": True})


def send(process, request):
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def reply(number):
    deadline = time.monotonic() + 180
    while True:
        response = responses.get(timeout=max(0, deadline - time.monotonic()))
        if response.get("id") == number:
            return response
        if "protocol_error" in response or response.get("eof"):
            raise AssertionError(response)


def data_from(response):
    result = response["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def durable_state(folder):
    """Read the last complete canonical transition during concurrent writes."""
    lines = (folder / "state-history.jsonl").read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("No complete workflow state transition")


def running_native(job_id):
    """Observe this journal's exact running worker and native command identities."""
    folder = EVIDENCE / "jobs" / job_id
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state_path = folder / "state-history.jsonl"
        if state_path.is_file():
            try:
                state = durable_state(folder)
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if state["status"] not in {"queued", "running"}:
                raise AssertionError({"job_completed_before_native_sync": state})
            worker = state.get("process")
            commands = folder / "native-commands"
            if worker and commands.is_dir() and identity(worker["pid"]) == worker:
                for command in commands.iterdir():
                    events_file = command / "events.jsonl"
                    if not events_file.is_file():
                        continue
                    try:
                        events = [json.loads(line) for line in events_file.read_text().splitlines()]
                    except (OSError, ValueError):
                        continue
                    native = next((item for item in events if item["event"] == "running"), None)
                    if native and identity(native["pid"]) is not None:
                        return {"worker": worker, "native": identity(native["pid"]),
                                "evidence": str(command)}
        time.sleep(0.02)
    raise AssertionError("Native command did not reach running synchronization point")


def stopped_native(observed):
    assert identity(observed["worker"]["pid"]) != observed["worker"]
    assert identity(observed["native"]["pid"]) != observed["native"]
    evidence = Path(observed["evidence"])
    assert (evidence / "command.json").is_file()
    assert (evidence / "events.jsonl").is_file()
    events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "parent_owned_tree_stopped", events
    assert (evidence / "stdout.bin").is_file()
    assert (evidence / "stderr.bin").is_file()
    observed["stdout_bytes"] = (evidence / "stdout.bin").stat().st_size
    observed["stderr_bytes"] = (evidence / "stderr.bin").stat().st_size


# Verify the same GUI executable and inherited pipes before starting MCP.
with (EVIDENCE / "probe.stderr.txt").open("x", encoding="utf-8") as probe_errors, Tree() as probe_tree:
    probe = probe_tree.spawn([str(GUI_PYTHON), str(ROOT / "silent_stdio.pyw"), "--probe-stdio"],
                             cwd=str(ROOT), env=env, stdin=-1, stdout=-1,
                             stderr=probe_errors, text=True, encoding="utf-8")
    probe_identity = identity(probe.pid)
    probe_output, _ = probe.communicate(
        json.dumps({"probe": "pipe-roundtrip"}) + "\n", timeout=180)
    receipt["probe_stdout"] = probe_output
    receipt["probe_reply"] = json.loads(probe_output)
    receipt["probe_exit_code"] = probe.returncode
    probe_tree.close()
    receipt["probe_stopped"] = identity(probe.pid) != probe_identity
    assert receipt["probe_reply"]["echo"] == {"probe": "pipe-roundtrip"}
    assert receipt["probe_exit_code"] == 0 and receipt["probe_stopped"]

# A bad launch must exit nonzero and retain stderr without leaving its job.
with (EVIDENCE / "failure.stderr.txt").open("x", encoding="utf-8") as failure_errors, Tree() as failure_tree:
    failure = failure_tree.spawn([str(GUI_PYTHON), str(ROOT / "silent_stdio.pyw"), "--unknown"],
                                 cwd=str(ROOT), env=env, stdin=-1, stdout=-1,
                                 stderr=failure_errors, text=True, encoding="utf-8")
    receipt["failure_launch_identity"] = identity(failure.pid)
    failure_output, _ = failure.communicate(timeout=180)
    receipt["failure_stdout"] = failure_output
    receipt["failure_exit_code"] = failure.returncode
    failure_tree.close()
    receipt["failure_stopped"] = failure.poll() is not None and identity(failure.pid) is None
    assert receipt["failure_exit_code"] != 0 and receipt["failure_stopped"]


with (EVIDENCE / "stderr.txt").open("x", encoding="utf-8") as errors, \
     (EVIDENCE / "stdout.jsonl").open("x", encoding="utf-8") as output, Tree() as tree:
    process = tree.spawn([str(GUI_PYTHON),
                          str(ROOT / "silent_stdio.pyw")],
                         cwd=str(ROOT), env=env, stdin=-1, stdout=-1, stderr=errors,
                         text=True, encoding="utf-8")
    launched = identity(process.pid)
    receipt["launcher"] = launched
    reader = threading.Thread(target=read_stdout, args=(process.stdout, output))
    reader.start()
    try:
        send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "kicad-task-verifier", "version": "1"}}})
        receipt["initialize"] = reply(1)
        assert "result" in receipt["initialize"], receipt["initialize"]
        send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = reply(2)
        receipt["tool_names"] = [t["name"] for t in listing["result"]["tools"]]
        receipt["tool_schemas"] = {t["name"]: t["inputSchema"] for t in listing["result"]["tools"]}
        assert set(receipt["tool_names"]) == {
            "boardtooling_capabilities", "inspect_board_bundle", "evaluate_board_gates",
            "query_board_scene", "board_release_preview", "board_release_finalize",
            "native_board_interaction_capability", "board_design_profiles",
            "create_board_contract_draft", "evaluate_board_contract",
            "plan_board_placement", "start_board_bundle_job",
            "start_board_contract_draft_job", "start_board_contract_evaluation_job",
            "start_board_placement_plan_job", "start_board_gate_job",
            "start_board_scene_job", "start_board_release_preview_job",
            "start_board_release_finalize_job", "board_workflow_job_status",
            "cancel_board_workflow_job"}
        assert "profile" in receipt["tool_schemas"]["create_board_contract_draft"]["required"]
        assert "moves" in receipt["tool_schemas"]["plan_board_placement"]["required"]
        assert "expected_bundle_digest" in receipt["tool_schemas"]["start_board_gate_job"]["required"]
        assert "evidence_dir" not in receipt["tool_schemas"]["start_board_gate_job"]["properties"]
        assert "attestation_paths" in receipt["tool_schemas"]["evaluate_board_contract"]["properties"]
        assert "attestation_paths" in receipt["tool_schemas"]["board_release_preview"]["required"]
        assert "attestation_paths" in receipt["tool_schemas"]["start_board_release_preview_job"]["required"]
        assert "trust_config" not in receipt["tool_schemas"]["board_release_preview"]["properties"]
        send(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "boardtooling_capabilities", "arguments": {}}})
        receipt["capabilities"] = reply(3)
        assert not receipt["capabilities"].get("result", {}).get("isError"), receipt["capabilities"]
        capability_result = receipt["capabilities"]["result"]
        capability_data = capability_result.get("structuredContent")
        if capability_data is None:
            capability_data = json.loads(capability_result["content"][0]["text"])
        assert capability_data["expert_design_contract"]["api_available"] is True
        assert capability_data["expert_design_contract"]["independent_acceptance_supported"] is False
        assert capability_data["design_signoff"]["supported"] is False
        assert capability_data["native_interactive_edge_shove"]["supported"] is False
        send(process, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "inspect_board_bundle", "arguments": {}}})
        receipt["missing_project"] = reply(4)
        assert (receipt["missing_project"].get("error") or
                receipt["missing_project"].get("result", {}).get("isError")), receipt["missing_project"]
        send(process, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "native_board_interaction_capability",
                                  "arguments": {"operation": "adaptive_edge_shrink"}}})
        receipt["native_unavailable"] = reply(5)
        result = receipt["native_unavailable"]["result"]
        data = result.get("structuredContent")
        if data is None:
            data = json.loads(result["content"][0]["text"])
        assert data["supported"] is False, data
        send(process, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "board_design_profiles", "arguments": {}}})
        receipt["profiles"] = reply(6)
        profile_result = receipt["profiles"]["result"]
        profile_data = profile_result.get("structuredContent")
        if profile_data is None:
            profile_data = json.loads(profile_result["content"][0]["text"])
        assert "profiles" in profile_data, profile_data
        send(process, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "create_board_contract_draft", "arguments": {}}})
        receipt["missing_draft_arguments"] = reply(7)
        assert (receipt["missing_draft_arguments"].get("error") or
                receipt["missing_draft_arguments"].get("result", {}).get("isError"))
        fixture = EVIDENCE / "fixture"
        fixture.mkdir()
        project = fixture / "transport.kicad_pro"
        project.write_text("{}", encoding="utf-8")
        project.with_suffix(".kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        send(process, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "start_board_bundle_job",
                                  "arguments": {"project_path": str(project)}}})
        started = reply(8)["result"]
        start_data = started.get("structuredContent")
        if start_data is None:
            start_data = json.loads(started["content"][0]["text"])
        job_id = start_data["job_id"]
        receipt["async_job_id"] = job_id
        deadline = time.monotonic() + 180
        request_id = 9
        while True:
            send(process, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                           "params": {"name": "board_workflow_job_status",
                                      "arguments": {"job_id": job_id}}})
            reply_data = reply(request_id)["result"]
            state = reply_data.get("structuredContent")
            if state is None:
                state = json.loads(reply_data["content"][0]["text"])
            if state["status"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Async bundle fixture exceeded test synchronization watchdog")
            request_id += 1
            time.sleep(0.1)
        receipt["async_job_status"] = state
        assert state["status"] == "completed", state
        assert state["worker_completed"] is True and state["operation_ok"] is True
        assert state["design_accepted"] is False
        assert state["result"]["project"] == project.name
        assert (EVIDENCE / "jobs" / job_id / "request.json").is_file()
        assert (EVIDENCE / "jobs" / job_id / "result.json").is_file()
        send(process, {"jsonrpc": "2.0", "id": request_id + 1, "method": "tools/call",
                       "params": {"name": "cancel_board_workflow_job",
                                  "arguments": {"job_id": job_id}}})
        cancel_result = reply(request_id + 1)["result"]
        cancel_data = cancel_result.get("structuredContent")
        if cancel_data is None:
            cancel_data = json.loads(cancel_result["content"][0]["text"])
        receipt["completed_job_cancel"] = cancel_data
        assert cancel_data["cancel_requested"] is False
        send(process, {"jsonrpc": "2.0", "id": request_id + 2, "method": "tools/call",
                       "params": {"name": "start_board_scene_job", "arguments": {
                           "project_path": str(project),
                           "expected_bundle_digest": state["result"]["digest"],
                           "bbox_mm": [0, 0, 1, 1]}}})
        cancel_scene = data_from(reply(request_id + 2))["job_id"]
        receipt["cancel_scene_running"] = running_native(cancel_scene)
        send(process, {"jsonrpc": "2.0", "id": request_id + 3, "method": "tools/call",
                       "params": {"name": "cancel_board_workflow_job",
                                  "arguments": {"job_id": cancel_scene}}})
        receipt["running_cancel_reply"] = data_from(reply(request_id + 3))
        assert receipt["running_cancel_reply"]["cancel_requested"] is True
        cancel_folder = EVIDENCE / "jobs" / cancel_scene
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                cancelled = durable_state(cancel_folder)
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if cancelled["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        receipt["running_cancel_state"] = cancelled
        assert cancelled["status"] == "cancelled" and cancelled["owned_tree_stopped"] is True
        stopped_native(receipt["cancel_scene_running"])
        send(process, {"jsonrpc": "2.0", "id": request_id + 4, "method": "tools/call",
                       "params": {"name": "start_board_scene_job", "arguments": {
                           "project_path": str(project),
                           "expected_bundle_digest": state["result"]["digest"],
                           "bbox_mm": [0, 0, 1, 1]}}})
        receipt["eof_scene_job_id"] = data_from(reply(request_id + 4))["job_id"]
        receipt["eof_scene_running"] = running_native(receipt["eof_scene_job_id"])
        process.stdin.close()
        receipt["exit_code"] = process.wait(timeout=180)
        assert receipt["exit_code"] == 0
        eof_state = durable_state(EVIDENCE / "jobs" / receipt["eof_scene_job_id"])
        receipt["eof_scene_state"] = eof_state
        assert eof_state["status"] == "cancelled", eof_state
        assert eof_state["owned_tree_stopped"] is True
        stopped_native(receipt["eof_scene_running"])
        receipt["passed"] = True
    finally:
        tree.close()
        process.wait()
        reader.join()
        receipt["launcher_stopped"] = identity(process.pid) != launched
        (EVIDENCE / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps({"evidence": str(EVIDENCE), "passed": receipt.get("passed", False),
                          "launcher_stopped": receipt["launcher_stopped"]}), flush=True)
