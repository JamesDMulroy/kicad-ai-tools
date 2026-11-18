"""Tell one KiCad session where its mouse is, inside its own process only.

KiCad reads the screen cursor (``GetCursorPos``) when it handles a key: the action under the cursor (net
highlight, next net item, unit switch...) then works at that point. On the session's private desktop there
is no real mouse there, so posted mouse moves put KiCad's cursor on an item, and the next key press moves it
back to wherever the system cursor happens to be. This hook (Frida, in the session's KiCad process only;
the owner's real mouse is never moved or read differently) makes ``GetCursorPos``/``GetMessagePos`` report
the point the sweep put the mouse on, the way a person's hand would keep it there.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

SCRIPT = r"""
let point = null;
const u = Process.getModuleByName('user32.dll');
['GetCursorPos', 'GetPhysicalCursorPos'].forEach(name => {
    const f = u.findExportByName(name);
    if (!f) return;
    Interceptor.attach(f, {
        onEnter(args) { this.out = args[0]; },
        onLeave(ret) { if (point) { this.out.writeS32(point[0]); this.out.add(4).writeS32(point[1]); ret.replace(1); } }
    });
});
Interceptor.attach(u.getExportByName('GetMessagePos'), {
    onLeave(ret) { if (point) ret.replace(((point[1] & 0xFFFF) << 16) | (point[0] & 0xFFFF)); }
});
let left = false;  // the left button, as KiCad reads it while a drag is driven
let right = false;  // the right button, for a context-menu click
['GetKeyState', 'GetAsyncKeyState'].forEach(name => {
    Interceptor.attach(u.getExportByName(name), {
        onEnter(args) { this.key = args[0].toInt32(); },
        onLeave(ret) {
            if (this.key === 1) ret.replace(left ? 0xFFFF8000 : 0);
            else if (this.key === 2) ret.replace(right ? 0xFFFF8000 : 0);
        }
    });
});
rpc.exports = { set(x, y) { point = [x, y]; }, clear() { point = null; }, button(down) { left = !!down; },
                rbutton(down) { right = !!down; } };
"""


class CursorHook:
    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(SCRIPT)
        self._script.load()

    def place(self, hwnd: int, x: int, y: int) -> None:
        """Report the client point (x, y) of ``hwnd`` as the mouse position."""
        point = wintypes.POINT(x, y)
        ctypes.windll.user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point))
        self._script.exports_sync.set(point.x, point.y)

    def button(self, down: bool) -> None:
        """Report the left mouse button held (a drag) or released."""
        self._script.exports_sync.button(bool(down))

    def right_button(self, down: bool) -> None:
        """Report the right mouse button held (a context-menu click) or released."""
        self._script.exports_sync.rbutton(bool(down))

    def clear(self) -> None:
        self._script.exports_sync.clear()

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()
