"""Real FastMCP dispatch and contained-process regression tests."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mcp.server.fastmcp import FastMCP
import kicad_mcp
from kicad_mcp.tools.workflow_tools import register_workflow_tools
from kicad_mcp.utils import boardtooling as bt
from kicad_mcp.utils import secure_subprocess as owned

if not bt.SCRIPTS.is_dir():  # BoardTooling is an optional, separately installed tool
    raise unittest.SkipTest(f"BoardTooling is not configured: {bt.SCRIPTS}")

DIGEST = "a" * 64
CONTRACT = {"bundle_digest": DIGEST, "variant": "V1", "contract_sha256": "c" * 64}
KEY_DIGEST = "d" * 64
SELECTION = {"trust_config_path": "C:/fixture/host-trust.json",
             "trust_config_sha256": "e" * 64,
             "gate_report_path": "C:/fixture/exits.json",
             "gate_report_sha256": "f" * 64,
             "attestations": {"C:/fixture/signed.json": "1" * 64}}



class _HiddenHelpers:
    """Windowless process helpers for the tests (CREATE_NO_WINDOW; not contained in any job)."""
    FLAGS = 0x08000000 if os.name == "nt" else 0

    @classmethod
    def run_hidden(cls, argv, **kwargs):
        return subprocess.run(argv, creationflags=cls.FLAGS, stdin=subprocess.DEVNULL, **kwargs)

    @classmethod
    def popen_hidden(cls, argv, **kwargs):
        return subprocess.Popen(argv, creationflags=cls.FLAGS, stdin=subprocess.DEVNULL, **kwargs)

class FakeBundle:
    def verify_snapshot(self, project, expected, **kwargs):
        if expected != DIGEST:
            raise bt.BoardToolingError("stale source")
        return {"project": Path(project).name, "digest": DIGEST}

    def snapshot(self, project, **kwargs):
        return {"project": Path(project).name, "digest": DIGEST}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / ".tmp"
        scratch.mkdir(exist_ok=True)
        self.addCleanup(lambda: scratch.rmdir() if not any(scratch.iterdir()) else None)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "source" / "board.kicad_pro"
        self.project.parent.mkdir()
        self.project.write_text("{}", encoding="utf-8")
        self.board = self.project.with_suffix(".kicad_pcb")
        self.board.write_text("(kicad_pcb)", encoding="utf-8")
        self.project.with_suffix(".kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        self.rules = self.root / "rules.json"
        self.rules.write_text("{}", encoding="utf-8")
        self.mcp = FastMCP("boardtooling-test")
        register_workflow_tools(self.mcp)
        bundle_patch = patch.object(bt, "_bundle", return_value=FakeBundle())
        bundle_patch.start()
        self.addCleanup(bundle_patch.stop)

    def call(self, name, **arguments):
        content, structured = asyncio.run(self.mcp.call_tool(name, arguments))
        self.assertTrue(content)
        return structured

    def accepted_evidence_fixture(self):
        """Disposable complete signed evidence using the maintained contract API."""
        dc = bt._contract()
        bundle = bt._module("test_real_bundle", bt.SCRIPTS / "board_bundle.py")
        exporter = bt._exporter()
        self.project.write_text(json.dumps({"schematic": {"variants": [{"name": "Built"}]}}), encoding="utf-8")
        self.board.write_text('(kicad_pcb (version 20240108) (footprint "R" '
                              '(uuid "11111111-1111-4111-8111-111111111111")))', encoding="utf-8")
        self.project.with_suffix(".kicad_dru").write_text("(version 1)", encoding="utf-8")
        (self.project.parent / "house-audit-config.json").write_text("{}", encoding="utf-8")
        self.project.with_suffix(".rule-slice.json").write_text("{}", encoding="utf-8")
        notes = self.project.parent / "FAB-NOTES.md"
        notes.write_text("Disposable reviewed notes", encoding="utf-8")
        contract = dc.author_draft(self.project, "general_house_baseline", "Built")
        for name in dc.COMMON_INPUTS:
            item = self.project.parent / (name + ".txt")
            item.write_text("disposable " + name, encoding="utf-8")
            contract["inputs"][name] = {"path": item.name, "sha256": bundle.sha256(item),
                                        "kind": dc.INPUT_KIND_BY_NAME[name]}
        outline = self.project.parent / "outline.txt"
        outline.write_text("disposable outline", encoding="utf-8")
        contract["mechanical"]["outline_artifact"] = outline.name
        contract["source_artifacts"][outline.name] = bundle.sha256(outline)
        applicability = self.project.parent / "bga-applicability.pdf"
        applicability.write_bytes(b"%PDF-disposable-measured-absence")
        contract["applicability"]["bga_escape"] = {
            "status": "inapplicable", "basis": "measured no BGA", "artifact": applicability.name}
        contract["fabrication"] = {"fabricator": "fixture fab", "service": "fixture service",
                                   "stackup_revision": "A", "ipc_revision": "fixture IPC",
                                   "ipc_class": 2, "assembly_process": "fixture assembly"}
        contract["state"] = "expert_approved"
        (self.project.parent / "board-contract.json").write_text(json.dumps(contract), encoding="utf-8")
        signer = Ed25519PrivateKey.generate()
        public = self.root / "authority.pub.pem"
        public.write_bytes(signer.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        trust = self.root / "host-trust.json"
        trust.write_text(json.dumps({"schema_version": 1, "keys": {"test-key": {
            "public_key_path": str(public), "sha256": bundle.sha256(public),
            "roles": ["expert", "native_evaluator", "fabricator", "simulator", "engineer", "procurement"],
            "purposes": sorted({purpose for authorities in dc.RULE_AUTHORITIES.values()
                                for _, purpose in authorities} | {"applicability_measurement"}),
            "rules": list(dc.RULE_AUTHORITIES), "revoked": False}},
            "revoked_key_ids": []}), encoding="utf-8")
        evidence = self.root / "signed-evidence"
        evidence.mkdir()
        artifact = evidence / "evaluation.txt"
        artifact.write_text("disposable evaluator output", encoding="utf-8")
        gate = evidence / "exits.json"
        gate.write_text(json.dumps({"bundle": bundle.snapshot(self.project), "variant": "Built",
            "rules_digest": bundle.sha256(self.project.with_suffix(".rule-slice.json")),
            "toolchain_digest": bundle.sha256(exporter.CLI), "untrustworthy": [], "failed": [],
            "applicability_skips": [], "exits": {name: 0 for name in exporter.REQUIRED_GATES},
            "rule_coverage": {"DRC.ZERO": {"status": "required", "gates": ["drc"]}}}), encoding="utf-8")
        receipts = []
        def sign(rule, role, purpose, attachments, status="pass"):
            expert = rule == "contract_lock" and purpose == "approve_specification"
            payload = {"schema_version": 1, "key_id": "test-key", "role": role,
                "purpose": purpose, "rule": rule, "status": status,
                "contract_id": contract["contract_id"], "revision": contract["revision"],
                "specification_digest": dc.specification_digest(contract),
                "contract_sha256": None if expert else dc.digest(contract),
                "source_artifacts": contract["source_artifacts"],
                "bundle_digest": None if expert else bundle.snapshot(self.project)["digest"],
                "variant": "Built", "artifacts": attachments, "findings": []}
            signed = {"payload": payload, "signature": {"algorithm": "ed25519",
                "value": base64.b64encode(signer.sign(dc.canonical(payload))).decode("ascii")}}
            path = evidence / (rule + "-" + role + "-" + purpose + ".json")
            path.write_text(json.dumps(signed), encoding="utf-8")
            receipts.append(path)
        def attachment(path, root="attestation"):
            return {"root": root, "path": path.name, "sha256": bundle.sha256(path)}
        for rule in dc.catalog()["profiles"]["general_house_baseline"]:
            if rule == "bga_escape":
                sign(rule, "native_evaluator", "applicability_measurement",
                     [attachment(applicability, "project")], "inapplicable")
                continue
            for role, purpose in dc.RULE_AUTHORITIES[rule]:
                attachments = [] if role == "expert" else [attachment(gate if rule == "drc_parity" else artifact)]
                sign(rule, role, purpose, attachments)
        reviewer = self.root / "reviewer.pub.pem"
        reviewer.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        return {"bundle": bundle, "contract": dc, "exporter": exporter, "project": self.project,
                "trust": trust, "gate": gate, "attestations": receipts, "notes": notes,
                "reviewer": reviewer, "evidence": evidence}

    def test_real_fastmcp_schema_dispatch_and_structural_claim(self):
        self.assertEqual(kicad_mcp.__version__, "0.1.0")
        schemas = {tool.name: tool.inputSchema for tool in asyncio.run(self.mcp.list_tools())}
        self.assertIn("expected_bundle_digest", schemas["evaluate_board_gates"]["required"])
        self.assertIn("evidence_dir", schemas["board_release_finalize"]["required"])
        self.assertIn("contract_sha256", schemas["plan_board_placement"]["required"])
        self.assertIn("moves", schemas["plan_board_placement"]["required"])
        self.assertIn("profile", schemas["create_board_contract_draft"]["required"])
        self.assertIn("attestation_paths", schemas["evaluate_board_contract"]["properties"])
        self.assertIn("attestation_paths", schemas["board_release_preview"]["required"])
        self.assertIn("gate_report_path", schemas["board_release_preview"]["required"])
        self.assertIn("profiles", self.call("board_design_profiles"))
        self.assertFalse(self.call("native_board_interaction_capability",
                                   operation="canvas_mouse_tracking")["supported"])
        self.assertIn("in_editor", self.call("native_board_interaction_capability",
                                             operation="footprint_drag"))
        from kicad_mcp.tools.analysis_tools import register_analysis_tools
        analysis = FastMCP("structural-test")
        register_analysis_tools(analysis)
        _, result = asyncio.run(analysis.call_tool(
            "validate_project", {"project_path": str(self.project)}))
        result = result["result"]
        self.assertTrue(result["valid"])
        self.assertEqual(result["scope"], "structural_only")
        self.assertFalse(result["design_signoff"])

    def test_stale_source_rejected_before_gate_dispatch(self):
        with patch.object(bt, "_run") as run:
            result = self.call("evaluate_board_gates", project_path=str(self.project),
                               expected_bundle_digest="b" * 64, variant="V1",
                               rules_path=str(self.rules), evidence_dir=str(self.root / "evidence"))
            run.assert_not_called()
        self.assertIn("stale source", result["error"])
        self.assertIn("stale source", json.loads((self.root / "evidence" / "adapter-run.json").read_text())["launch_error"])

    def test_failed_native_command_retains_output(self):
        failed = {"argv": ["native"], "exit_code": 9,
                  "stdout": "native out", "stderr": "native failure"}
        with patch.object(bt, "_run", return_value=failed), patch.object(
                bt, "_digest", return_value="c" * 64), patch.object(
                bt.Path, "is_file", autospec=True, side_effect=lambda p: True):
            result = self.call("query_board_scene", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, bbox_mm=[0, 0, 1, 1])
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 9)
        self.assertEqual(result["stderr"], "native failure")

    def test_missing_gate_evidence_rejected_with_logs(self):
        output = self.root / "evidence"
        with patch.object(bt, "_run", return_value={
            "argv": ["pwsh"], "exit_code": 0, "stdout": "done", "stderr": ""}):
            result = self.call("evaluate_board_gates", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1",
                               rules_path=str(self.rules), evidence_dir=str(output))
        self.assertFalse(result["ok"])
        self.assertIn("no exits.json", result["error"])
        self.assertEqual((output / "adapter.stdout.log").read_text(), "done")
        attempt = json.loads((output / "adapter-status.json").read_text())
        self.assertEqual(attempt["request"]["expected_bundle_digest"], DIGEST)
        self.assertEqual(attempt["request"]["variant"], "V1")

    def test_gate_launch_error_is_durable(self):
        output = self.root / "gate-launch"
        failed = {"argv": ["pwsh"], "exit_code": None, "stdout": "partial",
                  "stderr": "launch stderr", "launch_error": "job assignment failed"}
        with patch.object(bt, "_run", return_value=failed):
            result = self.call("evaluate_board_gates", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1",
                               rules_path=str(self.rules), evidence_dir=str(output))
        self.assertFalse(result["ok"])
        self.assertEqual(json.loads((output / "adapter-run.json").read_text())["launch_error"], "job assignment failed")
        self.assertEqual((output / "adapter.stdout.log").read_text(), "partial")
        self.assertEqual((output / "adapter.stderr.log").read_text(), "launch stderr")

    def test_real_shared_exporter_rejects_applicability_mismatch(self):
        exporter = bt._exporter()
        output = self.root / "evidence"

        def emit(argv, **kwargs):
            output.mkdir(exist_ok=True)
            (output / "exits.json").write_text(json.dumps({
                "board": str(self.board), "bundle": {"digest": DIGEST},
                "variant": "V1", "rules_slice": str(self.rules),
                "rules_digest": "r", "toolchain_digest": "t",
                "rule_coverage": {"rule": {"status": "required", "gates": ["drc"]}},
                "exits": {gate: 0 for gate in exporter.REQUIRED_GATES},
                "failed": [], "untrustworthy": [],
                "applicability_skips": [{"gate": "edge_plating",
                                         "reason": "board config has no edge_plating block"}],
            }), encoding="utf-8")
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}

        with patch.object(bt, "_run", side_effect=emit):
            result = self.call("evaluate_board_gates", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1",
                               rules_path=str(self.rules), evidence_dir=str(output))
        self.assertFalse(result["ok"])
        self.assertIn("applicability skip", result["error"])

    def test_measured_applicability_requires_its_native_evidence(self):
        exporter = bt._exporter()
        output = self.root / "measured-gates"

        def emit(argv, **kwargs):
            output.mkdir(exist_ok=True)
            (output / "exits.json").write_text(json.dumps({
                "board": str(self.board), "bundle": {"digest": DIGEST},
                "variant": "V1", "rules_slice": str(self.rules),
                "rules_digest": "r", "toolchain_digest": "t",
                "rule_coverage": {"rule": {"status": "required", "gates": ["drc"]}},
                "exits": {gate: 0 for gate in exporter.REQUIRED_GATES
                          if gate != "edge_plating"},
                "failed": [], "untrustworthy": [],
                "applicability_skips": [{
                    "gate": "edge_plating", "reason": "native absence measured",
                    "subject_count": 0, "bundle_digest": DIGEST,
                    "measurement": {"segments": 0, "declared_group": False}}],
            }), encoding="utf-8")
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": ""}

        with patch.object(bt, "_run", side_effect=emit):
            result = self.call("evaluate_board_gates", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1",
                               rules_path=str(self.rules), evidence_dir=str(output))
        self.assertFalse(result["ok"])
        self.assertIn("evidence is missing", result["error"])

    def test_finalization_failure_has_unique_attempt_evidence(self):
        preview = self.root / "preview"
        preview.mkdir()
        (preview / "release-inputs.json").write_text(json.dumps({
            "binding": {"bundle": {"digest": DIGEST}, "variant": "V1"}}))
        (preview / "mcp-preview-binding.json").write_text(json.dumps({
            "project_path": str(self.root / "different" / self.project.name),
            "bundle_digest": DIGEST, "variant": "V1", "fab": "generic",
            "review_public_key_digest": KEY_DIGEST,
            "contract_sha256": CONTRACT["contract_sha256"]}))
        evidence = self.root / "attempt"
        with patch.object(bt, "_run") as run, patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_review_key", return_value=(self.root / "review.pub", KEY_DIGEST)):
            result = self.call("board_release_finalize", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                               preview_dir=str(preview),
                               visual_review=str(self.root / "review.json"),
                               evidence_dir=str(evidence))
            run.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("different exact project", result["error"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual((evidence / "adapter.stdout.log").read_text(), "")
        self.assertTrue((evidence / "adapter-status.json").is_file())
        self.assertEqual(json.loads((evidence / "adapter-status.json").read_text())["request"]["fab"], "generic")
        with self.assertRaisesRegex(Exception, "new unique"):
            self.call("board_release_finalize", project_path=str(self.project),
                      expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                      preview_dir=str(preview), visual_review=str(self.root / "review.json"),
                      evidence_dir=str(evidence))

    def test_finalization_exporter_failure_retains_full_attempt(self):
        preview = self.root / "preview"
        preview.mkdir()
        (preview / "release-inputs.json").write_text(json.dumps({
            "binding": {"bundle": {"digest": DIGEST}, "variant": "V1",
                        "review_public_key_digest": KEY_DIGEST,
                        "design_contract": {"identity": CONTRACT, **SELECTION}}}))
        (preview / "mcp-preview-binding.json").write_text(json.dumps({
            "project_path": str(self.project), "bundle_digest": DIGEST,
            "variant": "V1", "fab": "generic",
            "review_public_key_digest": KEY_DIGEST,
            "contract_sha256": CONTRACT["contract_sha256"],
            "evidence_selection": SELECTION}))
        evidence = self.root / "failed-finalize"
        failed = {"argv": ["exporter"], "exit_code": 7,
                  "stdout": "exporter output", "stderr": "exporter failure"}
        with patch.object(bt, "_run", return_value=failed), patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_same_evidence", return_value=True), patch.object(bt, "_review_key", return_value=(self.root / "review.pub", KEY_DIGEST)):
            result = self.call("board_release_finalize", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                               preview_dir=str(preview),
                               visual_review=str(self.root / "review.json"),
                               evidence_dir=str(evidence))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual((evidence / "adapter.stdout.log").read_text(), "exporter output")
        self.assertEqual((evidence / "adapter.stderr.log").read_text(), "exporter failure")
        self.assertEqual(json.loads((evidence / "adapter-run.json").read_text())[
            "exit_code"], 7)

    def test_release_requires_contract_and_configured_reviewer_key(self):
        trust = self.root / "host-trust.json"
        gate = self.root / "exits.json"
        signed = self.root / "signed.json"
        for path in (trust, gate, signed):
            path.write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {bt.TRUST_CONFIG_ENV: str(trust)}):
            legacy = self.call("board_release_preview", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                               output_dir=str(self.root / "legacy-preview"),
                               gate_report_path=str(gate), attestation_paths=[str(signed)])
        self.assertFalse(legacy["ok"])
        self.assertIn("design contract required", legacy["error"])
        with patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_evidence_selection", return_value=SELECTION), patch.dict(os.environ, {}, clear=True):
            missing_key = self.call("board_release_preview", project_path=str(self.project),
                                    expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                                    output_dir=str(self.root / "missing-key"),
                                    gate_report_path=SELECTION["gate_report_path"],
                                    attestation_paths=list(SELECTION["attestations"]))
        self.assertIn(bt.REVIEW_KEY_ENV, missing_key["error"])
        self.assertIn(bt.REVIEW_KEY_ENV, json.loads((self.root / "missing-key" / "adapter-run.json").read_text())["launch_error"])

    def test_configured_public_key_identity_and_project_local_rejection(self):
        key = self.project.parent / "review.pub"
        def write_public(hex_bytes):
            public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_bytes))
            key.write_bytes(public.public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo))
        write_public("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        with patch.dict(os.environ, {bt.REVIEW_KEY_ENV: str(key)}):
            first = bt._review_key()
            write_public("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511b")
            second = bt._review_key()
            self.assertNotEqual(first[1], second[1])
            with patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_evidence_selection", return_value=SELECTION):
                blocked = self.call("board_release_preview", project_path=str(self.project),
                                    expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                                    output_dir=str(self.root / "local-key"),
                                    gate_report_path=SELECTION["gate_report_path"],
                                    attestation_paths=list(SELECTION["attestations"]))
        self.assertFalse(blocked["ok"])
        self.assertIn("cannot be stored in project sources", blocked["error"])

    def test_preview_launch_failure_keeps_error_and_exporter_arguments(self):
        evidence = self.root / "failed-preview"
        key = self.root / "review.pub"
        failed = {"argv": [], "exit_code": None, "stdout": "partial preview",
                  "stderr": "launch diagnostics", "launch_error": "contained launch failed"}
        def reject(argv, **kwargs):
            self.assertEqual(Path(argv[0]), bt.EXPORT_PYTHON)
            self.assertEqual(Path(argv[1]), bt.SCRIPTS / "export_release.py")
            self.assertEqual(argv[argv.index("--visual-public-key") + 1], str(key))
            self.assertIn("--prepare", argv)
            self.assertFalse(Path(argv[argv.index("--out") + 1]).exists())
            return {**failed, "argv": argv}
        with patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_evidence_selection", return_value=SELECTION), patch.object(bt, "_review_key", return_value=(key, KEY_DIGEST)), patch.object(bt, "_run", side_effect=reject):
            result = self.call("board_release_preview", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                               output_dir=str(evidence),
                               gate_report_path=SELECTION["gate_report_path"],
                               attestation_paths=list(SELECTION["attestations"]))
        self.assertFalse(result["ok"])
        self.assertEqual(json.loads((evidence / "adapter-run.json").read_text())["launch_error"], "contained launch failed")
        self.assertEqual((evidence / "adapter.stdout.log").read_text(), "partial preview")
        self.assertEqual((evidence / "adapter.stderr.log").read_text(), "launch diagnostics")
        attempt = json.loads((evidence / "adapter-status.json").read_text())
        self.assertEqual(attempt["request"]["expected_bundle_digest"], DIGEST)
        self.assertEqual(attempt["request"]["fab"], "generic")

    def test_preview_rejects_reviewer_key_change_after_export(self):
        evidence = self.root / "changed-key"
        key = self.root / "review.pub"
        def emit(argv, **kwargs):
            preview = evidence / "preview"
            self.assertFalse(preview.exists())
            preview.mkdir()
            (preview / "release-inputs.json").write_text(json.dumps({
                "binding": {"bundle": {"digest": DIGEST}, "variant": "V1",
                            "review_public_key_digest": KEY_DIGEST,
                            "design_contract": {"identity": CONTRACT, **SELECTION}},
                "manifest": {}}), encoding="utf-8")
            return {"argv": argv, "exit_code": 0, "stdout": "preview output", "stderr": ""}
        with patch.object(bt, "_release_contract", return_value=(CONTRACT, SELECTION)), patch.object(bt, "_evidence_selection", return_value=SELECTION), patch.object(bt, "_same_evidence", return_value=True), patch.object(
                bt, "_review_key", side_effect=[(key, KEY_DIGEST), (key, "e" * 64)]), patch.object(
                bt, "_run", side_effect=emit):
            result = self.call("board_release_preview", project_path=str(self.project),
                               expected_bundle_digest=DIGEST, variant="V1", fab="generic",
                               output_dir=str(evidence),
                               gate_report_path=SELECTION["gate_report_path"],
                               attestation_paths=list(SELECTION["attestations"]))
        self.assertFalse(result["ok"])
        self.assertIn("key changed", result["error"])
        self.assertEqual(json.loads((evidence / "adapter-run.json").read_text())["exit_code"], 0)
        self.assertEqual((evidence / "adapter.stdout.log").read_text(), "preview output")

    def test_contract_evaluator_error_is_not_relabelled_as_stale(self):
        (self.project.parent / "board-contract.json").write_text("{malformed", encoding="utf-8")
        result = self.call("evaluate_board_contract", project_path=str(self.project),
                           expected_bundle_digest=DIGEST)
        self.assertEqual(result["evaluation"]["contract_state"], "error")
        self.assertIn("Expecting property name", result["evaluation"]["requirements"][0])

    def test_real_signed_contract_acceptance_and_authority_negatives(self):
        fixture = self.accepted_evidence_fixture()
        expected = fixture["bundle"].snapshot(self.project)["digest"]
        args = {"project_path": str(self.project), "expected_bundle_digest": expected,
                "gate_report_path": str(fixture["gate"]),
                "attestation_paths": [str(path) for path in fixture["attestations"]]}
        with patch.object(bt, "_bundle", return_value=fixture["bundle"]):
            with patch.dict(os.environ, {bt.TRUST_CONFIG_ENV: str(fixture["trust"])}):
                accepted = self.call("evaluate_board_contract", **args)
                self.assertTrue(accepted["evaluation"]["accepted"],
                                accepted["evaluation"].get("requirements"))
                self.assertEqual(accepted["evidence_selection"]["gate_report_sha256"],
                                 fixture["bundle"].sha256(fixture["gate"]))
                target = fixture["attestations"][0]
                original = target.read_bytes()
                forged = json.loads(original)
                forged["signature"]["value"] = base64.b64encode(b"\0" * 64).decode("ascii")
                target.write_text(json.dumps(forged), encoding="utf-8")
                self.assertFalse(self.call("evaluate_board_contract", **args)["evaluation"]["accepted"])
                target.write_bytes(original)
                trust_original = fixture["trust"].read_bytes()
                revoked = json.loads(trust_original)
                revoked["revoked_key_ids"] = ["test-key"]
                fixture["trust"].write_text(json.dumps(revoked), encoding="utf-8")
                self.assertFalse(self.call("evaluate_board_contract", **args)["evaluation"]["accepted"])
                fixture["trust"].write_bytes(trust_original)
                board_original = self.board.read_bytes()
                self.board.write_text("(kicad_pcb (changed source))", encoding="utf-8")
                with self.assertRaises(Exception):
                    self.call("evaluate_board_contract", **args)
                self.board.write_bytes(board_original)
            with patch.dict(os.environ, {}, clear=True):
                without_trust = self.call("evaluate_board_contract", **args)
            self.assertFalse(without_trust["evaluation"]["accepted"])

    def test_release_preview_and_finalize_pin_signed_evidence_and_host_trust(self):
        fixture = self.accepted_evidence_fixture()
        expected = fixture["bundle"].snapshot(self.project)["digest"]
        paths = [str(path) for path in fixture["attestations"]]
        preview_attempt = self.root / "release-attempt"
        env = {bt.TRUST_CONFIG_ENV: str(fixture["trust"]),
               bt.REVIEW_KEY_ENV: str(fixture["reviewer"])}
        def emit_preview(argv, **kwargs):
            self.assertEqual(argv.count("--design-attestation"), len(paths))
            self.assertEqual([argv[i + 1] for i, value in enumerate(argv)
                              if value == "--design-attestation"], paths)
            self.assertEqual(argv[argv.index("--design-gate-report") + 1], str(fixture["gate"]))
            self.assertEqual(argv[argv.index("--design-trust-config") + 1], str(fixture["trust"]))
            binding = fixture["exporter"].binding(
                self.project, self.project.with_suffix(".rule-slice.json"), fixture["notes"],
                "Built", fixture["reviewer"], fixture["trust"], paths, fixture["gate"])
            out = Path(argv[argv.index("--out") + 1])
            self.assertFalse(out.exists())
            out.mkdir()
            (out / "release-inputs.json").write_text(json.dumps({
                "binding": binding, "manifest": {}}), encoding="utf-8")
            return {"argv": argv, "exit_code": 0, "stdout": "preview fixture", "stderr": ""}
        with patch.object(bt, "_bundle", return_value=fixture["bundle"]), patch.dict(os.environ, env):
            with patch.object(bt, "_run", side_effect=emit_preview):
                preview = self.call("board_release_preview", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    output_dir=str(preview_attempt), gate_report_path=str(fixture["gate"]),
                    attestation_paths=paths, rules_path=str(self.project.with_suffix(".rule-slice.json")),
                    fab_notes=str(fixture["notes"]))
            self.assertTrue(preview["ok"], preview)
            self.assertEqual(preview["evidence_selection"]["trust_config_sha256"],
                             fixture["bundle"].sha256(fixture["trust"]))
            preview_dir = Path(preview["preview_dir"])
            binding = json.loads((preview_dir / "release-inputs.json").read_text())["binding"]
            (preview_dir / "release-receipt.json").write_text(json.dumps({
                "status": "verified", "binding": binding}), encoding="utf-8")
            def emit_finalize(argv, **kwargs):
                self.assertEqual(argv[argv.index("--finalize") + 1], str(preview_dir))
                return {"argv": argv, "exit_code": 0, "stdout": "finalize fixture", "stderr": ""}
            visual = self.root / "visual-review.json"
            visual.write_text("{}", encoding="utf-8")
            with patch.object(bt, "_run", side_effect=emit_finalize):
                final = self.call("board_release_finalize", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    preview_dir=str(preview_dir), visual_review=str(visual),
                    evidence_dir=str(self.root / "final-attempt"))
            self.assertTrue(final["ok"], final)
            target = fixture["attestations"][0]
            original = target.read_bytes()
            target.write_bytes(original + b" ")
            with patch.object(bt, "_run") as run:
                drift = self.call("board_release_finalize", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    preview_dir=str(preview_dir), visual_review=str(visual),
                    evidence_dir=str(self.root / "drift-attempt"))
                run.assert_not_called()
            self.assertFalse(drift["ok"])
            self.assertIn("evidence or host trust changed", drift["error"])
            target.write_bytes(original)
            trust_bytes = fixture["trust"].read_bytes()
            fixture["trust"].write_bytes(trust_bytes + b" ")
            with patch.object(bt, "_run") as run:
                trust_drift = self.call("board_release_finalize", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    preview_dir=str(preview_dir), visual_review=str(visual),
                    evidence_dir=str(self.root / "trust-drift-attempt"))
                run.assert_not_called()
            self.assertFalse(trust_drift["ok"])
            fixture["trust"].write_bytes(trust_bytes)
            gate_bytes = fixture["gate"].read_bytes()
            fixture["gate"].write_bytes(gate_bytes + b" ")
            with patch.object(bt, "_run") as run:
                gate_drift = self.call("board_release_finalize", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    preview_dir=str(preview_dir), visual_review=str(visual),
                    evidence_dir=str(self.root / "gate-drift-attempt"))
                run.assert_not_called()
            self.assertFalse(gate_drift["ok"])
            fixture["gate"].write_bytes(gate_bytes)
            pin_path = preview_dir / "mcp-preview-binding.json"
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            pin["evidence_selection"]["gate_report_path"] = str(fixture["evidence"] / "other.json")
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            with patch.object(bt, "_run") as run:
                substituted = self.call("board_release_finalize", project_path=str(self.project),
                    expected_bundle_digest=expected, variant="Built", fab="generic",
                    preview_dir=str(preview_dir), visual_review=str(visual),
                    evidence_dir=str(self.root / "substituted-attempt"))
                run.assert_not_called()
            self.assertFalse(substituted["ok"])

    def test_shared_exporter_real_argument_parser_requires_public_key(self):
        hidden = _HiddenHelpers
        self.project.write_text(json.dumps({"schematic": {"variants": ["V1"]}}), encoding="utf-8")
        argv = [str(bt.EXPORT_PYTHON), "-B", str(bt.SCRIPTS / "export_release.py"),
                str(self.project), "--variant", "V1", "--prepare",
                "--out", str(self.root / "parser-only")]
        absent = hidden.run_hidden(argv, cwd=str(Path.cwd()), capture_output=True,
                                   text=True, timeout=30)
        self.assertNotEqual(absent.returncode, 0)
        self.assertIn("--visual-public-key is required", absent.stderr)
        key = self.root / "review.pub"
        key.write_text("parser fixture", encoding="utf-8")
        supplied = hidden.run_hidden(argv + ["--visual-public-key", str(key)],
                                     cwd=str(Path.cwd()), capture_output=True,
                                     text=True, timeout=30)
        self.assertNotEqual(supplied.returncode, 0)
        self.assertIn("reviewed fabrication notes are required", supplied.stderr)
        self.assertNotIn("--visual-public-key is required", supplied.stderr)

    def test_shared_contract_draft_evaluation_and_blocked_native_plan(self):
        self.project.write_text(json.dumps({"schematic": {"variants": ["V1"]}}), encoding="utf-8")
        footprint_id = "12345678-1234-4234-8234-123456789abc"
        self.board.write_text(f'(kicad_pcb (footprint "fixture" (uuid "{footprint_id}")))', encoding="utf-8")
        for name, content in {
            "board.kicad_dru": "(rules)", "fp-lib-table": "(fp_lib_table)",
            "sym-lib-table": "(sym_lib_table)", "house-audit-config.json": "{}",
            "board.teardrops.json": "{}", "board.rule-slice.json": "{}",
        }.items():
            (self.project.parent / name).write_text(content, encoding="utf-8")
        real = bt._module("fixture_board_bundle", bt.SCRIPTS / "board_bundle.py")
        with patch.object(bt, "_bundle", return_value=real):
            before = self.call("inspect_board_bundle", project_path=str(self.project))
            draft = self.call("create_board_contract_draft", project_path=str(self.project),
                              expected_bundle_digest=before["digest"],
                              profile="general_house_baseline", variant="V1")
            self.assertEqual(draft["state"], "draft")
            self.assertFalse(draft["accepted"])
            self.assertNotEqual(draft["bundle"]["digest"], before["digest"])
            current = draft["bundle"]["digest"]
            evaluation = self.call("evaluate_board_contract", project_path=str(self.project),
                                   expected_bundle_digest=current)
            self.assertEqual(evaluation["evaluation"]["contract_state"], "draft")
            self.assertFalse(evaluation["evaluation"]["accepted"])
            with self.assertRaises(Exception):
                self.call("evaluate_board_contract", project_path=str(self.project),
                          expected_bundle_digest=before["digest"])
            contract_path = self.project.parent / "board-contract.json"
            contract = json.loads(contract_path.read_text())
            contract["movement"]["allowed_footprint_uuids"] = [footprint_id]
            contract["movement"]["permitted_operations"] = ["plan_placement"]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            current = real.snapshot(self.project)["digest"]
            contract_hash = bt._contract().digest(contract)
            plan = self.call("plan_board_placement", project_path=str(self.project),
                             expected_bundle_digest=current,
                             contract_sha256=contract_hash, variant="V1",
                             operation="plan_placement", moves=[{
                                 "footprint_uuid": footprint_id, "dx_mm": 0.1, "dy_mm": -0.2}])
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("native adaptive", plan["missing_capability"])
            with self.assertRaises(Exception):
                self.call("plan_board_placement", project_path=str(self.project),
                          expected_bundle_digest=current,
                          contract_sha256=contract_hash, variant="V1",
                          operation="plan_placement", moves=[{
                              "footprint_uuid": footprint_id, "dx_mm": float("inf"), "dy_mm": 0}])

    @unittest.skipUnless(os.name == "nt", "Windows job containment test")
    def test_waiting_descendant_reaped_at_root_exit(self):
        marker = self.root / "descendant.txt"
        child_code = ("import os,threading;from pathlib import Path;"
                      f"Path({str(marker)!r}).write_text(str(os.getpid()));"
                      "threading.Event().wait()")
        parent_code = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                       f"subprocess.Popen([sys.executable,'-B','-c',{child_code!r}],"
                       "stdout=sys.stdout,stderr=sys.stderr,"
                       "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                       f"m=Path({str(marker)!r})\n"
                       "while not m.exists(): time.sleep(.01)\n"
                       "print('root-exit',flush=True)")
        evidence = self.root / "root-exit-evidence"
        result = owned.run_contained(
            [sys.executable, "-B", "-c", parent_code], timeout=10,
            evidence_dir=evidence, evidence_context={"fixture": "waiting descendant"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("root-exit", result.stdout)
        self.assertIn(b"root-exit", (evidence / "stdout.bin").read_bytes())
        events = [json.loads(line)["event"] for line in (evidence / "events.jsonl").read_text().splitlines()]
        self.assertLess(events.index("root_exit"), events.index("owned_tree_stopped"))
        child_pid = int(marker.read_text())
        from kicad_mcp.utils import winproc
        deadline = time.monotonic() + 180  # Test-only termination synchronization.
        while winproc.identity(child_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(winproc.identity(child_pid))

    @unittest.skipUnless(os.name == "nt", "Windows job containment test")
    def test_timeout_retains_output_and_spares_unowned_child(self):
        hidden = _HiddenHelpers
        marker = self.root / "unowned-sentinel-ready.txt"
        sentinel_code = ("import threading;from pathlib import Path;"
                         f"Path({str(marker)!r}).write_text('ready');"
                         "threading.Event().wait()")
        sentinel = hidden.popen_hidden(
            [sys.executable, "-B", "-c", sentinel_code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 180  # Test synchronization watchdog.
            while not marker.is_file():
                self.assertIsNone(sentinel.poll())
                if time.monotonic() >= deadline:
                    self.fail("Unowned sentinel did not reach its wait point")
                time.sleep(0.01)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                owned.run_contained(
                    [sys.executable, "-B", "-c",
                     "import threading;print('partial',flush=True);"
                     "threading.Event().wait()"], timeout=10)
            self.assertIn("partial", caught.exception.output)
            self.assertIsNone(sentinel.poll())
            self.assertFalse(owned._owned_jobs)
        finally:
            sentinel.terminate()  # Exact Popen handle launched by this test.
            sentinel.communicate()

    @unittest.skipUnless(os.name == "nt", "Windows job containment test")
    def test_server_shutdown_reaps_owned_job_and_keeps_output(self):
        marker = self.root / "running.txt"
        code = ("import threading;from pathlib import Path;"
                "print('before-shutdown',flush=True);"  # output first, THEN the marker the test waits for
                f"Path({str(marker)!r}).write_text('started');threading.Event().wait()")
        outcome = {}

        def launch():
            outcome["result"] = owned.run_contained(
                [sys.executable, "-B", "-c", code])

        worker = threading.Thread(target=launch)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            owned.close_owned_processes()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertIn("before-shutdown", outcome["result"].stdout)
            self.assertNotEqual(outcome["result"].returncode, 0)
            self.assertFalse(owned._owned_jobs)
        finally:
            if worker.is_alive():
                owned.close_owned_processes()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
