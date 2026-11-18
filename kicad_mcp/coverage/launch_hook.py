"""Intercept the programs a KiCad session asks Windows to start (browser, Explorer, text editor).

Actions like Help, Donate, Report Bug, "Reveal in Explorer" or "Edit in a Text Editor" hand a URL, a folder
or a file to another program. Run for real they would open windows on the owner's screen, which the
silent-launch rule forbids. This hook runs inside the session's own KiCad process (Frida): on
``ShellExecuteExW``/``ShellExecuteW`` (the browser and Explorer route) and on ``CreateProcessW`` for any
program outside KiCad's own install it records exactly what KiCad asked Windows to open, then points the
call at a path that does not exist, so Windows fails it and nothing starts. KiCad's own programs
(viewers, calculator) still start, on the private desktop.

A request recorded here is the proof that the action did its part: it asked Windows to open the right
URL, folder or file with the right program. Callers must check ``installed`` before pressing anything.
"""
from __future__ import annotations

import time

SCRIPT = r"""
function norm(s) { return s.split('/').join('\\').toLowerCase(); }
const kicadRoot = norm(%KICAD_ROOT%);
// kernelbase: kernel32's CreateProcessW forwards here, and newer binaries import it from here directly
const k32 = Process.getModuleByName('kernelbase.dll');
const sh32 = Process.getModuleByName('shell32.dll');
function wstr(p) { return p.isNull() ? '' : p.readUtf16String(); }
// Every outside launch is pointed at a path that does not exist: Windows fails it, nothing starts, and
// KiCad reports the failure in its own (private-desktop) message box. The real target is recorded first.
const blocked = Memory.allocUtf16String('C:\kicad-ai-tools-blocked-launch\does-not-exist.exe');  // portability-ok: deliberately non-existent target
globalThis.blocked = blocked;
const installed = [];

Interceptor.attach(sh32.getExportByName('ShellExecuteExW'), {
    onEnter(args) {
        const info = args[0];
        send({kind: 'ShellExecuteExW', verb: wstr(info.add(16).readPointer()), file: wstr(info.add(24).readPointer()),
              params: wstr(info.add(32).readPointer())});
        info.add(4).writeU32(info.add(4).readU32() | 0x400);   // SEE_MASK_FLAG_NO_UI: no Windows error box
        info.add(24).writePointer(blocked);
        info.add(32).writePointer(ptr(0));
    }
});
installed.push('ShellExecuteExW');

Interceptor.attach(sh32.getExportByName('ShellExecuteW'), {
    onEnter(args) {
        send({kind: 'ShellExecuteW', verb: wstr(args[1]), file: wstr(args[2]), params: wstr(args[3])});
        args[2] = blocked;
        args[3] = ptr(0);
    }
});
installed.push('ShellExecuteW');

Interceptor.attach(k32.getExportByName('CreateProcessW'), {
    onEnter(args) {
        const line = (wstr(args[0]) + ' ' + wstr(args[1])).trim();
        if (norm(line).indexOf(kicadRoot) >= 0) {   // KiCad's own program: recorded, and let it start
            send({kind: 'CreateProcessW', command: line, allowed: true});
            return;
        }
        send({kind: 'CreateProcessW', command: line});
        args[0] = blocked;
        args[1] = ptr(0);
    }
});
installed.push('CreateProcessW');
send({kind: 'installed', hooks: installed});
"""


class LaunchHook:
    """Attach to one KiCad process; ``requests`` lists what it asked Windows to start."""

    def __init__(self, pid: int, kicad_root: str):
        import json

        import frida

        self.requests: list[dict] = []
        self.installed: list[str] = []
        self._session = frida.attach(pid)
        self._script = self._session.create_script(SCRIPT.replace("%KICAD_ROOT%", json.dumps(str(kicad_root))))
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
            self.installed = payload["hooks"]
        else:
            self.requests.append(payload)

    @property
    def ready(self) -> bool:
        return {"ShellExecuteExW", "ShellExecuteW", "CreateProcessW"} <= set(self.installed)

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()
