import unittest,tempfile
from pathlib import Path
from kicad_mcp.utils.svg_exports import normalize

class SVGTests(unittest.TestCase):
    def scratch(self):
        root=Path(__file__).resolve().parents[1]/'.tmp/tests';root.mkdir(parents=True,exist_ok=True)
        return tempfile.TemporaryDirectory(dir=root)
    def test_native_style_formatting_and_backup(self):
        with self.scratch() as folder:
            root=Path(folder);svgs=root/'svg';svgs.mkdir();path=svgs/'page.svg'
            raw=b'<svg> \n<g style="fill:none;  \nstroke:red; ">\n<desc>readable title</desc><path d="M 0 0 L 1 1"/>\n</g></svg> \n';path.write_bytes(raw)
            result=normalize(svgs,root/'jobs',True)
            self.assertTrue(result['xml_drawing_semantics_unchanged']);self.assertEqual((Path(result['backup_dir'])/'page.svg').read_bytes(),raw)
            self.assertTrue(all(line==line.rstrip() for line in path.read_text().splitlines()))
    def test_meaningful_text_change_refuses_without_mutation(self):
        with self.scratch() as folder:
            root=Path(folder);svgs=root/'svg';svgs.mkdir();path=svgs/'page.svg';raw='<svg><text>meaningful space \nnext line</text></svg>';path.write_text(raw)
            with self.assertRaisesRegex(ValueError,'semantics'):normalize(svgs,root/'jobs',True)
            self.assertEqual(path.read_text(),raw)
    def test_mutation_required(self):
        with self.assertRaisesRegex(ValueError,'allow_mutation'):normalize('missing','missing',False)
