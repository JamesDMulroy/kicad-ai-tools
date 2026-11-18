"""Hotkey routing without KiCad: the profile file and the twelve-slot runner."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the dispatcher drives Win32 windows")


def test_batch_file_gives_each_action_one_key_and_blanks_the_rest(tmp_path: Path) -> None:
    from kicad_mcp.ipc import hotkeys

    names = ["a.one", "a.two", "a.three", "b.other"]
    assigned = hotkeys.write_hotkeys(tmp_path, names, ["a.one", "a.three"])
    lines = (tmp_path / "user.hotkeys").read_text(encoding="utf-8").splitlines()
    table = {line.split("\t")[0]: line.split("\t")[1] for line in lines}
    assert table == {"a.one": "F2", "a.two": "", "a.three": "F3", "b.other": ""}
    assert assigned["a.three"] == ("F3", 0x72)
    with pytest.raises(ValueError):
        hotkeys.write_hotkeys(tmp_path, names, ["x"] * (len(hotkeys.KEYS) + 1))


class _FakeSession:
    def __init__(self, root: Path):
        self.profile = root

    def _settings_version(self) -> str:
        return "10.99"


def test_runner_slots_use_unbound_keys_and_evict_least_recent(tmp_path: Path) -> None:
    from kicad_mcp.ipc import hotkeys

    runner = hotkeys.HotkeyRunner(_FakeSession(tmp_path))
    for i in range(len(hotkeys.SLOT_KEYS)):
        runner._assign(f"act.{i}")
    assert {key for key, _ in runner.slots.values()} == {k for k, _ in hotkeys.SLOT_KEYS}  # F13..F24 only
    runner.slots["act.0"] = runner.slots.pop("act.0")  # act.0 used again: act.1 is now the oldest
    runner._assign("act.new")
    assert "act.1" not in runner.slots and "act.0" in runner.slots
    written = (tmp_path / "10.99" / "user.hotkeys").read_text(encoding="utf-8") if False else \
        (tmp_path / "config" / "10.99" / "user.hotkeys").read_text(encoding="utf-8")
    assert "act.new\t" in written and "act.1\t" not in written
    assert len(written.splitlines()) == len(hotkeys.SLOT_KEYS)  # only the slots: no other action is touched


def test_shot_is_exact_outside_a_gl_canvas_and_tolerant_inside():
    from kicad_mcp.ipc.hotkeys import Shot

    width, height = 300, 300
    blank = bytearray(width * height * 4)

    def shot(changes, gl=(0, 0, 150, 300), skip=(290, 300)):
        raw = bytearray(blank)
        for x, y in changes:
            raw[4 * (y * width + x)] = 200
        return Shot(bytes(raw), width, height, gl, skip)

    base = shot([])
    assert base == shot([(3, 3)])  # one canvas pixel of noise
    assert base != shot([(x, y) for x in range(0, 150, 3) for y in range(0, 60, 3)])  # a real canvas change
    assert base != shot([(200, 10)])  # one pixel outside the canvas: exact
    assert base == shot([(200, 295)])  # the GL window's status bar rows are not compared
    assert Shot(bytes(blank), width, height, None) != Shot(bytes(shot([(3, 3)]).raw), width, height, None)
