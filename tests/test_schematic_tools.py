"""Readback tests for compact schematic tools; no GUI or external messages."""
from pathlib import Path
import json
import pytest

from kicad_mcp.tools import schematic_tools as tools
from kicad_mcp.utils import schematic_overlap as overlap


class Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def test_erc_readback():
    fixture = Path(__file__).parent / 'fixtures' / 'schematic-erc.rpt'
    result = tools.summarize_erc(fixture.read_text())
    assert result['errors'] == 2
    assert result['warnings'] == 0
    assert len(result['violations']) == 1
    issue = result['violations'][0]
    assert issue['count'] == 2 and issue['refs'] == ['U1', 'U2']
    assert len(issue['locations']) == 2
    assert 'supply source' in issue['hint']


def test_native_erc_sheet_headers_and_severities():
    result = tools.summarize_erc((Path(__file__).parent/'fixtures'/'schematic-erc-native.rpt').read_text())
    issue = result['violations'][0]
    assert issue['sheets'] == ['/control/','/power/']
    assert issue['severities'] == ['warning']
    assert issue['nets'] == ['EN','VBUS']
    assert issue['refs'] == ['#PWR01','FB1']


def test_svg_readback_and_strict_clearance(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'schematic-ink.svg'
    texts = overlap.svg_texts(fixture)
    assert texts == [('R1', (20.0, 20.0, 22.0, 21.0)), ('NET', (22.2, 20.0, 24.0, 21.0))]
    hits = overlap.check('fixture', texts, [], [], [], [], (100,100))
    assert len(hits) == 1
    assert hits[0][0] == 'text/text'
    assert hits[0][2] == (20,20,22,21)
    assert overlap.CLEAR == .3 and overlap.TOL == .12


def test_overlap_tool_readback(tmp_path, monkeypatch):
    sch = tmp_path/'fixture.kicad_sch'
    sch.write_text('(kicad_sch (lib_symbols))')
    def export(command, input_path, options, output_path, **kw):
        out = Path(output_path); out.mkdir()
        (out/'fixture.svg').write_bytes((Path(__file__).parent/'fixtures'/'schematic-ink.svg').read_bytes())
        return {'ok':True}
    monkeypatch.setattr(tools, 'run_cli', export)
    result = tools.check_overlap(str(sch), str(tmp_path))
    assert not result['ok']
    assert result['violations'][0]['coordinates'] == [20,20,22,21]


def test_job_tools_use_managed_receipts(tmp_path):
    class Jobs:
        root = tmp_path
        def start(self, operation, args):
            assert operation == 'kicad_cli'
            assert args['command'] == 'sch erc'
            assert not args['allow_mutation']
            return {'job_id':'6dc77c36-3c26-48bf-afc9-946325b04217', 'status':'queued'}
        def status(self, job_id):
            return {'job_id':job_id, 'status':'completed', 'result':{'ok':True}}
    server = Server()
    tools.register_schematic_tools(server, Jobs())
    assert set(server.tools) == {'schematic_readability_check','schematic_overlap_check','erc_summary','job_start','job_status',
                                 'svg_normalize_whitespace','schematic_pcb_sync','schematic_write_generated_bundle'}
    start = server.tools['job_start']('sch erc', str(tmp_path/'fixture.kicad_sch'))
    assert server.tools['job_status'](start['job_id'])['result']['ok']


def test_erc_tool_reads_report(tmp_path, monkeypatch):
    class Jobs:
        root = tmp_path
    def erc(command, input_path, options, output_path, **kw):
        assert options == {'--severity-all': True}
        Path(output_path).write_text((Path(__file__).parent/'fixtures'/'schematic-erc.rpt').read_text())
        return {'ok':True}
    monkeypatch.setattr(tools, 'run_cli', erc)
    server = Server(); tools.register_schematic_tools(server, Jobs())
    result = server.tools['erc_summary']('fixture.kicad_sch')
    assert not result['ok'] and result['errors'] == 2


def test_job_status_reads_failure_receipt(tmp_path):
    fixture = Path(__file__).parent/'fixtures'/'cli-job-receipt.json'
    receipt = tmp_path/'receipt.json'
    receipt.write_bytes(fixture.read_bytes())
    class Jobs:
        root = tmp_path
        def status(self, job_id):
            value = json.loads(receipt.read_text())
            assert value['job_id'] == job_id
            return value
    server = Server()
    tools.register_schematic_tools(server, Jobs())
    result = server.tools['job_status']('6dc77c36-3c26-48bf-afc9-946325b04217')
    assert result['owned_tree_stopped'] and not result['operation_ok']
    assert result['result']['error']['code'] == 'unknown_command'
    with pytest.raises(ValueError):
        server.tools['job_status']('../outside')


def test_export_failure_is_never_a_clean_overlap(tmp_path, monkeypatch):
    sch=tmp_path/'fixture.kicad_sch'; sch.write_text('(kicad_sch)')
    monkeypatch.setattr(tools, 'run_cli', lambda *a,**kw: {'ok':False,'error':'export failed'})
    assert tools.check_overlap(str(sch),str(tmp_path)) == {'ok':False,'error':'export failed'}
