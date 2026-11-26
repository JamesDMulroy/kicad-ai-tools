"""Drop files onto one KiCad window, the way Explorer does, from inside the session's own process.

A file dropped on a KiCad frame arrives as ``WM_DROPFILES`` whose handle is a block of global memory (a
``DROPFILES`` header and the paths) owned by the receiving process. That block cannot be made from another
process, so this hook (Frida, in the session's KiCad process only; nothing on the user's desktop is involved)
allocates it there and posts the message to the frame, which runs the action registered for the file's extension
(``pcbnew.Control.DdAppendBoard`` for a board, ``ddImportFootprint`` for a footprint, and so on).
"""
from __future__ import annotations

SCRIPT = r"""
const k = Process.getModuleByName('kernel32.dll');
const u = Process.getModuleByName('user32.dll');
const GlobalAlloc = new NativeFunction(k.getExportByName('GlobalAlloc'), 'pointer', ['uint', 'size_t']);
const GlobalLock = new NativeFunction(k.getExportByName('GlobalLock'), 'pointer', ['pointer']);
const GlobalUnlock = new NativeFunction(k.getExportByName('GlobalUnlock'), 'int', ['pointer']);
const PostMessageW = new NativeFunction(u.getExportByName('PostMessageW'), 'int', ['pointer', 'uint', 'pointer', 'pointer']);
rpc.exports = {
    drop(hwnd, paths) {
        // DROPFILES header (20 bytes), then each path NUL-terminated, then an empty path ending the list
        const size = 20 + paths.reduce((n, p) => n + (p.length + 1) * 2, 0) + 4;
        const handle = GlobalAlloc(0x42, size);           // GMEM_MOVEABLE | GMEM_ZEROINIT
        const block = GlobalLock(handle);
        block.writeU32(20);                               // pFiles: offset of the path list
        block.add(16).writeU32(1);                        // fWide: UTF-16 paths
        let offset = 20;
        for (const p of paths) {
            block.add(offset).writeUtf16String(p);
            offset += (p.length + 1) * 2;
        }
        GlobalUnlock(handle);
        return PostMessageW(ptr(hwnd), 0x0233, handle, ptr(0));  // WM_DROPFILES; the receiver frees it (DragFinish)
    }
};
"""


class DropHook:
    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(SCRIPT)
        self._script.load()

    def drop(self, hwnd: int, paths: list[str]) -> bool:
        """Post a drop of ``paths`` on ``hwnd`` (a KiCad frame)."""
        return bool(self._script.exports_sync.drop(int(hwnd), [str(p) for p in paths]))

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()
