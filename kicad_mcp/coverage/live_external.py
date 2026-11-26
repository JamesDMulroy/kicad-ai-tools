"""Live proof for actions that hand off to another program (browser, Explorer, text editor, KiCad tools).

Each action runs in a fresh session with ``launch_hook`` installed in its KiCad process first (the run is
skipped if the hook is not confirmed): a request to Windows for a browser, Explorer or an outside program is
recorded and never started, so nothing reaches the owner's screen. KiCad's own programs (library browsers,
calculator, print dialog) start for real on the private desktop. The action is triggered the way a person
would: its own menu item when the frame has one, otherwise its key from the session profile. A pass needs
the recorded request to name the expected target (URL, folder, file with the configured editor), or the
expected KiCad window to open. KiCad's "File Not Found ... open the online help?" prompt is answered Yes.

    python -m kicad_mcp.coverage.live_external --inventory inv.json --project <dir> --group suite --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import contextlib
import json
import os
from pathlib import Path
import time

from kicad_mcp.coverage.launch_hook import LaunchHook
from kicad_mcp.coverage.live_hotkeys import FRAMES, _input_for, _start
from kicad_mcp.ipc import hotkeys

EDITOR_EXE = os.path.join(os.environ.get("SYSTEMROOT", "Windows"), "notepad.exe")  # the profile's text editor; never started
IDYES, IDOK = 6, 1

# action -> (frame, context, what proves it: substrings of the recorded request, or "window:<title part>")
CASES = {
    "suite": [
        ("common.SuiteControl.donate", "eeschema", None, ["app-donate"]),
        ("common.SuiteControl.getInvolved", "eeschema", None, ["contribute"]),
        ("common.SuiteControl.reportBug", "eeschema", None, ["issues/new", "10.99.0"]),
    ],
    "suite2": [
        ("common.SuiteControl.gettingStarted", "eeschema", None, ["getting_started_in_kicad"]),
        ("common.SuiteControl.help", "eeschema", None, ["go.kicad.org/docs/"]),
        ("common.Control.showDatasheet", "symedit", "datasheet", ["kicad-ai-tools/datasheet.pdf"]),
        ("common.Control.print", "eeschema", None, ["window:Print"]),
        ("common.Control.showCalculatorTools", "eeschema", None, ["window:Calculator"]),
        ("common.Control.showSymbolBrowser", "eeschema", None, ["window:Symbol Library Browser"]),
    ],
    "board": [
        ("common.Control.showFootprintBrowser", "pcbnew", None, ["window:Footprint Library Browser"]),
        ("common.API.pluginsShowFolder", "pcbnew", None, ["plugins"]),
    ],
    "library": [
        ("common.Control.openDirectory", "symedit", "library", [".kicad_sym"]),
        ("common.Control.openWithTextEditor", "symedit", "library", ["notepad", ".kicad_sym"]),
    ],
    "manager": [
        ("kicad.Control.editPCB", "kicad", None, ["window:PCB Editor"]),
        ("kicad.Control.viewGerbers", "kicad", None, ["window:Gerber Viewer"]),
        ("kicad.Control.showCalculator", "kicad", None, ["window:Calculator Tools"]),
        ("kicad.Control.restoreLocalHistory", "kicad", "history", ["window:*"], "restore project from local history"),
        ("kicad.Control.loadProject", "kicad", None, ["window:*"], "open project"),
        ("kicad.Control.closeProject", "kicad", None, ["title-changed"], "close project"),
        ("kicad.Control.editOtherSch", "kicad", "other.kicad_sch", ["eeschema", "other.kicad_sch"]),
        ("kicad.Control.editOtherPCB", "kicad", "other.kicad_pcb", ["pcbnew", "other.kicad_pcb"]),
    ],
    "project": [
        ("kicad.Control.OpenProjectDirectory", "kicad", None, ["frames", "work"], "browse project files"),
        ("kicad.Control.openTextEditor", "kicad", None, ["notepad"]),
    ],
}


DATASHEET_URL = "https://www.example.com/kicad-ai-tools/datasheet.pdf"


def _datasheet_fixture(project: Path) -> Path:
    """A copy of ``project`` whose library symbols all carry a datasheet URL (the symbol editor shows the
    open symbol's datasheet)."""
    import shutil

    copy = Path(scoped_mkdtemp(prefix="kicad-ai-tools-ds-")) / project.name
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns("~*", "*.lck", "*-backups"))
    for library in copy.glob("*.kicad_sym"):
        text = library.read_text(encoding="utf-8")
        library.write_text(text.replace('(property "Datasheet" ""', f'(property "Datasheet" "{DATASHEET_URL}"'),
                           encoding="utf-8")
    return copy


def _manager_fixture(project: Path, context: str) -> Path:
    """A project copy for the project manager: with a local-history repository holding one snapshot
    ("history"), or with a schematic or board that is not the project's own ("other.kicad_*", opened by
    activating it in the project tree)."""
    import shutil
    import subprocess

    copy = Path(scoped_mkdtemp(prefix="kicad-ai-tools-mgr-")) / project.name
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns("~*", "*.lck", "*-backups", ".history"))
    if context == "history":
        history = copy / ".history"
        history.mkdir()
        shutil.copy2(next(copy.glob("*.kicad_sch")), history / next(copy.glob("*.kicad_sch")).name)
        flags = subprocess.CREATE_NO_WINDOW
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=kicad-ai-tools", "-c", "user.email=kicad-ai-tools@localhost",
                      "commit", "-q", "-m", "snapshot"]):
            subprocess.run(["git", "-C", str(history), *args], check=True, capture_output=True, timeout=60,
                           creationflags=flags)
    elif context.startswith("other."):
        source = next(copy.glob("*" + Path(context).suffix))
        shutil.copy2(source, copy / context)
        # the project tree lists schematics other than the project's own only in a git repository
        subprocess.run(["git", "-C", str(copy), "init", "-q"], check=True, capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return copy


def _send_message():
    """SendMessageW taking and returning pointer-sized values (tree item handles are 64-bit)."""
    import ctypes
    from ctypes import wintypes

    send = ctypes.WinDLL("user32").SendMessageW
    send.restype = ctypes.c_void_p
    send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
    return send


def _tree_items(tree: int) -> list[tuple[int, str]]:
    """(handle, text) of every row in a native tree control (SysTreeView32) in another process: its rows
    are read with TVM_GETITEMW through a buffer in that process (UI Automation cannot select them)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.VirtualAllocEx.restype = ctypes.c_void_p
    k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    send = _send_message()
    TVM_GETNEXTITEM, TVM_GETITEMW, TVIF_TEXT = 0x110A, 0x113E, 0x0001
    TVGN_ROOT, TVGN_NEXT, TVGN_CHILD = 0, 1, 4

    class TVITEMW(ctypes.Structure):
        _fields_ = [("mask", wintypes.UINT), ("hItem", ctypes.c_void_p), ("state", wintypes.UINT),
                    ("stateMask", wintypes.UINT), ("pszText", ctypes.c_void_p), ("cchTextMax", ctypes.c_int),
                    ("iImage", ctypes.c_int), ("iSelectedImage", ctypes.c_int), ("cChildren", ctypes.c_int),
                    ("lParam", ctypes.c_void_p)]

    owner = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(tree, ctypes.byref(owner))
    process = k32.OpenProcess(0x0008 | 0x0010 | 0x0020, False, owner.value)  # VM_OPERATION, VM_READ, VM_WRITE
    remote = k32.VirtualAllocEx(process, None, 4096, 0x3000, 0x04)
    try:
        def text(item: int) -> str:
            tv = TVITEMW(mask=TVIF_TEXT, hItem=item, pszText=remote + 512, cchTextMax=1000)
            k32.WriteProcessMemory(process, remote, ctypes.byref(tv), ctypes.sizeof(tv), None)
            send(tree, TVM_GETITEMW, 0, remote)
            buf = ctypes.create_unicode_buffer(1000)
            k32.ReadProcessMemory(process, remote + 512, buf, 2000, None)
            return buf.value

        rows: list[tuple[int, str]] = []

        def walk(item):
            while item:
                rows.append((item, text(item)))
                walk(send(tree, TVM_GETNEXTITEM, TVGN_CHILD, item))
                item = send(tree, TVM_GETNEXTITEM, TVGN_NEXT, item)

        walk(send(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0))
        return rows
    finally:
        k32.VirtualFreeEx(process, remote, 0, 0x8000)
        k32.CloseHandle(process)


def _activate_tree_item(session, name: str) -> str:
    """Activate the project-tree row named ``name`` the way a keyboard user does: Home, Down to the row,
    Enter (wxEVT_TREE_ITEM_ACTIVATED). The tree is a multi-selection one (wxTR_MULTIPLE), where wx vetoes
    any selection change it did not make itself (TVN_ITEMCHANGING), so the row is reached with the keys wx
    handles. Returns the activated row's text."""
    frame = hotkeys.canvas_of(session, "KiCad")[0]
    trees = [c for c in hotkeys._children(frame) if c[1] == "SysTreeView32" and c[3]]
    if not trees:
        raise RuntimeError("no project tree in the project manager")
    tree = max(trees, key=lambda c: c[2])[0]
    text = dict(_tree_items(tree))
    send = _send_message()
    visible, item = [], send(tree, 0x110A, 0x0000, 0)  # TVM_GETNEXTITEM, TVGN_ROOT
    while item:
        visible.append(item)
        item = send(tree, 0x110A, 0x0006, item)  # TVGN_NEXTVISIBLE
    index = next((i for i, h in enumerate(visible) if text.get(h) == name or text.get(h, "").endswith(name)), None)
    if index is None:
        raise RuntimeError(f"'{name}' not a visible project-tree row ({[text.get(h) for h in visible]})")
    for vk in [0x24] + [0x28] * index:  # VK_HOME, VK_DOWN
        hotkeys.press(tree, vk)
        time.sleep(0.1)
    time.sleep(0.5)
    caret = send(tree, 0x110A, 0x0009, 0)  # TVGN_CARET
    if caret != visible[index]:
        raise RuntimeError(f"keys reached '{text.get(caret)}', not '{name}'")
    hotkeys.press(tree, 0x0D)  # VK_RETURN: wx makes it "item activated" for the selected row
    # KiCad opens the row from its idle handler; with no input on the private desktop, wake the loop
    for _ in range(5):
        time.sleep(0.3)
        hotkeys.user32.PostMessageW(tree, 0x0000, 0, 0)  # WM_NULL
    return text[visible[index]]


def _select_library(session, editor: str) -> None:
    uia = session.attach_uia()
    window = {"name": FRAMES[editor][1], "match": "contains"}
    rows = uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "TreeItem"}})["controls"]
    if rows:
        uia.call("select", {"window": window, "selector": {"by": "path", "name": rows[0]["name"],
                                                           "control_type": "TreeItem"}})
        time.sleep(0.5)


WATCHED = ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "explorer.exe", "notepad.exe")


def _watched_processes() -> set[int]:
    """PIDs of browsers, Explorer and Notepad now running (to show none started during a case). Read from a
    Toolhelp process snapshot (tasklist timed out on a loaded machine)."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    pids: set[int] = set()
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        more = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() in WATCHED:
                pids.add(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return pids


def _settle_dialogs(session, before: set[int], seen: list[str]) -> None:
    """Answer KiCad's online-help prompt with Yes; close everything else that opened (Cancel/OK)."""
    for _ in range(4):
        new = {h: t for h, t in hotkeys._visible(session).items() if h not in before}
        if not new:
            return
        for hwnd, title in new.items():
            seen.append(title or "(untitled)")
            if title == "File Not Found":  # a wx dialog: its Yes button is pressed through UI Automation
                try:
                    session.attach_uia().call("invoke", {"window": {"name": "File Not Found", "match": "exact"},
                                                         "selector": {"by": "path", "name": "Yes",
                                                                      "control_type": "Button"}})
                except Exception:  # noqa: BLE001
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, IDYES, 0)
            else:
                hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, IDOK, 0)
                hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
        time.sleep(1.5)


@cleanup_after_call
def run_case(inventory: dict, project: Path, action: str, editor: str, context: str | None,
             expect: list[str], menu_label: str | None = None) -> dict:
    """``menu_label``: the Windows menu text when the inventory holds another platform's (an #ifdef'd
    friendly name such as "Reveal Project in Finder")."""
    meta = next(a for a in inventory["actions"] if a["name"] == action)
    all_actions = [a["name"] for a in inventory["actions"]]
    if context == "datasheet":
        project = _datasheet_fixture(project)
    elif context in ("history", "other.kicad_sch", "other.kicad_pcb"):
        project = _manager_fixture(project, context)
    document = _input_for(editor, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-ext-")))
    row = {"action": action, "frame": editor, "outcome": "no_effect", "detail": ""}
    session, assigned, frame, canvas = _start(editor, document, all_actions, [action],
                                              common_settings={"system.text_editor": EDITOR_EXE})
    hook = None
    try:
        hook = LaunchHook(session.pid, str(session.app_root))
        if not hook.ready:
            row.update(outcome="error", detail=f"launch hook not installed ({hook.installed}); not run")
            return row
        if context == "library":
            _select_library(session, editor)
        watched_before = _watched_processes()
        label = menu_label or hotkeys.normalize(meta.get("friendly_name") or "")
        item = next((i for i in hotkeys.menu_items(frame) if label and i["label"] == label), None)
        before = set(hotkeys._visible(session))
        title_before = hotkeys.window_text(frame)
        requests_before = list(hook.requests)  # the pre-state: what was asked of Windows before the action
        row["requests_before"] = requests_before
        if context and context.startswith("other."):
            row["route"] = f"project tree: activate {_activate_tree_item(session, context)}"
        elif item is not None:
            hotkeys.user32.PostMessageW(frame, hotkeys.WM_COMMAND, item["id"], 0)
            row["route"] = "menu: " + " > ".join(item["path"])
        else:
            hotkeys.press(canvas, assigned[action][1])
            row["route"] = f"key {assigned[action][0]}"
        time.sleep(3)
        windows: list[str] = []
        _settle_dialogs(session, before, windows)
        time.sleep(1)
        requests = list(hook.requests)
        row["requests"] = requests
        started = sorted(_watched_processes() - watched_before)
        row["browser_or_explorer_started"] = started  # should stay empty; anything here is reported
        row["windows"] = windows
        text = json.dumps(requests)
        ok = []
        for want in expect:
            if want == "window:*":
                ok.append(bool(windows))
            elif want == "title-changed":
                title_after = hotkeys.window_text(frame)
                row["title"] = [title_before, title_after]
                ok.append(bool(title_after) and title_after != title_before)
            elif want.startswith("window:"):
                ok.append(any(want[7:] in w for w in windows))
            else:
                ok.append(want.lower() in text.lower())
        if any(w for w in expect if not w.startswith(("window:", "title-")) and w.lower() in json.dumps(requests_before).lower()):
            ok.append(False)  # the expected request existed before the action: not attributable to it
        if expect and all(ok):
            shown = requests[0] if requests else windows
            detail = (f"asked Windows to open {shown} ({len(requests_before)} start requests before the action)" if requests
                      else f"opened {windows}")
            if "title" in row:
                detail = f"window title {row['title'][0]!r} -> {row['title'][1]!r}"
            row.update(outcome="ok", detail=detail)
        else:
            row["detail"] = f"expected {expect}; requests {requests[:2]}; windows {windows}"
        if not session.healthy():
            row["detail"] += "; editor not healthy after"
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        if hook is not None:
            with contextlib.suppress(Exception):  # the process may already have ended
                hook.close()
        session.close(discard=True)
    return row


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--group", choices=sorted(CASES), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated action names to run from the group")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    names = {a["name"] for a in inventory["actions"]}
    only = {n for n in args.only.split(",") if n}
    rows = [run_case(inventory, args.project, *case) for case in CASES[args.group]
            if case[0] in names and (not only or case[0] in only)]
    result = {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
              "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "external-launch-hook",
              "passed": sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok"), "rows": rows}
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in rows:
        print(row["outcome"], row["action"], row.get("route", ""), "|", row["detail"][:150])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
