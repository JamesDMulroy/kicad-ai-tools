import tempfile
import unittest
from pathlib import Path
from kicad_mcp.courtyard_research import library_inventory


class LibraryResearchTests(unittest.TestCase):
    def test_exact_box_hash_and_research_status(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'X.kicad_mod'
            p.write_text('(footprint X)', encoding='utf-8')
            tree = [['footprint', 'X', ['fp_rect', ['start', '-1', '-2'],
                     ['end', '1', '2'], ['stroke', ['width', '0.1']], ['layer', 'F.CrtYd']]]]
            r = library_inventory([str(p)], lambda s: tree)
            self.assertFalse(r['acceptance_eligible'])
            self.assertEqual(r['footprints'][0]['local_bbox_mm'], [-1.05,-2.05,1.05,2.05])
            self.assertEqual(len(r['footprints'][0]['sha256']), 64)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                library_inventory([str(p), str(p)], lambda s: tree)

    def test_refuses_wrong_path_and_invalid_root(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            library_inventory(['relative.kicad_mod'], lambda s: [])
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'X.kicad_mod'; p.write_text('bad')
            with self.assertRaisesRegex(ValueError, 'one footprint'):
                library_inventory([str(p)], lambda s: [['kicad_pcb']])
