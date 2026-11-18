"""Hierarchy-wide readability diagnostics; measurements never imply visual signoff."""
from __future__ import annotations

from pathlib import Path
import hashlib

from kicad_mcp.utils import schematic_overlap as ink


def value(node):
    return node[1] if isinstance(node, tuple) else node


def hierarchy(path: Path, ancestors=()):
    """Visit every instance, retaining its path and rejecting recursive sheets."""
    path = path.resolve(strict=True)
    if path in ancestors:
        raise ValueError(f'Recursive schematic hierarchy: {path}')
    tree = ink.parse(path.read_text(encoding='utf-8-sig'))[0]
    yield path, tree
    for sheet in ink.sub(tree, 'sheet'):
        props = {value(p[1]): value(p[2]) for p in ink.sub(sheet, 'property')}
        child = props.get('Sheetfile') or props.get('Sheet file')
        if not child:
            raise ValueError(f'Sheet without a Sheetfile in {path}')
        yield from hierarchy(path.parent / child, (*ancestors, path))


def visible_text_sizes(tree):
    """Report source font sizes, ignoring explicitly hidden properties and pins."""
    found = []
    def walk(node, context):
        if not isinstance(node, list) or not node:
            return
        tag = ink.head(node)
        # Instances contain Reference tokens but no rendered text.
        if tag == 'instances':
            return
        if any(value(c) == 'hide' for c in node if not isinstance(c, list)):
            return
        for h in ink.sub(node, 'hide'):
            if len(h) < 2 or value(h[1]) == 'yes':
                return
        if tag == 'font':
            for size in ink.sub(node, 'size'):
                found.append({'context': context, 'height_mm': float(size[1]), 'width_mm': float(size[2])})
        for child in node:
            if isinstance(child, list):
                walk(child, context + '/' + str(tag or '?'))
    walk(tree, '')
    return found


def measure_sheet(path: Path, tree, svg: Path):
    texts = ink.svg_texts(svg)
    wires, boxes, bodies, pins = ink.geometry(path)
    width, height = ink.page_size(svg)
    # 10mm border matches the strict overlap gate. Exclude the drawing sheet
    # during export so its border/title cannot inflate content extent.
    drawing = (10., 10., width - 10., height - 10.)
    extents = [b for _, b in texts] + wires + [b for _, b in bodies + pins]
    bbox = None
    fill = 0.
    if extents:
        bbox = [min(min(b[0], b[2]) for b in extents), min(min(b[1], b[3]) for b in extents),
                max(max(b[0], b[2]) for b in extents), max(max(b[1], b[3]) for b in extents)]
        clipped = (max(bbox[0], drawing[0]), max(bbox[1], drawing[1]),
                   min(bbox[2], drawing[2]), min(bbox[3], drawing[3]))
        fill = 100 * max(0, clipped[2]-clipped[0]) * max(0, clipped[3]-clipped[1]) / ((width-20)*(height-20))
    fonts = visible_text_sizes(tree)
    small = [f for f in fonts if min(f['height_mm'], f['width_mm']) < 1.27 - 1e-6]
    title = ink.sub(tree, 'title_block')
    title_text = ink.sub(title[0], 'title') if title else []
    paper = ink.sub(tree, 'paper')
    # KiCad's SVG viewBox is quantized through mil coordinates (0.0254mm).
    # Source paper must still be exactly A3 without the portrait modifier.
    is_a3 = (bool(paper) and value(paper[0][1]) == 'A3' and
             len(paper[0]) == 2 and abs(width-420) <= .0254 and abs(height-297) <= .0254)
    overlaps = ink.check(path.stem, texts, wires, boxes, bodies, pins, (width, height))
    local = ink.sub(tree, 'label')
    return {'schematic_path': str(path), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'paper_mm': [width, height], 'a3_landscape': is_a3,
            'title': value(title_text[0][1]) if title_text else '',
            'content_bbox_mm': bbox, 'bbox_fill_percent': round(fill, 3),
            'fill_method': 'content bounding rectangle clipped to 10mm drawing border; includes internal whitespace; visual review required',
            'minimum_text_mm': min((min(f['height_mm'], f['width_mm']) for f in fonts), default=None),
            'undersized_font_count': len(small), 'local_label_count': len(local),
            'symbol_count': len(ink.sub(tree, 'symbol')), 'wire_segment_count': len(wires),
            'overlap_count': len(overlaps),
            'overlaps': [dict(gate=k, detail=d, coordinates=list(b)) for k, d, b in overlaps],
            'structural_requirements_pass': bool(is_a3 and title_text and not small and not local and not overlaps and fill >= 60),
            'visual_signoff': False, 'svg_path': str(svg)}
