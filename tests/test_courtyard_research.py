import unittest
from kicad_mcp.courtyard_research import footprint_box

class ResearchTests(unittest.TestCase):
    def test_box_and_stroke(self):
        fp=['footprint','QFN',['fp_rect',['start','-1','-2'],['end','1','2'],
             ['stroke',['width','0.1']],['layer','F.CrtYd']]]
        self.assertEqual(footprint_box(fp),[-1.05,-2.05,1.05,2.05])

    def test_missing_and_unsupported_fail(self):
        with self.assertRaisesRegex(ValueError,'Missing'):
            footprint_box(['footprint','X'])
        with self.assertRaisesRegex(ValueError,'Unsupported'):
            footprint_box(['footprint','X',['fp_arc',['layer','F.CrtYd']]])
