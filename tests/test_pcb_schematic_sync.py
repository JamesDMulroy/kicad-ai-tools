import unittest,tempfile,pathlib
from kicad_mcp.utils.pcb_schematic_sync import netlist_data,validate_board_candidate,same_bytes
from kicad_mcp.utils.pcb_schematic_import import execute

class Pad:
    def __init__(self,n):self.n=n
    def GetNumber(self):return self.n
class Footprint:
    def Pads(self):return [Pad('1'),Pad('2')]
class PCB:
    @staticmethod
    def FootprintLoad(directory,name):return Footprint()
    @staticmethod
    def LoadBoard(path):raise AssertionError('Preview/error must not load or mutate board')

class SyncTests(unittest.TestCase):
    def test_explicit_multi_svg_mode_and_promoted_bytes(self):
        calls=[]
        def runner(*args,**kwargs):calls.append((args,kwargs));return {'ok':True}
        self.assertTrue(validate_board_candidate('board.kicad_pcb','evidence',runner)['ok'])
        self.assertTrue(calls[0][0][2]['--mode-multi']);self.assertIsNone(calls[0][1]['timeout_seconds'])
        with tempfile.TemporaryDirectory() as folder:
            a=pathlib.Path(folder)/'a';b=pathlib.Path(folder)/'b';a.write_bytes(b'correct');b.write_bytes(b'correct');self.assertTrue(same_bytes(a,b));b.write_bytes(b'wrong');self.assertFalse(same_bytes(a,b))
    def test_netlist_partition(self):
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/'in.net'
            path.write_text('(export (components (comp (ref "R1") (value "10k") (footprint "L:R") (tstamps "abc") (sheetpath (tstamps "/root/sheet/")) (fields (field (name "FIT2Ref") "ORIGINAL_R") (field (name "Description")) (field (name "LCSC") "")) (property (name "exclude_from_bom")))) (nets (net (code "1") (name "GND") (node (ref "R1") (pin "2")))))')
            result=netlist_data(path)
            self.assertEqual(result[0]['pads'],{'2':'GND'})
            self.assertEqual(result[0]['path'],'/root/sheet/')
            self.assertEqual(result[0]['fields'],{'FIT2Ref':'ORIGINAL_R','Description':'','LCSC':''});self.assertTrue(result[0]['exclude_from_bom'])
    def test_preview_and_missing_pad_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            lib=pathlib.Path(folder);(lib/'Part.kicad_mod').write_text('(footprint "Part" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu")))')
            component={'reference':'U1','footprint':'L:Part','pads':{'1':'GND'}}
            spec={'components':[component],'libraries':{'L':str(lib)},'apply':False}
            self.assertTrue(execute(spec)['ok'])
            component['pads']['3']='VDD';spec['apply']=True
            result=execute(spec)
            self.assertFalse(result['ok']);self.assertFalse(result['applied'])
            self.assertEqual(result['issues'][0]['pins'],['3'])
    def test_unqualified_footprint_refuses(self):
        spec={'components':[{'reference':'U1','footprint':'Part','pads':{}}],'libraries':{},'apply':True}
        self.assertFalse(execute(spec)['ok'])
    def test_roundtrip_preserves_mechanics_replaces_copper(self):
        with tempfile.TemporaryDirectory() as folder:
            lib=pathlib.Path(folder);(lib/'Part.kicad_mod').write_text('(footprint "Part" (layer "F.Cu") (attr smd exclude_from_bom) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))')
            board=lib/'old.kicad_pcb';board.write_text('(kicad_pcb (version 20241229) (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts")) (footprint "obsolete") (segment (start 0 0) (end 1 1)) (net 5 "old"))')
            spec={'components':[{'reference':'U1','value':'Device','footprint':'L:Part','pads':{'1':'GND'},'path':'/root/sheet/','uuid':'part','fields':{'FIT2Ref':'ORIGINAL_U','LCSC':'C123'}}],'libraries':{'L':str(lib)},'apply':True,'board':str(board),'candidate':str(lib/'new.kicad_pcb')}
            result=execute(spec);self.assertTrue(result['connectivity_matches']);self.assertTrue(result['ok'])
            text=(lib/'new.kicad_pcb').read_text();self.assertIn('gr_line',text);self.assertNotIn('obsolete',text);self.assertNotIn('(segment',text);self.assertIn('/root/sheet/part',text)
            self.assertIn('"FIT2Ref" "ORIGINAL_U"',text);self.assertIn('"LCSC" "C123"',text);self.assertNotIn('exclude_from_bom',text)

if __name__=='__main__':unittest.main()
