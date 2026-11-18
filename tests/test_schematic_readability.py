from pathlib import Path
import pytest

from kicad_mcp.utils import schematic_readability as audit


def test_hierarchy_keeps_repeated_instances_and_rejects_cycle(tmp_path):
    root = tmp_path/'main.kicad_sch'; child = tmp_path/'port.kicad_sch'
    root.write_text('(kicad_sch (sheet (property "Sheetfile" "port.kicad_sch")) (sheet (property "Sheetfile" "port.kicad_sch")))')
    child.write_text('(kicad_sch)')
    assert [p.name for p, _ in audit.hierarchy(root)] == ['main.kicad_sch','port.kicad_sch','port.kicad_sch']
    child.write_text('(kicad_sch (sheet (property "Sheetfile" "main.kicad_sch")))')
    with pytest.raises(ValueError, match='Recursive'):
        list(audit.hierarchy(root))


def test_font_audit_does_not_count_hidden_fields():
    tree = audit.ink.parse('''(kicad_sch
      (property "Footprint" "hidden" (hide yes) (effects (font (size 0.2 0.2))))
      (property "Other" "hidden" (effects (font (size 0.3 0.3)) hide))
      (label "TINY" (effects (font (size 0.4 0.4)))))''')[0]
    assert [s['height_mm'] for s in audit.visible_text_sizes(tree)] == [.4]


def fixture(tmp_path, monkeypatch, paper='"A3"', local=''):
    sch=tmp_path/'sheet.kicad_sch'
    sch.write_text(f'(kicad_sch (paper {paper}) (title_block (title "Power")) (lib_symbols) {local})')
    monkeypatch.setattr(audit.ink,'svg_texts',lambda _: [('VCC',(20,20,30,22))])
    monkeypatch.setattr(audit.ink,'page_size',lambda _: (420,297))
    monkeypatch.setattr(audit.ink,'geometry',lambda _: ([],[],[('U1',(40,40,380,260))],[]))
    return audit.measure_sheet(sch,audit.ink.parse(sch.read_text())[0],tmp_path/'sheet.svg')


def test_readability_is_not_visual_signoff(tmp_path,monkeypatch):
    result=fixture(tmp_path,monkeypatch)
    assert result['structural_requirements_pass']
    assert not result['visual_signoff']
    assert result['bbox_fill_percent'] == pytest.approx(77.978, abs=.001)
    assert 'internal whitespace' in result['fill_method']


def test_a3_svg_mil_rounding_is_accepted_but_not_wrong_dimensions(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    sch=tmp_path/'sheet.kicad_sch'
    tree=audit.ink.parse(sch.read_text())[0]
    monkeypatch.setattr(audit.ink,'page_size',lambda _: (419.9894,297.0022))
    assert audit.measure_sheet(sch,tree,tmp_path/'sheet.svg')['a3_landscape']
    monkeypatch.setattr(audit.ink,'page_size',lambda _: (421,297))
    assert not audit.measure_sheet(sch,tree,tmp_path/'sheet.svg')['a3_landscape']


@pytest.mark.parametrize('paper,local', [('"User" 420 297',''), ('"A3" portrait',''), ('"A3"','(label "SAME_SHEET" (effects (font (size 1.27 1.27))))'), ('"A3"','(text "TINY" (effects (font (size .2 .2))))')])
def test_cannot_pass_by_custom_size_local_labels_or_small_text(tmp_path,monkeypatch,paper,local):
    assert not fixture(tmp_path,monkeypatch,paper,local)['structural_requirements_pass']


def test_failed_export_is_not_readability_pass(tmp_path,monkeypatch):
    from kicad_mcp.tools import schematic_tools
    from tests.test_schematic_tools import Server
    class Jobs:
        root=tmp_path
    sch=tmp_path/'sheet.kicad_sch';sch.write_text('(kicad_sch)')
    monkeypatch.setattr(schematic_tools,'run_cli',lambda *a,**kw: {'ok':False,'error':'failed'})
    server=Server();schematic_tools.register_schematic_tools(server,Jobs())
    result=server.tools['schematic_readability_check'](str(sch))
    assert not result['ok'] and result['sheets']==[]
