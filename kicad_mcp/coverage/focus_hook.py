"""Keyboard focus inside one KiCad session, for actions that move it between controls.

A window on the session's private desktop is never the active one, so Windows does not give its controls
keyboard focus and an action that asks "which control has focus?" (``GetFocus``) gets nothing. This hook,
in the session's own process only, answers ``GetFocus`` with the control the sweep says a person is in,
and records every ``SetFocus`` KiCad makes: the control the action moves focus to.
"""
from __future__ import annotations

import threading
import time

SCRIPT = r"""
let focus = null;
const u = Process.getModuleByName('user32.dll');
Interceptor.attach(u.getExportByName('GetFocus'), { onLeave(ret) { if (focus) ret.replace(ptr(focus)); } });
Interceptor.attach(u.getExportByName('SetFocus'), {
    onEnter(args) { send({kind: 'setfocus', hwnd: args[0].toString()}); if (focus && !args[0].isNull()) focus = args[0].toString(); }
});
rpc.exports = { set(h) { focus = h ? ('0x' + h.toString(16)) : null; } };
"""


class FocusHook:
    def __init__(self, pid: int):
        import frida

        self._lock = threading.Lock()
        self.calls: list[int] = []
        self._session = frida.attach(pid)
        self._script = self._session.create_script(SCRIPT)
        self._script.on("message", self._on_message)
        self._script.load()

    def _on_message(self, message, _data) -> None:
        if message.get("type") == "send" and message["payload"].get("kind") == "setfocus":
            with self._lock:
                self.calls.append(int(message["payload"]["hwnd"], 16))

    def focus(self, hwnd: int | None) -> None:
        """Report ``hwnd`` as the focused control (None: stop answering)."""
        self._script.exports_sync.set(hwnd or 0)
        with self._lock:
            self.calls.clear()

    def set_calls(self, wait_s: float = 1.0) -> list[int]:
        time.sleep(wait_s)
        with self._lock:
            return list(self.calls)

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()
