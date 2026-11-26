import pytest

zstandard = pytest.importorskip("zstandard")

from kicad_mcp.tools import function_fallbacks


def test_zstd_decompress_enforces_embedded_size_limit():
    raw = b"a" * 5000
    blob = zstandard.ZstdCompressor().compress(raw)

    assert function_fallbacks.zstd_decompress(blob, limit=10_000) == raw
    with pytest.raises(function_fallbacks.SessionError):
        function_fallbacks.zstd_decompress(blob, limit=1000)
