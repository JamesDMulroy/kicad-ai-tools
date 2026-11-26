from kicad_mcp.utils.schematic_overlap import geometry


def test_symbol_body_applies_rotation_and_mirror(tmp_path):
    schematic = tmp_path / "transform.kicad_sch"
    schematic.write_text('''(kicad_sch
 (lib_symbols
  (symbol "T:B"
   (symbol "B_0_1"
    (rectangle (start -1 -2) (end 1 2))
   )
  )
  (symbol "T:M"
   (symbol "M_0_1"
    (rectangle (start 0 0) (end 2 1))
   )
  )
 )
 (symbol (lib_id "T:B") (at 100 100 90) (unit 1) (property "Reference" "U1"))
 (symbol (lib_id "T:M") (at 100 100 0) (mirror y) (unit 1) (property "Reference" "U2"))
)''', encoding="utf8")

    _, _, bodies, _ = geometry(schematic)

    assert bodies == [("U1", (98.0, 99.0, 102.0, 101.0)),
                      ("U2", (98.0, 99.0, 100.0, 100.0))]
