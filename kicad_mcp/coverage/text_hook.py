"""Read the text a KiCad session paints (message panel, painted labels) by hooking GDI inside its process.

Some of what a person reads in KiCad is painted, not held in controls UI Automation can see: the message
panel under the canvas ("Mode: Shove", "Corner Style: 45-degree"), for one. This hook runs in the session's
own KiCad process (Frida, like ``launch_hook``) and records every string wxWidgets' device contexts draw
(``DoDrawText``; the GDI calls below them carry glyph indexes, not text). ``painted`` forces a repaint of a window and returns the strings drawn for it,
so two readings can be compared: what the panel showed before an action and after it.
"""
from __future__ import annotations

import ctypes
import threading
import time

SCRIPT = r"""
// wx draws text through its DC classes' DoDrawText(const wxString&, x, y); GDI below it only sees glyph
// indexes. A wxString on Windows holds a std::wstring first: inline buffer (or pointer) at 0, length at 16,
// capacity at 24.
function wxstr(p) {
    const len = p.add(16).readU64().toNumber(), cap = p.add(24).readU64().toNumber();
    if (len <= 0 || len > 2000) return null;
    return (cap >= 8 ? p.readPointer() : p).readUtf16String(len);
}
let hooked = 0;
Process.enumerateModules().filter(m => /^wxmsw.*core/.test(m.name)).forEach(m => {
    m.enumerateExports().filter(e => e.type === 'function' && /^\?DoDrawText@|^\?DoDrawRotatedText@/.test(e.name)).forEach(e => {
        Interceptor.attach(e.address, { onEnter(args) {
            try { const t = wxstr(args[1]); if (t) send({kind: 'text', text: t}); } catch (err) {}
        } });
        hooked++;
    });
});
send({kind: 'installed', count: hooked});
"""

RDW_INVALIDATE, RDW_ALLCHILDREN, RDW_UPDATENOW, RDW_ERASE = 0x0001, 0x0080, 0x0100, 0x0004


class TextHook:
    """Attach to one KiCad process; ``painted(hwnd)`` returns the strings a full repaint of ``hwnd`` draws."""

    def __init__(self, pid: int):
        import frida

        self._lock = threading.Lock()
        self._texts: list[str] = []
        self.installed = False
        self._session = frida.attach(pid)
        self._script = self._session.create_script(SCRIPT)
        self._script.on("message", self._on_message)
        self._script.load()
        deadline = time.monotonic() + 10
        while not self.installed and time.monotonic() < deadline:
            time.sleep(0.1)

    def _on_message(self, message, _data) -> None:
        if message.get("type") != "send":
            return
        payload = message["payload"]
        if payload.get("kind") == "installed":
            self.installed = True
        elif payload.get("kind") == "text":
            with self._lock:
                self._texts.append(payload["text"])

    def painted(self, hwnd: int, settle_s: float = 0.8) -> set[str]:
        with self._lock:
            self._texts.clear()
        ctypes.windll.user32.RedrawWindow(ctypes.c_void_p(hwnd), None, None,
                                          RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
        time.sleep(settle_s)
        with self._lock:
            return {t.strip() for t in self._texts if t.strip()}

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()
