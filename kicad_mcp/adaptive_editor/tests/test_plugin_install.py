"""Plugin installer ownership rules (no KiCad needed): it removes only what it installed and refuses to
overwrite anything it does not own."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


def _installer():
    spec = importlib.util.spec_from_file_location("plugin_install", PLUGIN / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InstallerOwnership(unittest.TestCase):
    def setUp(self):
        self.inst = _installer()
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "plugins" / self.inst.PLUGIN_ID

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self):
        return json.loads((self.target / self.inst.RECORD).read_text(encoding="utf-8"))

    def test_fresh_install_then_uninstall_leaves_nothing(self):
        self.inst.install(self.target, False)
        self.assertTrue(self._record()["created_target"])
        cache = self.target / "__pycache__"  # bytecode Python writes for an installed module
        cache.mkdir()
        (cache / "panel.cpython-312.pyc").write_bytes(b"x")
        out = self.inst.uninstall(self.target, False)
        self.assertEqual(out["kept_modified"], [])
        self.assertFalse(self.target.exists())

    def test_user_files_and_foreign_bytecode_survive_uninstall(self):
        self.inst.install(self.target, False)
        (self.target / "my-notes.txt").write_text("mine", encoding="utf-8")
        cache = self.target / "__pycache__"
        cache.mkdir()
        (cache / "my_module.cpython-312.pyc").write_bytes(b"x")
        self.inst.uninstall(self.target, False)
        self.assertTrue((self.target / "my-notes.txt").is_file())
        self.assertTrue((cache / "my_module.cpython-312.pyc").is_file())

    def test_pre_existing_empty_target_is_not_removed(self):
        self.target.mkdir(parents=True)
        self.inst.install(self.target, False)
        self.assertFalse(self._record()["created_target"])
        self.inst.uninstall(self.target, False)
        self.assertTrue(self.target.is_dir())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_modified_file_blocks_upgrade_and_survives_uninstall(self):
        self.inst.install(self.target, False)
        (self.target / "panel.py").write_text("# changed by the user\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.inst.install(self.target, False)
        out = self.inst.uninstall(self.target, False)
        self.assertEqual(out["kept_modified"], ["panel.py"])
        self.assertTrue((self.target / "panel.py").is_file())

    def test_upgrade_refuses_to_overwrite_an_unowned_file(self):
        self.inst.install(self.target, False)
        record = self._record()
        record["files"].pop("panel.py")  # an older version that did not ship panel.py ...
        (self.target / self.inst.RECORD).write_text(json.dumps(record), encoding="utf-8")
        (self.target / "panel.py").write_text("# the user's own file\n", encoding="utf-8")  # ... user's file
        with self.assertRaises(SystemExit) as info:
            self.inst.install(self.target, False)
        self.assertIn("panel.py", str(info.exception))
        self.assertEqual((self.target / "panel.py").read_text(encoding="utf-8"), "# the user's own file\n")

    def test_interrupted_install_is_recoverable(self):
        self.inst.install(self.target, False)
        record = self._record()
        rel = "panel.py"
        record["pending"] = {rel: self.inst._sha(self.target / rel)}
        record["files"][rel] = "0" * 64  # as if the copy of the new version had already happened
        (self.target / self.inst.RECORD).write_text(json.dumps(record), encoding="utf-8")
        self.inst.install(self.target, False)  # must not refuse with 'modified locally'
        self.assertNotIn("pending", self._record())

    def test_a_copy_killed_partway_leaves_no_truncated_file_and_recovers(self):
        self.inst.install(self.target, False)
        rel = "panel.py"
        installed = (self.target / rel).read_bytes()
        record = self._record()
        record["pending"] = dict(record["files"])
        record["part_suffix"] = self.inst.PART_SUFFIX + "-killedrun01"
        (self.target / self.inst.RECORD).write_text(json.dumps(record), encoding="utf-8")
        part = self.target / (rel + record["part_suffix"])
        part.write_bytes(installed[: len(installed) // 2])  # the process died mid-copy
        self.assertEqual((self.target / rel).read_bytes(), installed)  # the real file was never truncated
        self.inst.install(self.target, False)  # no 'modified locally' refusal
        self.assertFalse(part.exists())  # its own partial copy is cleaned up
        self.assertNotIn("pending", self._record())
        foreign = self.target / (rel + self.inst.PART_SUFFIX)  # same prefix, but not named by any install
        foreign.write_bytes(b"someone else's file")
        out = self.inst.uninstall(self.target, False)
        self.assertEqual(out["kept_modified"], [])
        self.assertEqual(foreign.read_bytes(), b"someone else's file")  # never removed: not ours

    def test_a_crash_between_writing_and_renaming_the_record_is_recoverable(self):
        self.inst.install(self.target, False)
        record_path = self.target / self.inst.RECORD
        record_path.replace(self.target / (self.inst.RECORD + ".tmp"))  # written, not yet renamed
        self.inst.install(self.target, False)  # the pending record is recognised as ours
        self.assertTrue(record_path.is_file())
        self.assertFalse((self.target / (self.inst.RECORD + ".tmp")).exists())
        record_path.replace(self.target / (self.inst.RECORD + ".tmp"))
        self.inst.uninstall(self.target, False)
        self.assertFalse(self.target.exists())

    def test_an_interrupted_install_of_another_version_leaves_nothing_behind(self):
        self.inst.install(self.target, False)
        record = self._record()
        extra = "engine/adaptive_engine/only_in_version_b.py"  # a file of the interrupted version only
        (self.target / extra).write_text("b-only", encoding="utf-8")
        record["pending"] = {**record["files"], extra: self.inst._sha(self.target / extra)}
        (self.target / self.inst.RECORD).write_text(json.dumps(record), encoding="utf-8")
        out = self.inst.install(self.target, False)  # the current version, without that file
        self.assertIn(extra, out["removed_stale"])
        self.assertFalse((self.target / extra).exists())
        self.inst.uninstall(self.target, False)
        self.assertFalse(self.target.exists())  # nothing left behind

    def test_a_first_install_killed_before_its_folder_existed_leaves_only_a_removable_staging_folder(self):
        staging = self.target.parent / f".{self.target.name}.creating-killed00001"
        staging.mkdir(parents=True)
        (staging / self.inst.RECORD).write_text(json.dumps(
            {"plugin": self.inst.PLUGIN_ID, "files": {}, "created_target": True, "dirs": []}), encoding="utf-8")
        self.assertFalse(self.target.exists())  # the target never exists without its record
        self.inst.install(self.target, False)
        self.assertFalse(staging.exists())  # proven ours (only our record inside): removed
        self.assertTrue(self._record()["created_target"])
        self.inst.uninstall(self.target, False)
        self.assertFalse(self.target.exists())  # created by the installer, so removed by it

    def test_an_abandoned_staging_folder_is_tidied_even_before_its_record_was_complete(self):
        for suffix, files in (("emptyfolder01", {}), ("truncated0001", {self.inst.RECORD + ".tmp-x": '{"plug'})):
            staging = self.target.parent / f".{self.target.name}.creating-{suffix}"
            staging.mkdir(parents=True)
            for name, text in files.items():
                (staging / name).write_text(text, encoding="utf-8")
        foreign = self.target.parent / f".{self.target.name}.creating-someoneelse"
        foreign.mkdir()
        (foreign / "notes.txt").write_text("not ours", encoding="utf-8")
        self.inst.install(self.target, False)
        left = sorted(p.name for p in self.target.parent.glob(f".{self.target.name}.creating-*"))
        self.assertEqual(left, [foreign.name])  # only folders holding nothing but our record names are removed

    def test_a_truncated_record_write_is_never_trusted_and_never_blocks(self):
        self.inst.install(self.target, False)
        good = self._record()
        (self.target / (self.inst.RECORD + ".tmp-truncated01")).write_text('{"plugin": "local.kic', encoding="utf-8")
        self.assertEqual(self.inst._read_record(self.target), good)  # the complete record wins
        (self.target / self.inst.RECORD).replace(self.target / (self.inst.RECORD + ".tmp-complete001"))
        self.assertEqual(self.inst._read_record(self.target), good)  # a complete pending record is recovered
        self.inst.install(self.target, False)  # and the next install proceeds and tidies the leftovers
        self.assertEqual(sorted(p.name for p in self.target.glob(self.inst.RECORD + "*")), [self.inst.RECORD])
        self.inst.uninstall(self.target, False)
        self.assertFalse(self.target.exists())

    def test_manifest_identifier_passes_kicads_own_rule(self):
        import re
        self.assertEqual(self.inst.check_manifest()["identifier"], self.inst.PLUGIN_ID)
        rule = self.inst.KICAD_IDENTIFIER_RULE
        self.assertIsNone(re.search(rule, "kicad-ai-tools.adaptive-editor"))  # dropped silently by KiCad 10.0.5
        self.assertIsNotNone(re.search(rule, "com.example.adaptive_editor"))

    def test_unowned_non_empty_target_is_refused(self):
        self.target.mkdir(parents=True)
        (self.target / "something.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.inst.install(self.target, False)

    def test_record_with_parent_path_is_not_trusted_by_uninstall(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("keep me", encoding="utf-8")
        self.target.mkdir(parents=True)
        record = {"plugin": self.inst.PLUGIN_ID,
                  "files": {"../outside.txt": self.inst._sha(outside)}}
        (self.target / self.inst.RECORD).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.inst.uninstall(self.target, False)
        self.assertTrue(outside.is_file())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
