"""Conservative structural courtyard boxes; never native design signoff."""
import math
import hashlib
from pathlib import Path


def library_inventory(paths, parse):
    """Read exact standalone land patterns without loading or changing a board.

    These structural boxes are research evidence only. Native placement, 3D,
    electrical mapping and release gates remain required.
    """
    rows = []
    seen = set()
    for name in paths:
        path = Path(name)
        if not path.is_absolute() or path.suffix != '.kicad_mod':
            raise ValueError('Expected absolute .kicad_mod path')
        path = path.resolve(strict=True)
        if path in seen:
            raise ValueError('Duplicate footprint path')
        seen.add(path)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        tree = parse(raw.decode('utf-8-sig'))
        roots = [x for x in tree if isinstance(x, list) and x and x[0] == 'footprint']
        if len(roots) != 1:
            raise ValueError('Expected one footprint root')
        fp = roots[0]
        box = footprint_box(fp)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Footprint changed during inventory')
        rows.append(dict(path=str(path), footprint=fp[1], sha256=digest,
                         local_bbox_mm=box, width_mm=box[2]-box[0],
                         height_mm=box[3]-box[1],
                         bbox_area_mm2=(box[2]-box[0])*(box[3]-box[1])))
    return dict(footprints=rows, acceptance_eligible=False,
                method='standalone library courtyard graphic boxes including stroke; no save')


def child(node, name, default=None):
    return next((x for x in node if isinstance(x, list) and x and x[0] == name), default)


def footprint_box(fp):
    points = []
    for graphic in fp:
        if not isinstance(graphic, list) or not graphic or not str(graphic[0]).startswith('fp_'):
            continue
        layer = child(graphic, 'layer')
        if not layer or layer[1] not in ('F.CrtYd', 'B.CrtYd'):
            continue
        kind = graphic[0]
        stroke = child(graphic, 'stroke', [])
        width = float(child(stroke, 'width', ['width', 0])[1]) / 2
        if kind in ('fp_line', 'fp_rect'):
            start, end = child(graphic, 'start'), child(graphic, 'end')
            local = [(float(p[1]), float(p[2])) for p in (start, end)]
        elif kind == 'fp_poly':
            local = [(float(p[1]), float(p[2])) for p in child(graphic, 'pts')[1:]]
        elif kind == 'fp_circle':
            center, end = child(graphic, 'center'), child(graphic, 'end')
            cx, cy = float(center[1]), float(center[2])
            radius = math.hypot(float(end[1])-cx, float(end[2])-cy)
            local = [(cx-radius, cy-radius), (cx+radius, cy+radius)]
        else:
            raise ValueError('Unsupported courtyard primitive: ' + str(kind))
        x0, x1 = min(p[0] for p in local)-width, max(p[0] for p in local)+width
        y0, y1 = min(p[1] for p in local)-width, max(p[1] for p in local)+width
        points.extend([(x0, y0), (x1, y1)])
    if not points:
        raise ValueError('Missing courtyard')
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def inventory(tree):
    rows=[]
    board=next(x for x in tree if x[0]=='kicad_pcb')
    for fp in board:
        if not isinstance(fp,list) or not fp or fp[0]!='footprint': continue
        props={x[1]: x[2] for x in fp if isinstance(x,list) and len(x)>2 and x[0]=='property'}
        box=footprint_box(fp)
        rows.append(dict(ref=props.get('Reference',''),value=props.get('Value',''),
                         footprint=fp[1],side=child(fp,'layer')[1],
                         local_bbox_mm=box,width_mm=box[2]-box[0],height_mm=box[3]-box[1],
                         bbox_area_mm2=(box[2]-box[0])*(box[3]-box[1])))
    return sorted(rows,key=lambda x:x['ref'])
