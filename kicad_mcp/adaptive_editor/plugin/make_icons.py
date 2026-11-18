"""Generate the toolbar icons (pure Python PNG writer; run once, output is committed with the plugin).

adaptive-drag: a footprint square pushing a second square (arrow between them).
adaptive-edge: a board outline with its right edge and an inward/outward double arrow.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "icons"


def _png(path: Path, size: int, pixels) -> None:
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)


def _canvas(size):
    return [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]


def _rect(px, x0, y0, x1, y1, color, fill=False, t=1):
    n = len(px)
    for y in range(max(0, y0), min(n, y1 + 1)):
        for x in range(max(0, x0), min(n, x1 + 1)):
            edge = x - x0 < t or x1 - x < t or y - y0 < t or y1 - y < t
            if fill or edge:
                px[y][x] = color


def _arrow_h(px, x0, x1, y, color, t, both=False):
    _rect(px, min(x0, x1), y - t // 2, max(x0, x1), y + (t - 1) // 2, color, fill=True)
    head = 2 * t + 1
    ends = [(x1, 1 if x1 > x0 else -1)] + ([(x0, -1 if x1 > x0 else 1)] if both else [])
    for tip, d in ends:  # filled triangle: one pixel wide at the tip, widening by one per column back
        for i in range(head):
            x = tip - d * i
            _rect(px, x, y - i, x, y + i, color, fill=True)


def drag(size, fg, accent):
    px = _canvas(size)
    u = size / 24
    t = max(1, round(1.5 * u))
    _rect(px, round(2 * u), round(7 * u), round(10 * u), round(16 * u), fg, t=t)
    _rect(px, round(15 * u), round(5 * u), round(22 * u), round(12 * u), accent, fill=True)
    _arrow_h(px, round(10 * u), round(14 * u), round(15 * u), accent, max(1, round(u)))
    return px


def edge(size, fg, accent):
    px = _canvas(size)
    u = size / 24
    t = max(1, round(1.5 * u))
    _rect(px, round(2 * u), round(4 * u), round(14 * u), round(20 * u), fg, t=t)
    _rect(px, round(14 * u), round(4 * u), round(14 * u) + t, round(20 * u), accent, fill=True)
    _arrow_h(px, round(16 * u), round(22 * u), round(12 * u), accent, max(1, round(u)), both=True)
    return px


def main() -> None:
    OUT.mkdir(exist_ok=True)
    themes = {"": ((40, 40, 40, 255), (230, 120, 20, 255)), "-dark": ((220, 220, 220, 255), (255, 150, 40, 255))}
    for name, fn in (("adaptive-drag", drag), ("adaptive-edge", edge)):
        for suffix, (fg, accent) in themes.items():
            for size in (24, 48):
                _png(OUT / f"{name}{suffix}-{size}.png", size, fn(size, fg, accent))


if __name__ == "__main__":
    main()
