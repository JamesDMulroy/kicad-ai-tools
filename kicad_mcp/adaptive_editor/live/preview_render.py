"""Instant preview of an engine result from the neutral model (pure Python, no pcbnew).

Draws the board outline (changed edges: old dashed + new yellow), every part courtyard (moved parts:
original dashed grey + new position filled orange), tracks (changed tracks: old dashed grey + new
orange; bends shown), and vias. Used by the live panel so preview never touches the live board
(KiCad 10.0.5 IPC staged commits are not visible until pushed).

``primitives()`` returns backend-neutral drawing primitives in mm (the Tk panel draws these on a
canvas); ``render()`` turns the same primitives into an SVG string.
"""
from __future__ import annotations

from typing import Any

NM_PER_MM = 1_000_000


def _mm(p, dx=0, dy=0):
    return ((p[0] + dx) / NM_PER_MM, (p[1] + dy) / NM_PER_MM)


def primitives(board, changeset: dict[str, Any]) -> dict[str, Any]:
    """``{"bbox": [x0, y0, w, h], "items": [...]}`` in mm. Item kinds: line, poly, circle, text."""
    moves = {m["uuid"]: (m["dx"], m["dy"]) for m in changeset.get("part_moves", [])}
    vmoves = {m["uuid"]: (m["dx"], m["dy"]) for m in changeset.get("via_moves", [])}
    tchg = {t["uuid"]: t for t in changeset.get("track_changes", [])}
    echg = {e["uuid"]: e for e in changeset.get("edge_changes", [])}
    xs, ys = [], []
    for poly in board.outline.polygons:
        for p in poly:
            xs.append(p[0]); ys.append(p[1])
    for part in board.parts:
        xs.append(part.position[0]); ys.append(part.position[1])
    if not xs:
        xs, ys = [0, NM_PER_MM], [0, NM_PER_MM]
    pad = 3 * NM_PER_MM
    bbox = [(min(xs) - pad) / NM_PER_MM, (min(ys) - pad) / NM_PER_MM,
            (max(xs) - min(xs) + 2 * pad) / NM_PER_MM, (max(ys) - min(ys) + 2 * pad) / NM_PER_MM]
    items: list[dict[str, Any]] = []

    def line(pts, color, width, dash=None):
        items.append({"kind": "line", "pts": pts, "color": color, "width": width, "dash": dash})

    for e in board.outline.edges:
        c = echg.get(e.uuid)
        if c:
            line([_mm(e.start), _mm(e.end)], "#777777", 0.15, (0.6, 0.4))
            line([_mm(c["start"]), _mm(c["end"])], "#ffcc33", 0.25)
        else:
            line([_mm(e.start), _mm(e.end)], "#e0e000", 0.2)
    for tr in board.tracks:
        w = max(tr.width / NM_PER_MM, 0.1)
        c = tchg.get(tr.uuid)
        if c:
            line([_mm(tr.start), _mm(tr.end)], "#666666", w, (0.5, 0.4))
            pts = [c["start"]] + ([c["bend"]] if c.get("bend") else []) + [c["end"]]
            line([_mm(p) for p in pts], "#ff8c1a", w)
        else:
            line([_mm(tr.start), _mm(tr.end)], "#c83434", w)
    for v in board.vias:
        dx, dy = vmoves.get(v.uuid, (0, 0))
        items.append({"kind": "circle", "center": _mm(v.pos, dx, dy), "r": v.diameter / 2 / NM_PER_MM,
                      "fill": "#ff8c1a" if v.uuid in vmoves else "#c8c8c8"})
    for part in board.parts:
        d = moves.get(part.uuid)
        for poly in part.courtyard:
            if d:
                items.append({"kind": "poly", "pts": [_mm(p) for p in poly], "stroke": "#777777",
                              "width": 0.08, "dash": (0.4, 0.3), "fill": None, "opacity": 0})
                items.append({"kind": "poly", "pts": [_mm(p, *d) for p in poly], "stroke": "#ff8c1a",
                              "width": 0.1, "dash": None, "fill": "#ff8c1a", "opacity": 0.35})
            else:
                items.append({"kind": "poly", "pts": [_mm(p) for p in poly], "stroke": "#6fa8dc",
                              "width": 0.08, "dash": None, "fill": "#3a6ea5", "opacity": 0.25})
        dx, dy = d or (0, 0)
        items.append({"kind": "text", "at": _mm(part.position, dx, dy), "text": part.ref, "size": 1.0,
                      "color": "#ffffff"})
    return {"bbox": bbox, "items": items}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def svg_from_primitives(prims: dict[str, Any], *, size_px: int = 900) -> str:
    x0, y0, w, h = prims["bbox"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size_px}" height="{int(size_px * h / max(w, 1e-9))}" '
           f'viewBox="{x0:.3f} {y0:.3f} {w:.3f} {h:.3f}" style="background:#101418">']
    for it in prims["items"]:
        k = it["kind"]
        if k == "line":
            d = "M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in it["pts"])
            dash = f' stroke-dasharray="{it["dash"][0]},{it["dash"][1]}"' if it["dash"] else ""
            out.append(f'<path d="{d}" fill="none" stroke="{it["color"]}" stroke-width="{it["width"]}"'
                       f' stroke-linecap="round"{dash}/>')
        elif k == "poly":
            if not it["pts"]:
                continue
            d = "M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in it["pts"]) + " Z"
            fill = (f'fill="{it["fill"]}" fill-opacity="{it["opacity"]}"' if it["fill"] else 'fill="none"')
            dash = f' stroke-dasharray="{it["dash"][0]},{it["dash"][1]}"' if it["dash"] else ""
            out.append(f'<path d="{d}" {fill} stroke="{it["stroke"]}" stroke-width="{it["width"]}"{dash}/>')
        elif k == "circle":
            cx, cy = it["center"]
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{it["r"]}" fill="{it["fill"]}"/>')
        elif k == "text":
            x, y = it["at"]
            out.append(f'<text x="{x}" y="{y}" font-size="{it["size"]}" fill="{it["color"]}" '
                       f'text-anchor="middle">{_esc(it["text"])}</text>')
    out.append("</svg>")
    return "".join(out)


def render(board, changeset: dict[str, Any], *, size_px: int = 900) -> str:
    return svg_from_primitives(primitives(board, changeset), size_px=size_px)
