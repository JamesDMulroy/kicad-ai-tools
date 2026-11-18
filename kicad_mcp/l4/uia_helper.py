"""UI Automation command executor. Runs ON the hidden desktop, spawned by the broker via
CreateProcessW + lpDesktop using ``pythonw.exe`` (no console, no window).

Channel: a directory shared with the broker. The broker writes ``cmd-NNNNNN.json`` (atomically);
this process answers ``reply-NNNNNN.json`` (atomically). ``ready.json`` is written once started,
``fatal.json`` if it cannot start, and a ``stop`` file makes it exit. Nothing depends on stdout.

Commands (closed set): ``find``, ``dump-tree``, ``invoke``, ``set-value``, ``read-value``,
``select-menu``. Every command is bound to the target PID given on the command line: the UIA root's
children are filtered by process id, and window and control lookups only ever start from those
windows, so a window is never found by name or title alone and a decoy process is never visible.
This helper only uses UIA patterns (no SendInput, no window messages, no clipboard).

This file is self-contained on purpose (it is run as a script by a different interpreter); it must not
import ``kicad_mcp``. The pure helpers (``norm``, ``check_selector``, ``check_window``) are unit-tested.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import traceback
from pathlib import Path

CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}
CONTROL_TYPE_IDS = {name: ident for ident, name in CONTROL_TYPES.items()}
COMMANDS = ("find", "dump-tree", "invoke", "set-value", "read-value", "select-menu", "focus", "state", "expand", "select")
MATCH_MODES = ("exact", "prefix", "contains")
SELECTOR_KEYS = {"by", "automation_id", "name", "name_match", "control_type", "ancestry"}

# UIA property / pattern ids and tree scopes
P_PROCESS_ID, P_CONTROL_TYPE, P_AUTOMATION_ID = 30002, 30003, 30011
PAT_INVOKE, PAT_VALUE, PAT_EXPAND, PAT_SELECTION_ITEM, PAT_LEGACY = 10000, 10002, 10005, 10010, 10018
PAT_TOGGLE = 10015
SCOPE_CHILDREN, SCOPE_DESCENDANTS = 2, 4
MAX_TREE_DEPTH, MAX_TREE_NODES = 12, 3000
MENU_POPUP_CLASS = "#32768"


class HelperError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


# ---- pure helpers -------------------------------------------------------------------------------

def norm(text: str | None) -> str:
    """Normalise a UIA name: ellipsis, accelerator ampersands and a trailing tab-shortcut are dropped."""
    value = (text or "").replace("…", "...")
    value = value.split("\t", 1)[0]
    return value.replace("&", "").strip()


def name_matches(actual: str | None, wanted: str, mode: str) -> bool:
    a, w = norm(actual), norm(wanted)
    if mode == "exact":
        return a == w
    if mode == "prefix":
        return a.startswith(w)
    if mode == "contains":
        return w in a
    if mode == "any":
        return True
    raise HelperError("invalid_match_mode", f"unknown match mode {mode!r}")


def check_window(window) -> dict | None:
    if window is None:
        return None
    if not isinstance(window, dict) or set(window) - {"name", "match"} or not isinstance(window.get("name"), str) \
            or not window["name"] or window.get("match", "exact") not in MATCH_MODES:
        raise HelperError("invalid_window", f"window must be {{name, match}} with match in {MATCH_MODES}: {window!r}")
    return {"name": window["name"], "match": window.get("match", "exact")}


def check_selector(selector) -> dict:
    """Closed selector: by automation_id or by path (name + control type + optional ancestry suffix)."""
    if not isinstance(selector, dict) or set(selector) - SELECTOR_KEYS:
        raise HelperError("invalid_selector", f"selector keys must be within {sorted(SELECTOR_KEYS)}: {selector!r}")
    by = selector.get("by")
    control_type = selector.get("control_type")
    if control_type is not None and control_type not in CONTROL_TYPE_IDS:
        raise HelperError("invalid_selector", f"unknown control_type {control_type!r}")
    ancestry = selector.get("ancestry", [])
    if not isinstance(ancestry, list) or len(ancestry) > 8:
        raise HelperError("invalid_selector", "ancestry must be a short list")
    for item in ancestry:
        if not isinstance(item, dict) or set(item) - {"name", "control_type"} or not isinstance(item.get("name"), str) \
                or item.get("control_type") not in CONTROL_TYPE_IDS:
            raise HelperError("invalid_selector", f"bad ancestry entry {item!r}")
    if by == "automation_id":
        if not isinstance(selector.get("automation_id"), str) or not selector["automation_id"]:
            raise HelperError("invalid_selector", "automation_id selector needs a non-empty automation_id")
    elif by == "path":
        if not isinstance(selector.get("name"), str) or not selector["name"] or control_type is None:
            raise HelperError("invalid_selector", "path selector needs name and control_type")
        if selector.get("name_match", "exact") not in ("exact", "prefix", "any"):
            raise HelperError("invalid_selector", "name_match must be exact, prefix or any")
    else:
        raise HelperError("invalid_selector", f"by must be automation_id or path, got {by!r}")
    return {"by": by, "automation_id": selector.get("automation_id"), "name": selector.get("name"),
            "name_match": selector.get("name_match", "exact"), "control_type": control_type,
            "ancestry": [{"name": a["name"], "control_type": a["control_type"]} for a in ancestry]}


def ancestry_matches(chain: list[dict], wanted: list[dict]) -> bool:
    """``wanted`` must equal the innermost ``len(wanted)`` entries of ``chain`` (outer to inner)."""
    if not wanted:
        return True
    if len(chain) < len(wanted):
        return False
    tail = chain[-len(wanted):]
    return all(norm(c["name"]) == norm(w["name"]) and c["control_type"] == w["control_type"]
               for c, w in zip(tail, wanted))


# On Windows a file that was just created or renamed can be held open for a moment by another process
# (an anti-malware scanner or the search indexer), so opening or renaming it fails with a sharing violation
# (PermissionError). Channel files are retried for a short window instead of failing the call.
SHARING_RETRY_S = 5.0


def with_sharing_retry(action, window_s: float | None = None):
    end = time.time() + (SHARING_RETRY_S if window_s is None else window_s)
    while True:
        try:
            return action()
        except PermissionError:
            if time.time() >= end:
                raise
            time.sleep(0.05)


def handle_command(channel: Path, path: Path, uia, done: set[str]) -> None:
    """Answer one ``cmd-NNNNNN.json`` exactly once. A command file that is briefly held open by another
    process (sharing violation) is retried; one that still cannot be removed after its reply is remembered
    in ``done`` and only removed, never executed again, on later passes."""
    number = path.stem.split("-", 1)[1]
    if number not in done:
        reply = {"id": None, "ok": False}
        try:
            request = json.loads(with_sharing_retry(lambda: path.read_text(encoding="utf-8")))
            reply["id"] = request.get("id")
            reply["result"] = uia.execute(request.get("cmd"), request.get("args", {}))
            reply["ok"] = True
        except HelperError as exc:
            reply["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
        except BaseException as exc:
            reply["error"] = {"code": "helper_exception", "message": f"{type(exc).__name__}: {exc}",
                              "details": {"trace": traceback.format_exc()[-3000:]}}
        write_json_atomic(channel / f"reply-{number}.json", reply)
        done.add(number)
    try:
        with_sharing_retry(path.unlink)
    except FileNotFoundError:
        pass
    except OSError:
        pass  # still held: removed on a later pass; ``done`` keeps it from running again


def write_json_atomic(path: Path, payload) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with_sharing_retry(lambda: temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8"))
    with_sharing_retry(lambda: os.replace(temporary, path))


# ---- UIA access ---------------------------------------------------------------------------------

class Uia:
    def __init__(self, pid: int):
        import comtypes
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        self.UIA = UIA
        self.comtypes_version = getattr(comtypes, "__version__", "unknown")
        self.iuia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        self.walker = self.iuia.ControlViewWalker
        self.true = self.iuia.CreateTrueCondition()
        self.pid = int(pid)

    # -- element reading
    @staticmethod
    def _safe(function, default=None):
        try:
            return function()
        except Exception:
            return default

    def describe(self, el) -> dict:
        control_type = self._safe(lambda: el.CurrentControlType, 0)
        return {"name": self._safe(lambda: el.CurrentName, "") or "",
                "automation_id": self._safe(lambda: el.CurrentAutomationId, "") or "",
                "control_type": CONTROL_TYPES.get(control_type, str(control_type)),
                "class_name": self._safe(lambda: el.CurrentClassName, "") or "",
                "enabled": bool(self._safe(lambda: el.CurrentIsEnabled, False)),
                "offscreen": bool(self._safe(lambda: el.CurrentIsOffscreen, False))}

    def runtime_id(self, el):
        return tuple(self._safe(lambda: el.GetRuntimeId(), ()) or ())

    def ancestry(self, el, root) -> list[dict]:
        """Ancestors from the top-level window (excluded) down to ``el``'s parent."""
        chain = []
        current = self._safe(lambda: self.walker.GetParentElement(el))
        while current:
            if self._safe(lambda: self.iuia.CompareElements(current, root), False):
                break
            chain.append(current)
            current = self._safe(lambda: self.walker.GetParentElement(current))
        chain.reverse()
        if chain:
            chain = chain[1:]  # the first entry is the pid-owned top-level window itself
        return [{"name": norm(self.describe(c)["name"]), "control_type": self.describe(c)["control_type"]}
                for c in chain]

    # -- windows
    def top_windows(self, wmatch=None):
        root = self.iuia.GetRootElement()
        by_pid = self.iuia.CreatePropertyCondition(P_PROCESS_ID, self.pid)
        found = root.FindAll(SCOPE_CHILDREN, by_pid)
        candidates = [found.GetElement(index) for index in range(found.Length)]
        # UIA places owned top-level windows (wx dialogs whose parent is the frame) under their owner,
        # not under the desktop root, so look one level down for Window-type children of the task.
        owned = self.iuia.CreateAndCondition(by_pid, self.iuia.CreatePropertyCondition(
            P_CONTROL_TYPE, CONTROL_TYPE_IDS["Window"]))
        for top in list(candidates):
            sub = self._safe(lambda: top.FindAll(SCOPE_CHILDREN, owned))
            for index in range(sub.Length if sub is not None else 0):
                candidates.append(sub.GetElement(index))
        windows, seen = [], set()
        for el in candidates:
            key = self.runtime_id(el) or id(el)
            if key in seen:
                continue
            seen.add(key)
            if wmatch is None or name_matches(self._safe(lambda: el.CurrentName, ""), wmatch["name"], wmatch["match"]):
                windows.append(el)
        return windows

    def popup_menus(self):
        return [w for w in self.top_windows() if self._safe(lambda: w.CurrentClassName, "") == MENU_POPUP_CLASS]

    # -- patterns
    def pattern(self, el, pattern_id, interface):
        try:
            raw = el.GetCurrentPattern(pattern_id)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return raw.QueryInterface(interface)
        except Exception:
            return None

    # -- control search
    def find_controls(self, wmatch, selector) -> list:
        sel = check_selector(selector)
        results, seen = [], set()
        for window in self.top_windows(wmatch):
            if sel["by"] == "automation_id":
                condition = self.iuia.CreatePropertyCondition(P_AUTOMATION_ID, sel["automation_id"])
                if sel["control_type"]:
                    condition = self.iuia.CreateAndCondition(
                        condition, self.iuia.CreatePropertyCondition(P_CONTROL_TYPE, CONTROL_TYPE_IDS[sel["control_type"]]))
            else:
                condition = self.iuia.CreatePropertyCondition(P_CONTROL_TYPE, CONTROL_TYPE_IDS[sel["control_type"]])
            found = window.FindAll(SCOPE_DESCENDANTS, condition)
            for index in range(found.Length):
                el = found.GetElement(index)
                info = self.describe(el)
                if sel["by"] == "path":
                    if not name_matches(info["name"], sel["name"], sel["name_match"]):
                        continue
                    if sel["ancestry"] and not ancestry_matches(self.ancestry(el, self.iuia.GetRootElement()), sel["ancestry"]):
                        continue
                key = self.runtime_id(el) or id(el)
                if key in seen:
                    continue
                seen.add(key)
                results.append((el, window))
        return results

    def one(self, wmatch, selector):
        matches = self.find_controls(wmatch, selector)
        if not matches:
            raise HelperError("control_not_found", "selector matched no control", selector=selector)
        if len(matches) > 1:
            raise HelperError("control_ambiguous", f"selector matched {len(matches)} controls",
                              matches=[self.describe(m[0]) for m in matches[:5]])
        return matches[0][0]

    # -- commands
    def cmd_find(self, args):
        wmatch = check_window(args.get("window"))
        if args.get("selector") is None:
            windows = self.top_windows(wmatch)
            return {"count": len(windows), "matches": [self.describe(w) for w in windows]}
        matches = self.find_controls(wmatch, args["selector"])
        root = self.iuia.GetRootElement()
        out = []
        for el, _window in matches[:50]:
            item = self.describe(el)
            item["ancestry"] = self.ancestry(el, root)
            if item["automation_id"]:
                try:
                    by_id = self.find_controls(wmatch, {"by": "automation_id", "automation_id": item["automation_id"],
                                                        "control_type": item["control_type"]
                                                        if item["control_type"] in CONTROL_TYPE_IDS else None})
                    item["automation_id_matches"] = len(by_id)
                except HelperError:
                    item["automation_id_matches"] = None
            out.append(item)
        return {"count": len(matches), "matches": out}

    def _tree(self, el, depth, max_depth, budget):
        node = self.describe(el)
        budget["n"] -= 1
        if budget["n"] <= 0:
            node["truncated"] = True
            return node
        if depth < max_depth:
            kids = el.FindAll(SCOPE_CHILDREN, self.true)
            node["children"] = []
            for index in range(kids.Length):
                if budget["n"] <= 0:
                    node["truncated"] = True
                    break
                node["children"].append(self._tree(kids.GetElement(index), depth + 1, max_depth, budget))
        return node

    def cmd_dump_tree(self, args):
        wmatch = check_window(args.get("window"))
        depth = min(int(args.get("max_depth", 8)), MAX_TREE_DEPTH)
        budget = {"n": min(int(args.get("max_nodes", 1500)), MAX_TREE_NODES)}
        return {"windows": [self._tree(w, 0, depth, budget) for w in self.top_windows(wmatch)]}

    def _enabled(self, el):
        if not self._safe(lambda: el.CurrentIsEnabled, False):
            raise HelperError("control_disabled", "control is disabled", control=self.describe(el))

    def invoke_element(self, el) -> str:
        self._enabled(el)
        UIA = self.UIA
        for pattern_id, interface, action, label in (
                (PAT_INVOKE, UIA.IUIAutomationInvokePattern, lambda p: p.Invoke(), "invoke"),
                (PAT_SELECTION_ITEM, UIA.IUIAutomationSelectionItemPattern, lambda p: p.Select(), "select"),
                (PAT_LEGACY, UIA.IUIAutomationLegacyIAccessiblePattern, lambda p: p.DoDefaultAction(), "default_action")):
            pat = self.pattern(el, pattern_id, interface)
            if pat is not None:
                action(pat)
                return label
        raise HelperError("pattern_unsupported", "no invoke-like pattern on control", control=self.describe(el))

    def cmd_invoke(self, args):
        el = self.one(check_window(args.get("window")), args.get("selector"))
        return {"control": self.describe(el), "via": self.invoke_element(el)}

    def cmd_set_value(self, args):
        value = args.get("value")
        if not isinstance(value, str) or len(value) > 1000 or "\x00" in value:
            raise HelperError("invalid_value", "value must be a short string without NUL")
        el = self.one(check_window(args.get("window")), args.get("selector"))
        self._enabled(el)
        pat = self.pattern(el, PAT_VALUE, self.UIA.IUIAutomationValuePattern)
        if pat is None:
            raise HelperError("pattern_unsupported", "control has no ValuePattern", control=self.describe(el))
        if pat.CurrentIsReadOnly:
            raise HelperError("control_read_only", "control is read-only", control=self.describe(el))
        pat.SetValue(value)
        after = pat.CurrentValue
        return {"control": self.describe(el), "value_after": after, "matched": after == value}

    def element_state(self, el) -> dict:
        """Selection, keyboard focus, toggle and expand state of one control (each only when it has one)."""
        UIA = self.UIA
        state = {"focused": bool(self._safe(lambda: el.CurrentHasKeyboardFocus, False))}
        pat = self.pattern(el, PAT_SELECTION_ITEM, UIA.IUIAutomationSelectionItemPattern)
        if pat is not None:
            state["selected"] = bool(self._safe(lambda: pat.CurrentIsSelected, False))
        pat = self.pattern(el, PAT_TOGGLE, UIA.IUIAutomationTogglePattern)
        if pat is not None:
            state["toggle"] = int(self._safe(lambda: pat.CurrentToggleState, -1))
        pat = self.pattern(el, PAT_EXPAND, UIA.IUIAutomationExpandCollapsePattern)
        if pat is not None:
            state["expanded"] = int(self._safe(lambda: pat.CurrentExpandCollapseState, -1))
        return state

    def cmd_focus(self, args):
        """Keyboard focus to a control. UIA's SetFocus does not take in a window that is not active on its
        desktop, so the native control is focused through the owning thread's input state as well."""
        el = self.one(check_window(args.get("window")), args.get("selector"))
        self._safe(el.SetFocus)
        hwnd = int(self._safe(lambda: el.CurrentNativeWindowHandle, 0) or 0)
        via = "uia"
        if hwnd and not self._safe(lambda: el.CurrentHasKeyboardFocus, False):
            user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
            user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.SetFocus.argtypes = [ctypes.c_void_p]
            target = user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
            mine = kernel32.GetCurrentThreadId()
            if target and user32.AttachThreadInput(mine, target, True):
                try:
                    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    user32.GetAncestor.restype = ctypes.c_void_p
                    user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
                    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                    root = user32.GetAncestor(ctypes.c_void_p(hwnd), 2)  # GA_ROOT: the top-level frame
                    user32.SetForegroundWindow(ctypes.c_void_p(root))  # this desktop's foreground only
                    user32.SetActiveWindow(ctypes.c_void_p(root))
                    user32.SetFocus(ctypes.c_void_p(hwnd))
                    via = "attached-thread"
                finally:
                    user32.AttachThreadInput(mine, target, False)
            time.sleep(0.1)
        return {"control": self.describe(el), "state": self.element_state(el), "via": via, "hwnd": hwnd}

    def cmd_select(self, args):
        """Select a list or tree row (SelectionItemPattern.Select), as a click on it would."""
        el = self.one(check_window(args.get("window")), args.get("selector"))
        pat = self.pattern(el, PAT_SELECTION_ITEM, self.UIA.IUIAutomationSelectionItemPattern)
        if pat is None:
            raise HelperError("pattern_unsupported", "control has no SelectionItemPattern", control=self.describe(el))
        pat.Select()
        return {"control": self.describe(el), "state": self.element_state(el)}

    def cmd_state(self, args):
        """Every matching control with its state (e.g. which list rows are selected, which list has focus)."""
        wmatch = check_window(args.get("window"))
        out = []
        for el, _window in self.find_controls(wmatch, args.get("selector"))[:500]:
            out.append({**self.describe(el), **self.element_state(el)})
        return {"count": len(out), "controls": out}

    def cmd_expand(self, args):
        el = self.one(check_window(args.get("window")), args.get("selector"))
        pat = self.pattern(el, PAT_EXPAND, self.UIA.IUIAutomationExpandCollapsePattern)
        if pat is None:
            raise HelperError("pattern_unsupported", "control has no ExpandCollapsePattern", control=self.describe(el))
        (pat.Collapse if args.get("collapse") else pat.Expand)()
        return {"control": self.describe(el), "state": self.element_state(el)}

    def cmd_read_value(self, args):
        el = self.one(check_window(args.get("window")), args.get("selector"))
        pat = self.pattern(el, PAT_VALUE, self.UIA.IUIAutomationValuePattern)
        if pat is not None:
            return {"control": self.describe(el), "value": pat.CurrentValue, "source": "value_pattern"}
        info = self.describe(el)
        return {"control": info, "value": info["name"], "source": "name"}

    # -- menus
    def _menu_item(self, scopes, want, mode, timeout_s):
        end = time.time() + timeout_s
        condition = self.iuia.CreatePropertyCondition(P_CONTROL_TYPE, CONTROL_TYPE_IDS["MenuItem"])
        while True:
            found, seen = [], set()
            for scope in scopes():
                items = scope.FindAll(SCOPE_DESCENDANTS, condition)
                for index in range(items.Length):
                    el = items.GetElement(index)
                    if not name_matches(self._safe(lambda: el.CurrentName, ""), want, mode):
                        continue
                    key = self.runtime_id(el) or id(el)
                    if key not in seen:
                        seen.add(key)
                        found.append(el)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise HelperError("menu_item_ambiguous", f"{len(found)} menu items match {want!r}",
                                  matches=[self.describe(f) for f in found[:5]])
            if time.time() >= end:
                raise HelperError("menu_item_not_found", f"menu item {want!r} not found")
            time.sleep(0.15)

    def _expand(self, el):
        self._enabled(el)
        pat = self.pattern(el, PAT_EXPAND, self.UIA.IUIAutomationExpandCollapsePattern)
        if pat is not None:
            pat.Expand()
            return "expand"
        return self.invoke_element(el)

    def _native_menu(self, main, steps, invoke_leaf):
        """Walk the window's Win32 menu bar (wxMSW menus are native HMENUs) without opening any popup.

        UIA ExpandCollapse on a native menu bar needs a popup on the input desktop and fails with
        COR_E_INVALIDOPERATION on a private desktop, so menus are resolved here: GetMenu/GetSubMenu by
        name, WM_INITMENUPOPUP so wx runs its update-UI handlers (enable state, lazy items), then the
        leaf's enabled state from GetMenuState and, when invoking, WM_COMMAND with the leaf's command id.
        Returns None when the window has no native menu bar (caller falls back to UIA)."""
        user32 = ctypes.WinDLL("user32")
        vp, ui, lp = ctypes.c_void_p, ctypes.c_uint, ctypes.c_ssize_t
        user32.GetMenu.restype, user32.GetMenu.argtypes = vp, [vp]
        user32.GetSubMenu.restype, user32.GetSubMenu.argtypes = vp, [vp, ctypes.c_int]
        user32.GetMenuItemCount.restype, user32.GetMenuItemCount.argtypes = ctypes.c_int, [vp]
        user32.GetMenuStringW.argtypes = [vp, ui, ctypes.c_wchar_p, ctypes.c_int, ui]
        user32.GetMenuState.restype, user32.GetMenuState.argtypes = ui, [vp, ui, ui]
        user32.GetMenuItemID.restype, user32.GetMenuItemID.argtypes = ui, [vp, ctypes.c_int]
        user32.SendMessageTimeoutW.argtypes = [vp, ui, ctypes.c_size_t, lp, ui, ui, ctypes.POINTER(ctypes.c_size_t)]
        user32.PostMessageW.argtypes = [vp, ui, ctypes.c_size_t, lp]
        WM_INITMENUPOPUP, WM_UNINITMENUPOPUP, WM_COMMAND = 0x0117, 0x0125, 0x0111
        WM_ENTERMENULOOP, WM_EXITMENULOOP = 0x0211, 0x0212
        MF_BYPOSITION, MF_GRAYED, MF_DISABLED, MF_SEPARATOR, MF_POPUP = 0x400, 0x1, 0x2, 0x800, 0x10
        hwnd = self._safe(lambda: main.CurrentNativeWindowHandle, 0)
        if not hwnd:
            return None
        hmenu = user32.GetMenu(vp(hwnd))
        if not hmenu:
            return None

        def send(msg, wparam, lparam):
            out = ctypes.c_size_t()
            if not user32.SendMessageTimeoutW(vp(hwnd), msg, wparam, lparam, 0x0002, 5000, ctypes.byref(out)):
                raise HelperError("menu_message_timeout", f"window did not process message 0x{msg:04x}")

        described, opened = [], []
        send(WM_ENTERMENULOOP, 0, 0)
        loop_open = True
        try:
            for depth, (want, mode) in enumerate(steps):
                matches, seen = [], []
                for pos in range(max(user32.GetMenuItemCount(vp(hmenu)), 0)):
                    state = user32.GetMenuState(vp(hmenu), pos, MF_BYPOSITION)
                    if state == 0xFFFFFFFF:
                        continue
                    if state & MF_POPUP:  # high byte holds the submenu's item count, not flags
                        state &= 0xFF
                    if state & MF_SEPARATOR:
                        continue
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetMenuStringW(vp(hmenu), pos, buf, 512, MF_BYPOSITION)
                    seen.append(buf.value)
                    if name_matches(buf.value, want, mode):
                        matches.append((pos, buf.value, state))
                if not matches:
                    raise HelperError("menu_item_not_found", f"menu item {want!r} not found",
                                      available=[norm(s) for s in seen[:60]])
                if len(matches) > 1:
                    raise HelperError("menu_item_ambiguous", f"{len(matches)} menu items match {want!r}",
                                      matches=[m[1] for m in matches[:5]])
                pos, label, state = matches[0]
                enabled = not (state & (MF_GRAYED | MF_DISABLED))
                info = {"name": label, "automation_id": "", "control_type": "MenuItem", "class_name": "#32768",
                        "enabled": enabled, "offscreen": True, "norm_name": norm(label), "via": "native_menu"}
                described.append(info)
                if depth < len(steps) - 1:
                    sub = user32.GetSubMenu(vp(hmenu), pos)
                    if not sub:
                        raise HelperError("menu_not_submenu", f"menu item {want!r} has no submenu")
                    send(WM_INITMENUPOPUP, sub, pos)  # wx update-UI: enable state and lazily built items
                    opened.append(sub)
                    hmenu = sub
                    continue
                if state & MF_POPUP:
                    raise HelperError("menu_leaf_is_submenu", f"menu leaf {want!r} is a submenu")
                if invoke_leaf:
                    if not enabled:
                        raise HelperError("control_disabled", f"menu item {want!r} is disabled", control=info)
                    command_id = user32.GetMenuItemID(vp(hmenu), pos)
                    if command_id == 0xFFFFFFFF:
                        raise HelperError("menu_item_no_command", f"menu item {want!r} has no command id")
                    for sub in reversed(opened):
                        send(WM_UNINITMENUPOPUP, sub, 0)
                    opened = []
                    send(WM_EXITMENULOOP, 0, 0)  # close the menu loop as a real menu would, before the command
                    loop_open = False
                    if not user32.PostMessageW(vp(hwnd), WM_COMMAND, command_id & 0xFFFF, 0):
                        raise HelperError("menu_post_failed", f"could not post the command for {want!r}")
                    info["command_id"] = command_id
        finally:
            for sub in reversed(opened):
                try:
                    send(WM_UNINITMENUPOPUP, sub, 0)
                except HelperError:
                    pass
            if loop_open:
                try:
                    send(WM_EXITMENULOOP, 0, 0)
                except HelperError:
                    pass
        return {"steps": described, "leaf": described[-1], "invoked": invoke_leaf, "via": "native_menu"}

    def cmd_select_menu(self, args):
        window = check_window(args.get("window"))
        path = args.get("path")
        if not isinstance(path, list) or not path or len(path) > 6:
            raise HelperError("invalid_path", "path must be a list of 1-6 menu names")
        steps = []
        for item in path:
            if isinstance(item, str):
                item = {"name": item, "match": "exact"}
            if not isinstance(item, dict) or set(item) - {"name", "match"} or not isinstance(item.get("name"), str) \
                    or item.get("match", "exact") not in ("exact", "prefix"):
                raise HelperError("invalid_path", f"bad menu path entry {item!r}")
            steps.append((item["name"], item.get("match", "exact")))
        invoke_leaf = bool(args.get("invoke_leaf", False))
        windows = self.top_windows(window)
        if len(windows) != 1:
            raise HelperError("window_not_unique", f"{len(windows)} windows match the menu window")
        main = windows[0]
        native = self._native_menu(main, steps, invoke_leaf)
        if native is not None:
            return native
        described, expanded, scope = [], [], [main]
        for depth, (want, mode) in enumerate(steps):
            el = self._menu_item(lambda s=tuple(scope): s, want, mode, 2.5 if depth else 1.5)
            info = self.describe(el)
            info["norm_name"] = norm(info["name"])
            described.append(info)
            last = depth == len(steps) - 1
            if last:
                if invoke_leaf:
                    self._enabled(el)
                    info["via"] = self.invoke_element(el)
                break
            info["via"] = self._expand(el)
            expanded.append(el)
            scope = [el] + self.popup_menus()
        if not invoke_leaf:  # dry run: put the menus back
            for el in reversed(expanded):
                pat = self.pattern(el, PAT_EXPAND, self.UIA.IUIAutomationExpandCollapsePattern)
                if pat is not None:
                    self._safe(pat.Collapse)
        return {"steps": described, "leaf": described[-1], "invoked": invoke_leaf}

    def execute(self, command, args):
        handler = {"find": self.cmd_find, "dump-tree": self.cmd_dump_tree, "invoke": self.cmd_invoke,
                   "set-value": self.cmd_set_value, "read-value": self.cmd_read_value,
                   "select-menu": self.cmd_select_menu, "focus": self.cmd_focus, "state": self.cmd_state,
                   "expand": self.cmd_expand, "select": self.cmd_select}.get(command)
        if handler is None:
            raise HelperError("unknown_command", f"command must be one of {COMMANDS}: {command!r}")
        if not isinstance(args, dict):
            raise HelperError("invalid_args", "args must be an object")
        return handler(args)


# ---- process / desktop utilities and the main loop ----------------------------------------------

def thread_desktop_name() -> str:
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.GetThreadDesktop.restype = ctypes.c_void_p
    user32.GetThreadDesktop.argtypes = [ctypes.c_ulong]
    user32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                                 ctypes.POINTER(ctypes.c_ulong)]
    handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    buffer = ctypes.create_unicode_buffer(256)
    needed = ctypes.c_ulong()
    user32.GetUserObjectInformationW(handle, 2, buffer, 512, ctypes.byref(needed))
    return buffer.value


def process_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--site", action="append", default=[])
    parser.add_argument("--idle-timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    channel = Path(args.channel)
    for site in args.site:
        if site not in sys.path:
            sys.path.append(site)
    try:
        here = thread_desktop_name()
        if here != args.desktop:
            write_json_atomic(channel / "fatal.json", {"error": "wrong_desktop", "thread_desktop": here,
                                                       "expected": args.desktop})
            return 2
        uia = Uia(args.pid)
    except BaseException:
        write_json_atomic(channel / "fatal.json", {"error": "startup_failed", "trace": traceback.format_exc()[-4000:]})
        return 3
    write_json_atomic(channel / "ready.json", {"pid": os.getpid(), "target_pid": args.pid,
                                               "thread_desktop": here, "comtypes": uia.comtypes_version})
    last_activity = time.time()
    done: set[str] = set()  # command numbers already answered: never executed again
    while True:
        if (channel / "stop").exists():
            return 0
        if time.time() - last_activity > args.idle_timeout:
            return 0
        pending = sorted(channel.glob("cmd-*.json"))
        if not pending:
            if not process_alive(args.pid):
                return 0
            time.sleep(0.1)
            continue
        for path in pending:
            last_activity = time.time()
            handle_command(channel, path, uia, done)


if __name__ == "__main__":
    raise SystemExit(main())
