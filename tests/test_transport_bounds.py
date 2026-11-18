"""Offline tests for transport byte caps, request deadlines and tree-entry path validation.

Every response is a fake built from ``io.BytesIO`` and served through a monkeypatched
``sc._opener.open``; the clock is a monkeypatched ``sc._monotonic`` so nothing sleeps, and sockets
are blocked for every test.
"""
import io
import json
import socket
import urllib.error

import pytest

from kicad_mcp.utils import source_catalog as sc

SHA = "18fb9289" + "0" * 32
URL = f"{sc.GITLAB_API_BASE}/projects/{sc.PROJECT_ID}/repository/tags/10.0.5"
ARCHIVE_URL = f"{sc.GITLAB_API_BASE}/projects/{sc.PROJECT_ID}/repository/archive.tar.gz?sha={SHA}"
TAG_BODY = json.dumps({"commit": {"id": SHA}}).encode()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeResponse:
    """Context-manager response with .status, .headers and chunked .read(n)."""

    def __init__(self, body=b"", status=200, headers=None, on_read=None):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self._on_read = on_read
        self.read_sizes = []

    def read(self, n=-1):
        self.read_sizes.append(n)
        if self._on_read:
            self._on_read()
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(sc._opener, "open", no_network)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(sc, "_monotonic", fake)
    return fake


def serve(monkeypatch, handler):
    monkeypatch.setattr(sc._opener, "open", handler)


def serve_one(monkeypatch, response):
    def handler(request, timeout=None):
        if isinstance(response, BaseException):
            raise response
        return response

    serve(monkeypatch, handler)


# --- JSON cap --------------------------------------------------------------------------------
def test_json_cap_constants():
    assert sc.MAX_JSON_RESPONSE_BYTES == 4 * 1024 * 1024
    assert sc.MAX_ERROR_BODY_BYTES == 64 * 1024
    assert sc.MAX_RAW_FILE_HARD_CEILING_BYTES == 64 * 1024 * 1024
    assert sc.REQUEST_DEADLINE_SECONDS == 60
    assert sc.ARCHIVE_DEADLINE_SECONDS == 180


def test_json_cap_exactly_at_limit_passes_and_one_over_raises(monkeypatch):
    cap = sc.MAX_JSON_RESPONSE_BYTES
    serve_one(monkeypatch, FakeResponse(b"x" * cap, headers={"X-A": "1"}))
    status, headers, body = sc._default_transport(URL)
    assert status == 200 and len(body) == cap and headers == {"X-A": "1"}
    serve_one(monkeypatch, FakeResponse(b"x" * (cap + 1)))
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc._default_transport(URL)


def test_reads_are_chunked_and_never_larger_than_the_chunk_size(monkeypatch):
    response = FakeResponse(b"x" * (3 * sc.READ_CHUNK_BYTES + 5))
    serve_one(monkeypatch, response)
    assert len(sc._default_transport(URL)[2]) == 3 * sc.READ_CHUNK_BYTES + 5
    assert response.read_sizes and max(response.read_sizes) <= sc.READ_CHUNK_BYTES
    assert -1 not in response.read_sizes


def test_json_cap_enforced_through_resolve_pinned_commit(monkeypatch):
    monkeypatch.setattr(sc, "MAX_JSON_RESPONSE_BYTES", 200)
    serve_one(monkeypatch, FakeResponse(TAG_BODY))
    assert sc.resolve_pinned_commit(transport=sc._default_transport)["resolved_sha"] == SHA
    padded = json.dumps({"commit": {"id": SHA}, "pad": "x" * 300}).encode()
    serve_one(monkeypatch, FakeResponse(padded))
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc.resolve_pinned_commit(transport=sc._default_transport)


def test_json_cap_enforced_through_list_source_tree(monkeypatch):
    monkeypatch.setattr(sc, "MAX_JSON_RESPONSE_BYTES", 300)

    def handler(request, timeout=None):
        if "repository/tags" in request.full_url:
            return FakeResponse(TAG_BODY)
        entries = [{"id": "a", "name": f"f{i}.c", "type": "blob", "path": f"f{i}.c", "mode": "100644"}
                   for i in range(20)]
        return FakeResponse(json.dumps(entries).encode())

    serve(monkeypatch, handler)
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc.list_source_tree("")


# --- Error body cap --------------------------------------------------------------------------
def _http_error(code, body):
    return urllib.error.HTTPError(URL, code, "err", {"Retry-After": "9"}, io.BytesIO(body))


def test_error_body_at_cap_is_returned_and_one_over_raises(monkeypatch):
    cap = sc.MAX_ERROR_BODY_BYTES
    serve_one(monkeypatch, _http_error(429, b"e" * cap))
    status, headers, body = sc._default_transport(URL)
    assert status == 429 and len(body) == cap and headers["Retry-After"] == "9"
    serve_one(monkeypatch, _http_error(500, b"e" * (cap + 1)))
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc._default_transport(URL)


def test_url_error_is_still_wrapped(monkeypatch):
    serve_one(monkeypatch, urllib.error.URLError("no network"))
    with pytest.raises(sc.SourceCatalogError, match="GitLab request failed"):
        sc._default_transport(URL)


# --- Raw file cap ----------------------------------------------------------------------------
def raw_world(monkeypatch, size, raw):
    """Default transport; tag/metadata via the fake opener, ``raw`` (response or exception) for /raw."""
    calls = []

    def handler(request, timeout=None):
        url = request.full_url
        calls.append(url)
        if "/raw" in url:
            if isinstance(raw, BaseException):
                raise raw
            return raw
        if "repository/tags" in url:
            return FakeResponse(TAG_BODY)
        if "repository/files" in url:
            return FakeResponse(json.dumps({"size": size, "blob_id": "b"}).encode())
        raise AssertionError(url)

    serve(monkeypatch, handler)
    return calls


def test_raw_cap_is_max_of_length_and_size_just_under_and_over(monkeypatch):
    raw_world(monkeypatch, 10, FakeResponse(b"0123456789"))        # size 10 > length 5: cap 10
    result = sc.read_source_file("README.md", 0, 5)
    assert result["returned_bytes"] == 5 and result["total_size"] == 10
    raw_world(monkeypatch, 10, FakeResponse(b"0123456789X"))       # 11 bytes > cap 10
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc.read_source_file("README.md", 0, 5)


def test_raw_cap_uses_length_when_it_exceeds_size(monkeypatch):
    # cap = max(8, 5) = 8: 8 bytes pass the transport, then the full-file size check rejects them.
    raw_world(monkeypatch, 5, FakeResponse(b"12345678"))
    with pytest.raises(sc.SourceCatalogError, match="incomplete full-file response"):
        sc.read_source_file("README.md", 0, 8)
    raw_world(monkeypatch, 5, FakeResponse(b"123456789"))
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc.read_source_file("README.md", 0, 8)


def test_raw_range_ignoring_200_for_small_file_still_works(monkeypatch):
    raw_world(monkeypatch, 17, FakeResponse(b"hello kicad world!"[:17]))
    result = sc.read_source_file("README.md", 6, 5)
    assert result["returned_bytes"] == 5 and result["next_offset"] == 11


def test_raw_206_partial_within_cap_passes(monkeypatch):
    raw_world(monkeypatch, 10, FakeResponse(b"01234", 206, {"Content-Range": "bytes 0-4/10"}))
    result = sc.read_source_file("README.md", 0, 5)
    assert result["returned_bytes"] == 5 and result["done"] is False


def test_raw_hard_ceiling_clamps_the_cap(monkeypatch):
    monkeypatch.setattr(sc, "MAX_RAW_FILE_HARD_CEILING_BYTES", 8)
    raw_world(monkeypatch, 20, FakeResponse(b"x" * 20))            # size 20 > ceiling 8
    with pytest.raises(sc.SourceCatalogError, match="response_too_large"):
        sc.read_source_file("README.md", 0, 5)
    raw_world(monkeypatch, 8, FakeResponse(b"x" * 8))              # exactly at the ceiling passes
    assert sc.read_source_file("README.md", 0, 5)["returned_bytes"] == 5


def test_read_source_file_rejects_length_above_ceiling_before_any_transport_call(monkeypatch):
    calls = []

    def transport(url, headers=None):
        calls.append(url)
        raise AssertionError("transport must not be called")

    monkeypatch.setattr(sc, "MAX_RAW_FILE_HARD_CEILING_BYTES", 8)
    with pytest.raises(sc.SourceCatalogError, match="ceiling"):
        sc.read_source_file("README.md", 0, 9, transport=transport)
    assert calls == []
    with pytest.raises(sc.SourceCatalogError, match="ceiling"):
        sc.read_source_file("README.md", 0, sc.MAX_RAW_FILE_HARD_CEILING_BYTES + 1, transport=transport)
    assert calls == []


def test_real_ceiling_rejects_just_above_64_mib():
    def transport(url, headers=None):
        raise AssertionError("transport must not be called")

    with pytest.raises(sc.SourceCatalogError, match="ceiling"):
        sc.read_source_file("README.md", 0, 64 * 1024 * 1024 + 1, transport=transport)


def test_injected_two_argument_transport_is_called_unchanged():
    seen = []

    def transport(url, headers):
        seen.append((url, headers))
        if "tags" in url:
            return 200, {}, TAG_BODY
        if "/raw" in url:
            return 200, {}, b"hello"
        return 200, {}, b'{"size": 5}'

    assert sc.read_source_file("README.md", 0, 5, transport=transport)["returned_bytes"] == 5
    assert len(seen) == 3


# --- Deadlines -------------------------------------------------------------------------------
def test_deadline_exceeded_with_slow_reader(monkeypatch, clock):
    body = b"x" * (3 * sc.READ_CHUNK_BYTES)
    serve_one(monkeypatch, FakeResponse(body, on_read=lambda: setattr(clock, "now", clock.now + 30)))
    with pytest.raises(sc.SourceCatalogError, match="deadline_exceeded"):
        sc._default_transport(URL)


def test_finishing_in_time_passes(monkeypatch, clock):
    body = b"x" * (3 * sc.READ_CHUNK_BYTES)
    serve_one(monkeypatch, FakeResponse(body, on_read=lambda: setattr(clock, "now", clock.now + 1)))
    assert sc._default_transport(URL)[2] == body


def test_deadline_applies_to_error_bodies(monkeypatch, clock):
    err = urllib.error.HTTPError(URL, 500, "err", {}, io.BytesIO(b"e" * (2 * sc.READ_CHUNK_BYTES)))
    real_read = err.read

    def slow_read(n=-1):
        clock.now += 40
        return real_read(n)

    err.read = slow_read
    serve_one(monkeypatch, err)
    with pytest.raises(sc.SourceCatalogError, match="deadline_exceeded"):
        sc._default_transport(URL)


def test_deadline_uses_configured_seconds(monkeypatch, clock):
    monkeypatch.setattr(sc, "REQUEST_DEADLINE_SECONDS", 5)
    serve_one(monkeypatch, FakeResponse(b"x" * (3 * sc.READ_CHUNK_BYTES),
                                        on_read=lambda: setattr(clock, "now", clock.now + 3)))
    with pytest.raises(sc.SourceCatalogError, match="deadline_exceeded"):
        sc._default_transport(URL)


def test_archive_deadline_exceeded(monkeypatch, clock):
    body = b"x" * (3 * sc.READ_CHUNK_BYTES)
    serve_one(monkeypatch, FakeResponse(body, on_read=lambda: setattr(clock, "now", clock.now + 90)))
    with pytest.raises(sc.SourceCatalogError, match="deadline_exceeded"):
        sc._default_archive_transport(ARCHIVE_URL, {}, 10_000_000)


def test_archive_in_time_returns_at_most_max_bytes_plus_one(monkeypatch, clock):
    serve_one(monkeypatch, FakeResponse(b"x" * 1000, on_read=lambda: setattr(clock, "now", clock.now + 1)))
    status, _headers, body = sc._default_archive_transport(ARCHIVE_URL, {}, 100)
    assert status == 200 and len(body) == 101
    serve_one(monkeypatch, FakeResponse(b"x" * 50))
    assert len(sc._default_archive_transport(ARCHIVE_URL, {}, 100)[2]) == 50


def test_archive_error_body_read_stays_at_65536(monkeypatch):
    serve_one(monkeypatch, urllib.error.HTTPError(ARCHIVE_URL, 503, "err", {}, io.BytesIO(b"e" * 70000)))
    status, _headers, body = sc._default_archive_transport(ARCHIVE_URL, {}, 100)
    assert status == 503 and len(body) == 65536


# --- list_source_tree entry validation -------------------------------------------------------
def tree_transport(entries):
    body = json.dumps(entries).encode()

    def transport(url, headers=None):
        if "repository/tags" in url:
            return 200, {}, TAG_BODY
        return 200, {"x-next-page": "", "x-total-pages": "1"}, body

    return transport


def entry(path, name=None, type_="blob"):
    return {"id": "abc", "name": path.rsplit("/", 1)[-1] if name is None else name, "type": type_,
            "path": path, "mode": "100644"}


@pytest.mark.parametrize("request_path,item", [
    ("", entry("../x", "x")),
    ("", entry("/etc/passwd", "passwd")),
    ("", entry("a/../b", "b")),
    ("", entry("a\\b", "a\\b")),
    ("", entry("", "")),
    ("pcbnew", entry("eeschema/a.cpp")),
    ("pcbnew", entry("pcbnew_evil/a.cpp")),
    ("pcbnew", entry("pcbnew")),
    ("pcbnew", entry("a.cpp")),
    ("", entry("a/b.cpp")),
    ("pcbnew", entry("pcbnew/sub/a.cpp")),
    ("pcbnew", entry("pcbnew/a.cpp", name="b.cpp")),
    ("", entry("a.cpp", name="")),
    ("", {"id": "x", "name": "a", "type": "blob", "mode": "100644"}),
    ("", {"id": "x", "name": "a", "type": "blob", "path": 5, "mode": "100644"}),
], ids=["traversal", "absolute", "inner-dotdot", "backslash", "empty-path", "outside", "prefix-lookalike",
        "equal-to-request", "root-level-under-subdir", "grandchild-at-root", "grandchild", "name-mismatch",
        "empty-name", "missing-path", "non-string-path"])
def test_list_source_tree_rejects_bad_entries(request_path, item):
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        sc.list_source_tree(request_path, transport=tree_transport([item]))


def test_list_source_tree_accepts_well_formed_response():
    entries = [entry("pcbnew/a.cpp"), entry("pcbnew/sub", type_="tree"), "not-a-dict"]
    result = sc.list_source_tree("pcbnew", transport=tree_transport(entries))
    assert [e["path"] for e in result["entries"]] == ["pcbnew/a.cpp", "pcbnew/sub"]
    assert result["path"] == "pcbnew" and result["next_page"] is None


def test_list_source_tree_accepts_root_listing():
    result = sc.list_source_tree("", transport=tree_transport([entry("README.md"), entry("pcbnew", type_="tree")]))
    assert [e["name"] for e in result["entries"]] == ["README.md", "pcbnew"]
