"""Layout gate for all generated sheets: text/text, text/wire, text/pin, text/symbol-body, text/box-edge, box/box and body/box-edge overlaps.

Text extents are the real ink boxes of the KiCad-exported SVG (stroked-text paths), so they include every visible string
(labels, pin names and numbers, reference/value fields, notes, box titles). Wires, pins, symbol bodies and the dashed group
boxes are read from the .kicad_sch files.

Diagnostic tools call
the pure readers and check() below; CLI paths resolve through kicad_env.
"""
import glob
import math
import os
import re
import subprocess
import sys
import tempfile

CLEAR = 0.3     # mm of clear air required between two text ink boxes
TOL = 0.12      # mm of ink penetration that is tolerated (stroke width and rounding)


# ------------------------------------------------------------------ s-expression reader
def parse(text):
    tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
    stack = [[]]
    for m in tok.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            x = stack.pop()
            stack[-1].append(x)
        elif t[0] == '"':
            stack[-1].append(("s", t[1:-1]))
        else:
            stack[-1].append(t)
    return stack[0]


def head(x):
    return x[0] if x and isinstance(x[0], str) else None


def sub(x, name):
    return [c for c in x if isinstance(c, list) and head(c) == name]


def f(v):
    return float(v)


def inter(a, b, tol=TOL):
    """boxes (x1, y1, x2, y2) overlap by more than tol on both axes"""
    return min(a[2], b[2]) - max(a[0], b[0]) > tol and min(a[3], b[3]) - max(a[1], b[1]) > tol


def contains(o, i, tol=TOL):
    return i[0] >= o[0] - tol and i[1] >= o[1] - tol and i[2] <= o[2] + tol and i[3] <= o[3] + tol


def seg_hits_box(s, b, tol=TOL):
    """axis-aligned segment (x1, y1, x2, y2) against box b shrunk by tol"""
    x1, y1, x2, y2 = min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3])
    return x2 > b[0] + tol and x1 < b[2] - tol and y2 > b[1] + tol and y1 < b[3] - tol


# ------------------------------------------------------------------ SVG text ink boxes
def svg_texts(path):
    s = open(path, encoding="utf8").read()
    out, seen = [], set()
    for g in re.finditer(r'<g class="stroked-text"><desc>(.*?)</desc>(.*?)</g>', s, re.S):
        xs, ys = [], []
        for m in re.finditer(r"[ML]\s*([-\d.]+)\s+([-\d.]+)", g.group(2)):
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
        if xs:
            e = (g.group(1), tuple(round(v, 2) for v in (min(xs), min(ys), max(xs), max(ys))))
            if e not in seen:       # the exporter emits every string twice
                seen.add(e)
                out.append(e)
    return out


# ------------------------------------------------------------------ geometry from the schematic
def geometry(path):
    root = parse(open(path, encoding="utf8").read())[0]
    libs = {}
    for ls in sub(root, "lib_symbols"):
        for sym in sub(ls, "symbol"):
            libs[sym[1][1]] = sym
    wires, boxes, bodies, pins = [], [], [], []
    for it in root:
        if not isinstance(it, list):
            continue
        h = head(it)
        if h == "wire":
            p = sub(sub(it, "pts")[0], "xy")
            for a, b in zip(p, p[1:]):
                wires.append((f(a[1]), f(a[2]), f(b[1]), f(b[2])))
        elif h == "rectangle":
            a, b = sub(it, "start")[0], sub(it, "end")[0]
            boxes.append((min(f(a[1]), f(b[1])), min(f(a[2]), f(b[2])), max(f(a[1]), f(b[1])), max(f(a[2]), f(b[2]))))
        elif h == "sheet":
            at, sz = sub(it, "at")[0], sub(it, "size")[0]
            bodies.append(("sheet " + str(sub(it, "property")[0][2][1]), (f(at[1]), f(at[2]), f(at[1]) + f(sz[1]), f(at[2]) + f(sz[2]))))
        elif h == "symbol" and sub(it, "lib_id"):
            lid = sub(it, "lib_id")[0][1][1]
            at = sub(it, "at")[0]
            unit = int(sub(it, "unit")[0][1])
            ref = [p for p in sub(it, "property") if p[1][1] == "Reference"][0][2][1]
            lib = libs.get(lid)
            if lib is None or sub(lib, "power"):
                continue
            x0, y0 = f(at[1]), f(at[2])
            xs, ys = [], []
            for us in sub(lib, "symbol"):
                m = re.match(r".*_(\d+)_(\d+)$", us[1][1])
                if not m or int(m.group(1)) not in (0, unit):
                    continue
                for g in us:
                    if not isinstance(g, list):
                        continue
                    if head(g) == "rectangle":
                        for k in ("start", "end"):
                            c = sub(g, k)[0]
                            xs.append(f(c[1])); ys.append(f(c[2]))
                    elif head(g) == "polyline":
                        for c in sub(sub(g, "pts")[0], "xy"):
                            xs.append(f(c[1])); ys.append(f(c[2]))
                    elif head(g) == "circle":
                        c, r = sub(g, "center")[0], f(sub(g, "radius")[0][1])
                        xs += [f(c[1]) - r, f(c[1]) + r]; ys += [f(c[2]) - r, f(c[2]) + r]
                    elif head(g) == "arc":
                        for k in ("start", "mid", "end"):
                            c = sub(g, k)[0]
                            xs.append(f(c[1])); ys.append(f(c[2]))
                    elif head(g) == "pin":
                        pa = sub(g, "at")[0]
                        px, py, ang = f(pa[1]), f(pa[2]), f(pa[3])
                        ln = f(sub(g, "length")[0][1])
                        qx, qy = px + ln * round(math.cos(math.radians(ang))), py + ln * round(math.sin(math.radians(ang)))
                        pins.append((ref, (x0 + px, y0 - py, x0 + qx, y0 - qy)))
            if xs:
                bodies.append((ref, (x0 + min(xs), y0 - max(ys), x0 + max(xs), y0 - min(ys))))
    return wires, boxes, bodies, pins


# ------------------------------------------------------------------ the checks
def page_size(svg):
    m = re.search(r'viewBox="0\.0+ 0\.0+ ([\d.]+) ([\d.]+)"', open(svg, encoding="utf8").read(2000))
    return float(m.group(1)), float(m.group(2))


def check(sheet, texts, wires, boxes, bodies, pins, page=None):
    hits = []
    if page:    # text must stay inside the sheet border (10 mm margin)
        for t, tb in texts:
            if tb[0] < 10 or tb[1] < 10 or tb[2] > page[0] - 10 or tb[3] > page[1] - 10:
                hits.append(("text/page-edge", "%r leaves the sheet border" % t, tb))
    n = len(texts)
    for i in range(n):
        for j in range(i + 1, n):
            if inter(texts[i][1], texts[j][1], -CLEAR):
                hits.append(("text/text", "%r  x  %r" % (texts[i][0], texts[j][0]), texts[i][1]))
    for t, tb in texts:
        for w in wires:
            if seg_hits_box(w, tb):
                hits.append(("text/wire", "%r  x  wire (%.2f,%.2f)-(%.2f,%.2f)" % ((t,) + w), tb))
        for ref, p in pins:
            if seg_hits_box(p, tb):
                hits.append(("text/pin", "%r  x  pin of %s" % (t, ref), tb))
        for ref, bb in bodies:
            if inter(tb, bb) and not contains(bb, tb, 0.3):
                hits.append(("text/body", "%r  x  body of %s (text crosses the body edge)" % (t, ref), tb))
        for k, b in enumerate(boxes):
            edges = [(b[0], b[1], b[2], b[1]), (b[0], b[3], b[2], b[3]), (b[0], b[1], b[0], b[3]), (b[2], b[1], b[2], b[3])]
            if any(seg_hits_box(e, tb, 0.05) for e in edges):
                hits.append(("text/box-edge", "%r  x  box (%.1f,%.1f)-(%.1f,%.1f)" % ((t,) + b), tb))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if inter(a, b, 0.0) and not contains(a, b, 0) and not contains(b, a, 0):
                hits.append(("box/box", "(%.1f,%.1f,%.1f,%.1f) x (%.1f,%.1f,%.1f,%.1f)" % (a + b), a))
    for ref, bb in bodies:
        if ref.startswith("sheet"):
            continue
        for b in boxes:
            edges = [(b[0], b[1], b[2], b[1]), (b[0], b[3], b[2], b[3]), (b[0], b[1], b[0], b[3]), (b[2], b[1], b[2], b[3])]
            if any(seg_hits_box(e, bb) for e in edges):
                hits.append(("body/box-edge", "body of %s crosses box (%.1f,%.1f)-(%.1f,%.1f)" % ((ref,) + b), bb))
    return hits


def sheets(proj, root):
    """The root sheet first, then every other .kicad_sch in the project folder (by file stem)."""
    others = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(proj, "*.kicad_sch")))
    return ["root"] + [s for s in others if s != root]


def export_svgs(proj, outdir, root):
    from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
    K = str(get_kicad_cli_path())
    r = subprocess.run([K, "sch", "export", "svg", "-o", outdir, os.path.join(proj, root + ".kicad_sch")],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        sys.exit("svg export failed: " + r.stdout + r.stderr)


def run(proj, root, svg_dir=None, verbose=False):
    """Check every sheet of the project whose root schematic is ``<proj>/<root>.kicad_sch``."""
    tmp = None
    names = sheets(proj, root)
    try:
        if svg_dir is None:
            tmp = tempfile.TemporaryDirectory()
            svg_dir = tmp.name
            export_svgs(proj, svg_dir, root)
        total = {}
        allhits = []
        for sh in names:
            sch = os.path.join(proj, root + ".kicad_sch" if sh == "root" else sh + ".kicad_sch")
            svg = os.path.join(svg_dir, root + ".svg" if sh == "root" else "%s-%s.svg" % (root, sh))
            wires, boxes, bodies, pins = geometry(sch)
            hits = check(sh, svg_texts(svg), wires, boxes, bodies, pins, page_size(svg))
            cnt = {}
            for k, d, b in hits:
                cnt[k] = cnt.get(k, 0) + 1
                allhits.append((sh, k, d, b))
            total[sh] = cnt
            print("%-6s %s" % (sh, ("%d overlaps: " % sum(cnt.values()) + ", ".join("%s %d" % kv for kv in sorted(cnt.items()))) if cnt else "0 overlaps"))
        n = len(allhits)
        if verbose or n:
            for sh, k, d, b in allhits:
                print("  [%s] %s: %s  @ (%.1f,%.1f)-(%.1f,%.1f)" % ((sh, k, d) + tuple(b)))
        print("overlap check: %d overlap(s) in %d sheets" % (n, len(names)))
        return n
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    # usage: python -m kicad_mcp.utils.schematic_overlap <project dir> <root sheet stem> [--svg-dir D] [--list]
    a = sys.argv[1:]
    sd = a[a.index("--svg-dir") + 1] if "--svg-dir" in a else None
    sys.exit(1 if run(a[0], a[1], sd, "--list" in a) else 0)
