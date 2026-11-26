from kicad_mcp.tools import fallback_tools


def test_block_span_returns_matching_stackup_block():
    text = '(kicad_pcb\n  (stackup (layer "F.Cu"))\n  (net 1))'
    start, end = fallback_tools._block_span(text, "stackup")
    assert text[start:end] == '  (stackup (layer "F.Cu"))'


def test_block_span_rejects_unclosed_block():
    text = '(kicad_pcb (stackup (layer "F.Cu")'
    try:
        fallback_tools._block_span(text, "stackup")
    except fallback_tools.SessionError:
        pass
    else:
        raise AssertionError("expected an unclosed stackup block to raise")


def test_block_span_ignores_parenthesis_inside_string():
    text = '(stackup (x ")"))'
    start, end = fallback_tools._block_span(text, "stackup")
    assert text[start:end] == text
