"""Adaptive Editor panel: live preview + one-step apply for KiCad 10 (IPC API).

``Controller`` holds all logic and is UI-free (tested headlessly against a hidden KiCad). ``Panel``
is a Tk window over it: drag on the preview (or use the sliders) to set the displacement; every
change re-solves from the baseline snapshot and redraws the preview instantly. Nothing touches the
board until Apply, which writes the whole plan in ONE commit (one Ctrl+Z). Cancel writes nothing.

KiCad 10.0.5 does not render uncommitted IPC changes (tests_live/probe_commit_visibility.py), so the
preview is drawn here from the engine's neutral model instead of on the KiCad canvas.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _engine_paths() -> list[Path]:
    """Installed layout: <plugin>/engine/{live,adaptive_engine}; source tree: kicad_mcp/adaptive_editor."""
    installed = HERE / "engine"
    if (installed / "live" / "live_session.py").is_file():
        return [installed / "live", installed]
    dev = HERE.parent  # kicad_mcp/adaptive_editor in a source checkout
    return [dev / "live", dev]


for _p in reversed(_engine_paths()):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from live_session import LiveError, LiveSession  # noqa: E402

NM = 1_000_000
SIDES_FOR = {"vertical": ("left", "right"), "horizontal": ("top", "bottom")}
NORMALS = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


class PanelError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _uuid(item) -> str:
    return str(item.id.value)


def _is_edge_cuts(item) -> bool:
    layer = getattr(item, "layer", None)
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer
        return layer == BoardLayer.BL_Edge_Cuts
    except Exception:  # pragma: no cover - older kipy
        return "Edge" in str(layer)


class Controller:
    """UI-free adaptive session over a live board.

    kind 'drag': driver = selected footprints. kind 'edge': driver = the one selected Edge.Cuts
    segment; ``set_displacement`` then takes the outward distance (positive grows the board)."""

    def __init__(self, board, kind: str, spawn=None):
        if kind not in ("drag", "edge"):
            raise PanelError("bad_kind", kind)
        self.board, self.kind, self.spawn = board, kind, spawn
        self.refs: dict[str, str] = {}        # reference -> uuid
        self.driver: dict = {}
        self.side: str | None = None
        self.dx_nm = self.dy_nm = 0
        self.options = {"mode": "cascade", "copper": "adapt", "allow_partial": True,
                        "frozen": [], "priority": [], "movers": [], "eligibility_mm": None}
        self.session: LiveSession | None = None
        self.last: dict | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        for fp in self.board.get_footprints():
            self.refs[fp.reference_field.text.value] = _uuid(fp)
        sel = list(self.board.get_selection())
        if self.kind == "drag":
            uuids = [_uuid(i) for i in sel if hasattr(i, "reference_field")]
            if not uuids:
                raise PanelError("no_selection", "select one or more footprints first")
            self.driver = {"kind": "part", "uuids": uuids}
        else:
            edges = [i for i in sel if hasattr(i, "start") and hasattr(i, "end") and _is_edge_cuts(i)]
            if len(edges) != 1:
                raise PanelError("no_selection", "select exactly one straight Edge.Cuts segment first")
            self.driver = {"kind": "edge", "edge_uuid": _uuid(edges[0]), "side": None}
        self.session = LiveSession(self.board, spawn=self.spawn)
        info = self.session.start()
        if self.kind == "edge":
            self.side = self._find_side()
            self.driver["side"] = self.side
        return {"driver": self.driver, "worker": info}

    def _find_side(self) -> str:
        edge = next((e for e in self.session.edges() if e["uuid"] == self.driver["edge_uuid"]), None)
        if edge is None:
            raise PanelError("unknown_edge", "the selected segment is not part of the board outline")
        if edge["start"][0] == edge["end"][0]:
            candidates = SIDES_FOR["vertical"]
        elif edge["start"][1] == edge["end"][1]:
            candidates = SIDES_FOR["horizontal"]
        else:
            raise PanelError("not_axis_aligned", "only horizontal or vertical outline edges can be moved")
        for side in candidates:  # the engine checks the side before the displacement
            probe = dict(self._request(), driver={**self.driver, "side": side}, displacement_nm=[0, 0])
            code = (self.session.update(probe, render=False)["report"] or {}).get("reject_reason")
            if code != "side_mismatch":
                return side
        raise PanelError("unsupported_topology", "could not determine which side of the board this edge is")

    # ---------------------------------------------------------------- request
    def set_displacement(self, a_mm: float, b_mm: float = 0.0) -> None:
        """drag: (dx, dy) in mm. edge: a_mm = outward distance (b_mm ignored)."""
        if self.kind == "edge":
            nx, ny = NORMALS[self.side]
            d = round(a_mm * NM)
            self.dx_nm, self.dy_nm = nx * d, ny * d
        else:
            self.dx_nm, self.dy_nm = round(a_mm * NM), round(b_mm * NM)

    def set_options(self, **kw) -> None:
        for k, v in kw.items():
            if k not in self.options:
                raise PanelError("bad_option", k)
            self.options[k] = v

    def _uuids_for(self, refs) -> list[str]:
        missing = [r for r in refs if r not in self.refs]
        if missing:
            raise PanelError("unknown_reference", ", ".join(missing))
        return [self.refs[r] for r in refs]

    def _request(self) -> dict:
        o = self.options
        elig = o["eligibility_mm"]
        return {"driver": self.driver, "displacement_nm": [self.dx_nm, self.dy_nm], "mode": o["mode"],
                "movers": self._uuids_for(o["movers"]), "frozen": self._uuids_for(o["frozen"]),
                "priority": self._uuids_for(o["priority"]),
                "eligibility_nm": None if elig in (None, "") else round(float(elig) * NM),
                "allow_partial": bool(o["allow_partial"]), "copper": o["copper"]}

    # ---------------------------------------------------------------- solve / apply
    def preview(self, render="prims") -> dict:
        if self.session is None:
            raise PanelError("not_started")
        self.last = self.session.update(self._request(), render=render)
        return self.last

    def summary(self) -> str:
        if not self.last:
            return ""
        cs, rep = self.last["changeset"], self.last.get("report") or {}
        ref_of = {u: r for r, u in self.refs.items()}
        moved = [f"{ref_of.get(m['uuid'], m['uuid'][:8])} ({m['dx'] / NM:+.2f}, {m['dy'] / NM:+.2f})"
                 for m in cs.get("part_moves", [])]
        lines = [f"status: {self.last.get('status')}"
                 + (f"  ({rep.get('reject_reason')})" if rep.get("reject_reason") else "")]
        if rep.get("requested_nm"):
            lines.append(f"achieved {rep.get('achieved_nm', 0) / NM:.3f} of {rep['requested_nm'] / NM:.3f} mm")
        if moved:
            lines.append("parts: " + ", ".join(moved))
        if cs.get("track_changes"):
            lines.append(f"tracks re-aimed: {len(cs['track_changes'])}")
        if cs.get("via_moves"):
            lines.append(f"vias moved: {len(cs['via_moves'])}")
        if cs.get("edge_changes"):
            lines.append(f"outline edges changed: {len(cs['edge_changes'])}")
        for b in (rep.get("blockers") or [])[:6]:
            lines.append("blocked by: " + json.dumps(b)[:200])
        for w in (rep.get("warnings") or [])[:6]:
            lines.append("warning: " + str(w))
        return "\n".join(lines)

    def apply(self, refill: bool = False) -> dict:
        if self.session is None or not self.last:
            raise PanelError("nothing_to_apply")
        if self.last.get("status") in ("rejected", "blocked"):
            raise PanelError("not_applicable", f"the plan was {self.last.get('status')}")
        label = "Adaptive edge move" if self.kind == "edge" else "Adaptive drag"
        out = self.session.apply(label, refill=refill)
        self.session = None
        return out

    def cancel(self) -> None:
        if self.session is not None:
            self.session.cancel()
            self.session = None


# -------------------------------------------------------------------- Tk panel


class Panel:
    CANVAS_W, CANVAS_H = 720, 480

    def __init__(self, root, controller: Controller):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.root, self.c = tk, root, controller
        self._pending = None
        self._drag_from = None
        self._base = (0.0, 0.0)
        self.view = None  # (scale, ox, oy) world mm -> canvas px
        edge = controller.kind == "edge"
        root.title("Adaptive Edge Move" if edge else "Adaptive Drag")
        frm = ttk.Frame(root, padding=6); frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1); root.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(frm, width=self.CANVAS_W, height=self.CANVAS_H, background="#101418",
                                highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=12, sticky="nsew", padx=(0, 8))
        frm.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        r = 0
        self.a = tk.DoubleVar(value=0.0); self.b = tk.DoubleVar(value=0.0)
        ttk.Label(frm, text="Outward distance (mm)" if edge else "dx (mm)").grid(row=r, column=1, sticky="w"); r += 1
        ttk.Scale(frm, from_=-30, to=30, variable=self.a, command=lambda v: self._changed()).grid(row=r, column=1, sticky="ew")
        ttk.Entry(frm, textvariable=self.a, width=8).grid(row=r, column=2); r += 1
        if not edge:
            ttk.Label(frm, text="dy (mm)").grid(row=r, column=1, sticky="w"); r += 1
            ttk.Scale(frm, from_=-30, to=30, variable=self.b, command=lambda v: self._changed()).grid(row=r, column=1, sticky="ew")
            ttk.Entry(frm, textvariable=self.b, width=8).grid(row=r, column=2); r += 1
        self.mode = tk.StringVar(value="cascade"); self.copper = tk.StringVar(value="adapt")
        self.partial = tk.BooleanVar(value=True)
        box = ttk.Frame(frm); box.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        for v in ("cascade", "explicit"):
            ttk.Radiobutton(box, text=v, value=v, variable=self.mode, command=self._changed).pack(side="left")
        box = ttk.Frame(frm); box.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        ttk.Label(box, text="copper:").pack(side="left")
        for v in ("adapt", "fixed"):
            ttk.Radiobutton(box, text=v, value=v, variable=self.copper, command=self._changed).pack(side="left")
        ttk.Checkbutton(frm, text="stop at limit (partial)", variable=self.partial, command=self._changed).grid(
            row=r, column=1, columnspan=2, sticky="w"); r += 1
        self.fields = {}
        for key, label in (("frozen", "Frozen refs"), ("priority", "Priority refs"), ("movers", "Movers (explicit)"),
                           ("eligibility_mm", "Eligibility (mm)")):
            ttk.Label(frm, text=label).grid(row=r, column=1, sticky="w")
            var = tk.StringVar(); ent = ttk.Entry(frm, textvariable=var, width=18)
            ent.grid(row=r, column=2, sticky="ew"); ent.bind("<Return>", lambda e: self._changed())
            ent.bind("<FocusOut>", lambda e: self._changed())
            self.fields[key] = var; r += 1
        self.status = tk.Text(frm, width=44, height=9, wrap="word", background="#f4f4f4")
        self.status.grid(row=r, column=1, columnspan=2, sticky="nsew"); frm.rowconfigure(r, weight=1); r += 1
        btns = ttk.Frame(frm); btns.grid(row=r, column=1, columnspan=2, sticky="e")
        self.refill = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="refill zones", variable=self.refill).pack(side="left")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Apply", command=self._apply).pack(side="right", padx=4)
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.prims = None
        self._solve()

    # -- input
    def _press(self, ev):
        self._drag_from = (ev.x, ev.y)
        self._base = (self.a.get(), self.b.get())

    def _motion(self, ev):
        if not self._drag_from or not self.view:
            return
        s = self.view[0]
        dx = (ev.x - self._drag_from[0]) / s
        dy = (ev.y - self._drag_from[1]) / s
        if self.c.kind == "edge":
            nx, ny = NORMALS[self.c.side]
            self.a.set(round(self._base[0] + dx * nx + dy * ny, 2))
        else:
            self.a.set(round(self._base[0] + dx, 2)); self.b.set(round(self._base[1] + dy, 2))
        self._changed()

    def _changed(self):
        if self._pending is not None:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(60, self._solve)  # debounce: solve at most ~16x per second

    def _refs(self, key):
        return [r.strip() for r in self.fields[key].get().replace(";", ",").split(",") if r.strip()]

    def _solve(self):
        self._pending = None
        try:
            self.c.set_displacement(float(self.a.get()), float(self.b.get()))
            elig = self.fields["eligibility_mm"].get().strip()
            self.c.set_options(mode=self.mode.get(), copper=self.copper.get(), allow_partial=self.partial.get(),
                               frozen=self._refs("frozen"), priority=self._refs("priority"),
                               movers=self._refs("movers"), eligibility_mm=elig or None)
            res = self.c.preview(render="prims")
            self.prims = res.get("prims")
            self._set_status(self.c.summary())
        except (PanelError, LiveError, ValueError) as exc:
            self._set_status(f"error: {exc}")
        self._redraw()

    def _set_status(self, text):
        self.status.configure(state="normal"); self.status.delete("1.0", "end")
        self.status.insert("end", text); self.status.configure(state="disabled")

    # -- drawing
    def _redraw(self):
        draw(self.canvas, self.prims, self)

    # -- buttons
    def _apply(self):
        try:
            out = self.c.apply(refill=self.refill.get())
            self._set_status(f"applied: {json.dumps(out)}")
            self.root.after(400, self.root.destroy)
        except (PanelError, LiveError) as exc:
            self._set_status(f"not applied: {exc}")

    def _cancel(self):
        self.c.cancel()
        self.root.destroy()


def draw(canvas, prims, holder=None) -> int:
    """Draw engine preview primitives (mm) onto a Tk canvas, fit to size. Returns the item count."""
    canvas.delete("all")
    if not prims:
        return 0
    w = max(int(canvas.winfo_width()), int(canvas.cget("width")))
    h = max(int(canvas.winfo_height()), int(canvas.cget("height")))
    x0, y0, bw, bh = prims["bbox"]
    s = min(w / max(bw, 1e-9), h / max(bh, 1e-9))
    ox, oy = (w - bw * s) / 2 - x0 * s, (h - bh * s) / 2 - y0 * s
    if holder is not None:
        holder.view = (s, ox, oy)
    P = lambda p: (p[0] * s + ox, p[1] * s + oy)  # noqa: E731
    n = 0
    for it in prims["items"]:
        k = it["kind"]
        if k == "line":
            pts = [c for p in it["pts"] for c in P(p)]
            dash = tuple(max(1, int(d * s)) for d in it["dash"]) if it["dash"] else None
            canvas.create_line(*pts, fill=it["color"], width=max(1.0, it["width"] * s), capstyle="round",
                               dash=dash)
        elif k == "poly":
            if len(it["pts"]) < 2:
                continue
            pts = [c for p in it["pts"] for c in P(p)]
            dash = tuple(max(1, int(d * s)) for d in it["dash"]) if it["dash"] else None
            fill = it["fill"] if it["fill"] and it["opacity"] > 0 else ""
            canvas.create_polygon(*pts, outline=it["stroke"], fill=fill, width=max(1.0, it["width"] * s),
                                  dash=dash, stipple="gray50" if fill and it["opacity"] < 0.5 else "")
        elif k == "circle":
            (cx, cy), r = P(it["center"]), it["r"] * s
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=it["fill"], outline="")
        elif k == "text":
            x, y = P(it["at"])
            canvas.create_text(x, y, text=it["text"], fill=it["color"],
                               font=("Segoe UI", max(7, int(it["size"] * s * 0.8))))
        else:
            continue
        n += 1
    return n


def main(kind: str) -> int:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        from kipy import KiCad
        kicad = KiCad()  # KICAD_API_SOCKET / KICAD_API_TOKEN come from KiCad's plugin launcher
        board = kicad.get_board()
        ctl = Controller(board, kind)
        ctl.start()
    except Exception as exc:  # report to the user who pressed the button, then exit
        messagebox.showerror("Adaptive Editor", f"Could not start: {exc}", parent=root)
        root.destroy()
        return 1
    Panel(root, ctl)
    root.deiconify()
    root.mainloop()
    return 0


def selftest(prims_path: str, out_path: str) -> int:
    """Headless check of the Tk drawing: draw primitives on a never-shown canvas and report.
    Run only on a private desktop (hidden_kicad.run_hidden); the root window is never mapped."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    canvas = tk.Canvas(root, width=Panel.CANVAS_W, height=Panel.CANVAS_H)
    prims = json.loads(Path(prims_path).read_text(encoding="utf-8"))
    n = draw(canvas, prims)
    kinds = sorted({canvas.type(i) for i in canvas.find_all()})
    ps = canvas.postscript(colormode="color", width=Panel.CANVAS_W, height=Panel.CANVAS_H)
    Path(out_path).write_text(json.dumps({"drawn": n, "canvas_items": len(canvas.find_all()), "kinds": kinds,
                                          "postscript_bytes": len(ps)}), encoding="utf-8")
    Path(out_path).with_suffix(".ps").write_text(ps, encoding="utf-8")
    root.destroy()
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "drag"))
