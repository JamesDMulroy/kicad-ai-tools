"""Offline tests for source_catalog.search_source_code (design section 3.4).

Everything runs against a synthetic in-memory GitLab: a fake JSON transport for the tag and
recursive tree endpoints (keyset pagination via ``Link``) and a fake archive transport that serves a
tar.gz built with tarfile/io.BytesIO. Sockets, urllib openers, tar extraction and file writes are
guarded for every test, so a regression that touches the network or the disk fails loudly.
"""
import builtins
import gzip
import hashlib
import io
import json
import socket
import tarfile
import urllib.parse

import pytest

from kicad_mcp.utils import source_catalog as sc

SHA = "18fb9289ff0efdca53c0352ed81a0973f0a6b58c"
OTHER_SHA = "18fb9289" + "0" * 32
ROOT = "kicad-10.0.5-18fb9289"
TREE_URL = f"{sc.GITLAB_API_BASE}/projects/{sc.PROJECT_ID}/repository/tree"


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


class World:
    """Mutable fake GitLab: tests tweak ``tree`` / ``members`` before calling ``run``."""

    def __init__(self, scope="pcbnew", page_size=2, root=ROOT):
        self.scope, self.page_size, self.root = scope, page_size, root
        self.tree = {}            # path -> tree entry dict
        self.members = []         # (kind, full archive name, payload)
        self.dirs = set()         # relative dirs to emit as archive directory members
        self.comment = SHA        # pax global header comment (None = omit)
        self.tag_sha = SHA
        self.reverse = False
        self.link_mutator = None
        self.tag_override = None
        self.tree_override = None
        self.tree_override_page = 0
        self.archive_override = None
        self.body_override = None
        self.calls = []           # ("api"|"archive", url, headers)
        self.archive_max_bytes = []

    # -- content builders -------------------------------------------------
    def _note_dirs(self, path):
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.dirs.add("/".join(parts[:i]))

    def _entry(self, path, data, mode="100644", type_="blob"):
        return {"id": blob_id(data), "name": path.rsplit("/", 1)[-1], "type": type_, "path": path, "mode": mode}

    def add_file(self, path, data):
        self.tree[path] = self._entry(path, data)
        self.archive_only(path, data)
        return self

    def tree_only(self, path, data):
        self.tree[path] = self._entry(path, data)
        return self

    def archive_only(self, path, data):
        self._note_dirs(path)
        self.members.append(("file", f"{self.root}/{path}", data))
        return self

    def add_symlink(self, path, target, tree_target=None):
        self.tree[path] = self._entry(path, (target if tree_target is None else tree_target).encode(), "120000")
        self._note_dirs(path)
        self.members.append(("sym", f"{self.root}/{path}", target))
        return self

    def add_submodule(self, path):
        self.tree[path] = {"id": "c" * 40, "name": path.rsplit("/", 1)[-1], "type": "commit",
                           "path": path, "mode": "160000"}
        return self

    # -- served documents ---------------------------------------------------
    def tree_entries(self):
        entries = dict(self.tree)
        for path in self.tree:
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                parent = "/".join(parts[:i])
                if parent != self.scope and sc._in_scope(parent, self.scope):
                    entries.setdefault(parent, {"id": "d" * 40, "name": parts[i - 1], "type": "tree",
                                                "path": parent, "mode": "040000"})
        return [entries[key] for key in sorted(entries)]

    def archive_body(self):
        if self.body_override is not None:
            return self.body_override
        raw = io.BytesIO()
        pax = {"comment": self.comment} if self.comment is not None else {}
        members = [("dir", self.root, None)] + [("dir", f"{self.root}/{d}", None) for d in sorted(self.dirs)]
        members += list(reversed(self.members)) if self.reverse else self.members
        with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax) as tar:
            for kind, name, payload in members:
                info = tarfile.TarInfo(name)
                info.mtime = 0
                if kind == "file":
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
                    continue
                info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE,
                             "fifo": tarfile.FIFOTYPE, "chr": tarfile.CHRTYPE}[kind]
                if kind in ("sym", "hard"):
                    info.linkname = payload
                tar.addfile(info)
        return raw.getvalue()

    def next_url(self, token):
        query = {"ref": SHA, "recursive": "true", "pagination": "keyset", "per_page": "100"}
        if self.scope:
            query["path"] = self.scope
        query["page_token"] = str(token)
        url = f"{TREE_URL}?{urllib.parse.urlencode(query)}"
        return self.link_mutator(url) if self.link_mutator else url

    # -- transports -----------------------------------------------------------
    def transport(self, url, headers=None):
        self.calls.append(("api", url, headers))
        parsed = urllib.parse.urlsplit(url)
        if parsed.path.endswith("/repository/tags/10.0.5"):
            if self.tag_override:
                return self.tag_override
            return 200, {}, ('{"commit": {"id": "%s"}}' % self.tag_sha).encode()
        if parsed.path.endswith("/repository/tree"):
            token = int(urllib.parse.parse_qs(parsed.query).get("page_token", ["0"])[0])
            if self.tree_override and token == self.tree_override_page:
                return self.tree_override
            entries = self.tree_entries()
            chunk = entries[token * self.page_size:(token + 1) * self.page_size]
            out_headers = {}
            if (token + 1) * self.page_size < len(entries):
                out_headers["Link"] = f'<{self.next_url(token + 1)}>; rel="next"'
            return 200, out_headers, json.dumps(chunk).encode()
        raise AssertionError(f"unexpected API url {url}")

    def archive(self, url, headers, max_bytes):
        self.calls.append(("archive", url, headers))
        self.archive_max_bytes.append(max_bytes)
        if self.archive_override:
            return self.archive_override
        return 200, {}, self.archive_body()[:max_bytes + 1]

    def run(self, query="foo", path=None, **kw):
        path = self.scope if path is None else path
        return sc.search_source_code(query, path, transport=self.transport,
                                     archive_transport=self.archive, **kw)

    @property
    def archive_calls(self):
        return [c for c in self.calls if c[0] == "archive"]

    @property
    def tree_calls(self):
        return [c for c in self.calls if c[0] == "api" and "/repository/tree" in c[1]]


A_CPP = b"int Foo = 1;\nvoid bar() {}\nFOO foo Foo\n"
B_CPP = b"foobar\nmy_foo_x\nfoo\n"
C_H = b"#define FOO 1\n"


def base_world(**kw):
    world = World(**kw)
    world.add_file("pcbnew/a.cpp", A_CPP)
    world.add_file("pcbnew/b.cpp", B_CPP)
    world.add_file("pcbnew/sub/c.h", C_H)
    return world


def keys(result):
    return [(m["path"], m["line"], m["column"]) for m in result["matches"]]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Block network, tar extraction and disk writes; the cache starts empty."""
    monkeypatch.chdir(tmp_path)
    sc._search_cache.clear()
    writes = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and set(mode) & set("wax+"):
            writes.append((file, mode))
        return real_open(file, mode, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    def no_extract(*args, **kwargs):
        raise AssertionError("archive extraction attempted")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(sc._opener, "open", no_network)
    monkeypatch.setattr(tarfile.TarFile, "extract", no_extract)
    monkeypatch.setattr(tarfile.TarFile, "extractall", no_extract)
    yield writes
    assert writes == [], "search must not write to disk"
    assert list(tmp_path.iterdir()) == [], "search must not create files"
    sc._search_cache.clear()


# --------------------------------------------------------------------------
# Contract basics: matches, ordering, columns, data-not-instructions
# --------------------------------------------------------------------------
def test_basic_literal_search_shape_and_data_flag(isolated):
    result = base_world().run("foo")
    assert result["status"] == "matches"
    assert result["operation"] == "search_source_code"
    assert result["ref"]["resolved_sha"] == SHA
    assert result["path"] == "pcbnew"
    assert result["source_text_is_data"] is True
    assert result["total_matches"] == 8 and result["returned"] == 8 and result["next_cursor"] is None
    assert keys(result) == [
        ("pcbnew/a.cpp", 1, 5), ("pcbnew/a.cpp", 3, 1), ("pcbnew/a.cpp", 3, 5), ("pcbnew/a.cpp", 3, 9),
        ("pcbnew/b.cpp", 1, 1), ("pcbnew/b.cpp", 2, 4), ("pcbnew/b.cpp", 3, 1), ("pcbnew/sub/c.h", 1, 9)]
    first = result["matches"][0]
    assert first == {"path": "pcbnew/a.cpp", "line": 1, "column": 5, "text": "int Foo = 1;",
                     "text_truncated": False, "blob_id": blob_id(A_CPP)}
    cov = result["coverage"]
    assert cov["complete"] is True and cov["tree_blobs"] == 3 and cov["archive_files"] == 3
    assert cov["missing_in_archive"] == cov["unexpected_in_archive"] == cov["blob_mismatch"] == []
    assert isolated == []


def test_source_text_is_data_also_on_incomplete_and_no_match():
    assert base_world().run("zzz")["source_text_is_data"] is True
    world = base_world()
    world.tree_only("pcbnew/ghost.cpp", b"x")
    assert world.run("foo")["source_text_is_data"] is True


def test_whole_repository_scope_omits_path_params():
    world = World(scope="")
    world.add_file("README.md", b"foo readme\n")
    world.add_file("pcbnew/a.cpp", A_CPP)
    result = world.run("foo", path="")
    assert result["status"] == "matches" and result["path"] == ""
    assert "path=" not in world.tree_calls[0][1]
    assert "path=" not in world.archive_calls[0][1]
    assert result["coverage"]["tree_blobs"] == 2


def test_order_is_deterministic_regardless_of_archive_order():
    forward = base_world().run("foo")
    reverse_world = base_world()
    reverse_world.reverse = True
    sc._search_cache.clear()
    assert keys(reverse_world.run("foo")) == keys(forward)


def test_line_numbers_use_splitlines_and_are_one_based():
    world = World().add_file("pcbnew/x.txt", b"a\nfoo\r\nb\rfoo\n\nfoo")
    assert keys(world.run("foo")) == [("pcbnew/x.txt", 2, 1), ("pcbnew/x.txt", 4, 1), ("pcbnew/x.txt", 6, 1)]


def test_long_line_text_is_capped_and_centred_on_hit():
    line = "x" * 600 + "NEEDLE" + "y" * 600
    world = World().add_file("pcbnew/long.txt", (line + "\nshort NEEDLE\n").encode())
    first, second = world.run("NEEDLE")["matches"]
    assert first["column"] == 601 and first["text_truncated"] is True
    assert len(first["text"]) == sc.MAX_MATCH_TEXT_CHARS == 400 and "NEEDLE" in first["text"]
    assert first["text"].index("NEEDLE") in range(190, 210)
    assert second["text"] == "short NEEDLE" and second["text_truncated"] is False


# --------------------------------------------------------------------------
# Case folding, literal vs identifier
# --------------------------------------------------------------------------
def test_case_insensitive_is_default_and_sensitive_is_exact():
    world = base_world()
    assert world.run("foo")["total_matches"] == 8
    assert world.run("foo", case_sensitive=True)["total_matches"] == 4
    assert [m["column"] for m in world.run("Foo", case_sensitive=True)["matches"]] == [5, 9]
    assert world.run("FOO", case_sensitive=False)["total_matches"] == 8


def test_identifier_mode_requires_non_word_edges_but_literal_does_not():
    literal = base_world().run("foo", mode="literal")
    ident = base_world().run("foo", mode="identifier")
    assert literal["total_matches"] == 8 and ident["total_matches"] == 6
    ident_keys = keys(ident)
    assert ("pcbnew/b.cpp", 1, 1) not in ident_keys      # foobar
    assert ("pcbnew/b.cpp", 2, 4) not in ident_keys      # my_foo_x
    assert ("pcbnew/b.cpp", 3, 1) in ident_keys
    assert ("pcbnew/sub/c.h", 1, 9) in ident_keys        # case-insensitive, '#define FOO 1'


def test_identifier_edges_with_punctuation_digits_and_underscore():
    data = b"foo\nfoo_\n_foo\n1foo\nfoo1\n(foo)\nfoo.bar\nx->foo;\n"
    world = World().add_file("pcbnew/i.txt", data)
    assert [m["line"] for m in world.run("foo", mode="identifier")["matches"]] == [1, 6, 7, 8]
    assert world.run("foo", mode="literal")["total_matches"] == 8


def test_query_is_literal_not_a_pattern():
    world = World().add_file("pcbnew/r.txt", b"a.c\nabc\nf(x)\n[x]\na.*c\n")
    assert [m["line"] for m in world.run("a.c")["matches"]] == [1]
    assert [m["line"] for m in world.run("f(x)")["matches"]] == [3]
    assert [m["line"] for m in world.run("[x]")["matches"]] == [4]
    assert [m["line"] for m in world.run("a.*c")["matches"]] == [5]


# --------------------------------------------------------------------------
# Status: no_match only when complete
# --------------------------------------------------------------------------
def test_no_match_only_when_coverage_complete():
    result = base_world().run("zzz")
    assert result["status"] == "no_match" and result["total_matches"] == 0
    assert result["matches"] == [] and result["next_cursor"] is None and result["coverage"]["complete"] is True


@pytest.mark.parametrize("breakage", ["missing", "extra", "mismatch"])
def test_incomplete_coverage_never_reports_no_match(breakage):
    world = base_world()
    if breakage == "missing":
        world.tree_only("pcbnew/ghost.cpp", b"x")
    elif breakage == "extra":
        world.archive_only("pcbnew/extra.cpp", b"x")
    else:
        world.members[0] = ("file", f"{ROOT}/pcbnew/a.cpp", b"tampered\n")
    result = world.run("zzz")
    assert result["status"] == "incomplete" and result["status"] != "no_match"
    assert result["total_matches"] == 0 and result["coverage"]["complete"] is False


def test_incomplete_still_returns_matches_with_coverage_lists():
    world = base_world().tree_only("pcbnew/ghost.cpp", b"x")
    result = world.run("foo")
    assert result["status"] == "incomplete" and result["total_matches"] == 8
    cov = result["coverage"]
    assert cov["missing_in_archive"] == ["pcbnew/ghost.cpp"] and cov["missing_in_archive_count"] == 1
    assert cov["unexpected_in_archive_count"] == 0 and cov["blob_mismatch_count"] == 0
    assert cov["tree_blobs"] == 4 and cov["archive_files"] == 3


def test_unexpected_member_listed_and_incomplete():
    world = base_world().archive_only("pcbnew/extra.cpp", b"foo\n")
    result = world.run("foo")
    assert result["status"] == "incomplete"
    assert result["coverage"]["unexpected_in_archive"] == ["pcbnew/extra.cpp"]
    assert result["coverage"]["unexpected_in_archive_count"] == 1


def test_blob_mismatch_listed_incomplete_and_unproven_content_not_searched():
    world = base_world()
    world.members[0] = ("file", f"{ROOT}/pcbnew/a.cpp", b"TAMPERED foo\n")
    result = world.run("TAMPERED")
    assert result["status"] == "incomplete" and result["total_matches"] == 0
    assert result["coverage"]["blob_mismatch"] == ["pcbnew/a.cpp"]
    assert result["coverage"]["blob_mismatch_count"] == 1
    assert "pcbnew/a.cpp" not in {m["path"] for m in world.run("foo")["matches"]}


def test_coverage_lists_capped_at_200_with_exact_counts():
    world = World(page_size=100).add_file("pcbnew/a.cpp", A_CPP)
    for i in range(205):
        world.tree_only(f"pcbnew/ghost{i:03d}.cpp", b"x")
    cov = world.run("foo")["coverage"]
    assert len(cov["missing_in_archive"]) == 200 and cov["missing_in_archive_count"] == 205
    assert cov["list_cap"] == 200 and cov["complete"] is False


# --------------------------------------------------------------------------
# Member types: symlink, hardlink, others
# --------------------------------------------------------------------------
@pytest.mark.parametrize("target", ["a.cpp", "/etc/passwd", "../../../outside", "C:\\Windows\\win.ini"])
def test_symlink_is_classified_not_followed_and_not_searched(target):
    world = base_world().add_symlink("pcbnew/link", target)
    result = world.run("a.cpp")
    assert result["status"] == "no_match" and result["total_matches"] == 0   # link text is never searched
    cov = result["coverage"]
    assert cov["complete"] is True
    assert cov["symlinks"] == ["pcbnew/link"] and cov["symlinks_count"] == 1
    assert world.run("foo")["total_matches"] == 8


def test_symlink_target_hash_mismatch_is_blob_mismatch():
    world = base_world().add_symlink("pcbnew/link", "a.cpp", tree_target="other.cpp")
    result = world.run("foo")
    assert result["status"] == "incomplete"
    assert result["coverage"]["blob_mismatch"] == ["pcbnew/link"]


def test_symlink_absent_from_tree_is_unexpected():
    world = base_world()
    world._note_dirs("pcbnew/link")
    world.members.append(("sym", f"{ROOT}/pcbnew/link", "a.cpp"))
    result = world.run("foo")
    assert result["status"] == "incomplete"
    assert result["coverage"]["unexpected_in_archive"] == ["pcbnew/link"]


@pytest.mark.parametrize("kind", ["hard", "fifo", "chr"])
def test_hardlink_device_and_fifo_members_are_integrity_errors(kind):
    world = base_world()
    world.members.append((kind, f"{ROOT}/pcbnew/odd", "a.cpp" if kind == "hard" else None))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


# --------------------------------------------------------------------------
# Archive identity: names, root, pax SHA
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    f"{ROOT}/pcbnew/../../etc/passwd",
    f"{ROOT}/../evil.cpp",
    f"{ROOT}/pcbnew/sub/../../../x",
    "/etc/passwd",
    f"/{ROOT}/pcbnew/a.cpp",
    f"{ROOT}\\pcbnew\\x.cpp",
    f"{ROOT}/pcbnew//x.cpp",
    f"{ROOT}/./pcbnew/x.cpp",
])
def test_traversal_absolute_and_malformed_member_names_fail_whole_call(name):
    world = base_world()
    world.members.append(("file", name, b"foo\n"))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


@pytest.mark.parametrize("name", ["other-root/pcbnew/a.cpp", "lonefile", "pcbnew/a.cpp"])
def test_archive_root_mismatch_is_integrity_error(name):
    world = base_world()
    world.members.append(("file", name, b"foo\n"))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


def test_any_single_root_segment_is_accepted_and_stripped_once():
    world = World(root="whatever-root")
    world.add_file("pcbnew/a.cpp", A_CPP)
    assert world.run("foo")["status"] == "matches"
    sc._search_cache.clear()                 # same cache key as above; force a fresh archive fetch
    nested = World(root="top/nested")        # two leading segments: the second is not a repo dir
    nested.add_file("pcbnew/a.cpp", A_CPP)
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        nested.run("foo")


def test_member_outside_requested_scope_is_integrity_error():
    world = base_world()
    world.members.append(("file", f"{ROOT}/eeschema/x.cpp", b"foo\n"))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")
    world = base_world()
    world.members.append(("dir", f"{ROOT}/eeschema", None))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")
    sibling = base_world()                   # prefix-lookalike directory is not inside pcbnew
    sibling.members.append(("file", f"{ROOT}/pcbnew_evil/x.cpp", b"foo\n"))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        sibling.run("foo")


def test_duplicate_member_is_integrity_error():
    world = base_world()
    world.members.append(("file", f"{ROOT}/pcbnew/a.cpp", A_CPP))
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


@pytest.mark.parametrize("comment", [OTHER_SHA, "10.0.5", SHA.upper(), None])
def test_pax_comment_must_equal_resolved_sha(comment):
    world = base_world()
    world.comment = comment
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


@pytest.mark.parametrize("body", [b"not a gzip stream", b"", gzip.compress(b"garbage" * 100)])
def test_unreadable_archive_is_integrity_error(body):
    world = base_world()
    world.body_override = body
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


def test_tree_entries_outside_scope_or_malformed_are_integrity_errors():
    world = base_world()
    world.tree["eeschema/x.cpp"] = world._entry("eeschema/x.cpp", b"x")
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")
    world = base_world()
    world.tree["pcbnew/../x"] = world._entry("pcbnew/../x", b"x")
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")


# --------------------------------------------------------------------------
# Caps => scope_too_large (never a partial result)
# --------------------------------------------------------------------------
def test_compressed_archive_cap(monkeypatch):
    world = base_world()
    monkeypatch.setattr(sc, "MAX_ARCHIVE_BYTES", 16)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")
    assert world.archive_max_bytes == [16]


def test_member_size_cap(monkeypatch):
    world = base_world().add_file("pcbnew/big.txt", b"y" * 50)
    monkeypatch.setattr(sc, "MAX_MEMBER_BYTES", 40)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")


def test_member_count_cap(monkeypatch):
    world = base_world()
    monkeypatch.setattr(sc, "MAX_MEMBERS", 2)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")


def test_tree_page_cap(monkeypatch):
    world = base_world()                     # 4 tree entries, 2 per page => 2 pages
    monkeypatch.setattr(sc, "MAX_TREE_PAGES", 1)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")
    assert world.archive_calls == []


def test_decompression_bomb_single_member(monkeypatch):
    world = World().add_file("pcbnew/zero.bin", bytes(2 << 20))
    assert len(world.archive_body()) < 20_000            # tiny on the wire, large when inflated
    monkeypatch.setattr(sc, "MAX_DECOMPRESSED_BYTES", 64 * 1024)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")


def test_decompression_bomb_many_members(monkeypatch):
    world = World()
    for i in range(10):
        world.add_file(f"pcbnew/z{i}.bin", bytes(10 * 1024))
    monkeypatch.setattr(sc, "MAX_DECOMPRESSED_BYTES", 50 * 1024)
    with pytest.raises(sc.SourceCatalogError, match="scope_too_large"):
        world.run("foo")


def test_caps_at_limit_still_succeed(monkeypatch):
    world = base_world()
    monkeypatch.setattr(sc, "MAX_MEMBERS", 3 + 3)        # root + pcbnew + pcbnew/sub dirs + 3 files
    monkeypatch.setattr(sc, "MAX_TREE_PAGES", 2)
    assert world.run("foo")["status"] == "matches"


# --------------------------------------------------------------------------
# Binary / non-UTF-8 / submodules
# --------------------------------------------------------------------------
def test_binary_and_non_utf8_are_skipped_and_counted():
    world = base_world()
    world.add_file("pcbnew/blob.bin", b"\x00foo\x01foo")
    world.add_file("pcbnew/latin.txt", b"foo \xff\xfe bar\n")
    result = world.run("foo")
    cov = result["coverage"]
    assert cov["complete"] is True and result["status"] == "matches"
    assert cov["binary"] == ["pcbnew/blob.bin"] and cov["binary_count"] == 1
    assert cov["non_utf8"] == ["pcbnew/latin.txt"] and cov["non_utf8_count"] == 1
    assert result["total_matches"] == 8
    assert cov["tree_blobs"] == cov["archive_files"] == 5


def test_only_binary_or_non_utf8_hits_give_no_match_when_complete():
    world = World()
    world.add_file("pcbnew/blob.bin", b"\x00foo")
    world.add_file("pcbnew/latin.txt", b"foo \xff")
    result = world.run("foo")
    assert result["status"] == "no_match" and result["coverage"]["complete"] is True
    assert result["coverage"]["binary_count"] == 1 and result["coverage"]["non_utf8_count"] == 1


def test_nul_after_sniff_window_is_not_binary():
    data = b"foo\n" + b"x" * sc.BINARY_SNIFF_BYTES + b"\x00tail\n"
    result = World().add_file("pcbnew/late.txt", data).run("foo")
    assert result["coverage"]["binary_count"] == 0 and result["total_matches"] == 1


def test_submodule_listed_not_covered_and_not_a_discrepancy():
    world = base_world().add_submodule("pcbnew/thirdparty/dep")
    result = world.run("foo")
    cov = result["coverage"]
    assert cov["submodules_not_covered"] == ["pcbnew/thirdparty/dep"] and cov["submodules_not_covered_count"] == 1
    assert cov["complete"] is True and result["status"] == "matches"
    assert cov["tree_blobs"] == 3                        # the submodule is not counted as a blob
    assert world.run("zzz")["status"] == "no_match"


# --------------------------------------------------------------------------
# Network rules: Link validation, HTTP errors, 429
# --------------------------------------------------------------------------
def test_valid_link_pages_are_followed_and_inventory_is_complete():
    world = base_world()
    result = world.run("foo")
    assert len(world.tree_calls) == 2 and len(world.archive_calls) == 1
    assert result["coverage"]["tree_blobs"] == 3 and result["coverage"]["complete"] is True


@pytest.mark.parametrize("mutate", [
    lambda u: u.replace("https://gitlab.com", "https://evil.example"),
    lambda u: u.replace("https://gitlab.com", "https://gitlab.com.evil.example"),
    lambda u: u.replace("https://gitlab.com", "http://gitlab.com"),
    lambda u: u.replace("gitlab.com/", "gitlab.com:8443/"),
    lambda u: u.replace("https://gitlab.com", "https://user@gitlab.com"),
    lambda u: u.replace("ref=" + SHA, "ref=" + OTHER_SHA),
    lambda u: u.replace("ref=" + SHA, "ref=10.0.5"),
    lambda u: u.replace("ref=" + SHA, "ref=master"),
    lambda u: u.replace("path=pcbnew", "path=eeschema"),
    lambda u: u.replace("&path=pcbnew", ""),
    lambda u: u.replace("recursive=true", "recursive=false"),
    lambda u: u.replace("/repository/tree", "/repository/files"),
    lambda u: u.replace(sc.PROJECT_ID, "other%2Fproject"),
    lambda u: u + "#frag",
], ids=["foreign-host", "host-suffix", "http", "port", "userinfo", "other-sha", "tag-ref", "branch-ref",
        "other-path", "path-dropped", "recursive-changed", "other-endpoint", "other-project", "fragment"])
def test_foreign_or_changed_link_is_rejected_and_never_followed(mutate):
    world = base_world()
    world.link_mutator = mutate
    with pytest.raises(sc.SourceCatalogError, match="integrity_error"):
        world.run("foo")
    assert len(world.tree_calls) == 1                    # only the first page was ever requested
    assert world.archive_calls == []
    assert all(urllib.parse.urlsplit(c[1]).hostname == "gitlab.com" for c in world.calls)


def test_requests_are_pinned_to_resolved_sha_and_carry_no_credentials():
    world = base_world()
    world.run("foo")
    for kind, url, headers in world.calls:
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "gitlab.com"
        assert "authorization" not in {k.lower() for k in (headers or {})}
        assert "private-token" not in {k.lower() for k in (headers or {})}
        query = urllib.parse.parse_qs(parsed.query)
        if kind == "archive":
            assert query["sha"] == [SHA] and query["path"] == ["pcbnew"]
            assert parsed.path.endswith("/repository/archive.tar.gz")
        elif "/repository/tree" in url:
            assert query["ref"] == [SHA] and query["recursive"] == ["true"] and query["pagination"] == ["keyset"]
    assert world.archive_max_bytes == [sc.MAX_ARCHIVE_BYTES]


def test_tag_resolving_outside_pinned_prefix_is_refused_before_any_tree_request():
    world = base_world()
    world.tag_sha = "ab" * 20
    with pytest.raises(sc.SourceCatalogError, match="pinned prefix"):
        world.run("foo")
    assert world.tree_calls == [] and world.archive_calls == []


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502, 503])
@pytest.mark.parametrize("where", ["tag", "tree", "archive"])
def test_http_errors_raise_and_never_become_a_result(where, status):
    world = base_world()
    response = (status, {}, b"{}")
    if where == "tag":
        world.tag_override = response
    elif where == "tree":
        world.tree_override = response
    else:
        world.archive_override = response
    with pytest.raises(sc.SourceCatalogError, match=f"HTTP {status}"):
        world.run("zzz")


@pytest.mark.parametrize("where", ["tag", "tree", "tree-page-2", "archive"])
def test_429_raises_with_retry_after_and_never_no_match(where):
    world = base_world()
    response = (429, {"retry-after": "17"}, b"slow down")
    if where == "tag":
        world.tag_override = response
    elif where.startswith("tree"):
        world.tree_override = response
        world.tree_override_page = 1 if where.endswith("2") else 0
    else:
        world.archive_override = response
    with pytest.raises(sc.SourceCatalogError, match=r"HTTP 429.*Retry-After: 17"):
        world.run("zzz")


def test_429_without_retry_after_header_says_so():
    world = base_world()
    world.archive_override = (429, {}, b"")
    with pytest.raises(sc.SourceCatalogError, match=r"HTTP 429.*Retry-After: not provided"):
        world.run("zzz")


def test_default_transports_refuse_non_gitlab_hosts_without_connecting():
    with pytest.raises(sc.SourceCatalogError, match="non-GitLab"):
        sc._default_transport("https://evil.example/api/v4/x")
    with pytest.raises(sc.SourceCatalogError, match="non-GitLab"):
        sc._default_transport("http://gitlab.com/api/v4/x")
    with pytest.raises(sc.SourceCatalogError, match="non-GitLab"):
        sc._default_archive_transport("https://evil.example/archive.tar.gz", {}, 10)


# --------------------------------------------------------------------------
# Input validation happens before any network call
# --------------------------------------------------------------------------
BAD_ARGS = (
    [("query", v) for v in ["", "a" * 201, "a\nb", "a\rb", "a\x00b", 123, None, b"foo"]]
    + [("path", v) for v in ["../x", "/abs", "a//b", "a\\b", "a/./b", "a/..", "x\x00y", 5]]
    + [("mode", v) for v in ["regex", "REGEX", "", "glob", "Literal", None]]
    + [("case_sensitive", v) for v in ["yes", 1, None]]
    + [("max_matches", v) for v in [0, 501, -1, "10", True, 1.5, None]]
    + [("cursor", v) for v in [-1, "0", True, 1.5, None]]
)


@pytest.mark.parametrize("name,value", BAD_ARGS, ids=[f"{n}={v!r}"[:40] for n, v in BAD_ARGS])
def test_invalid_arguments_rejected_with_no_network_call(name, value):
    world = base_world()
    with pytest.raises(sc.SourceCatalogError):
        world.run(**{name: value})
    assert world.calls == []


def test_regex_mode_error_names_regex():
    with pytest.raises(sc.SourceCatalogError, match="regex"):
        base_world().run("foo.*", mode="regex")


def test_boundary_values_are_accepted():
    world = base_world()
    assert world.run("a" * 200)["status"] == "no_match"
    assert world.run("foo", max_matches=1)["returned"] == 1
    assert world.run("foo", max_matches=500)["returned"] == 8
    assert world.run("foo", cursor=0)["cursor"] == 0


# --------------------------------------------------------------------------
# Pagination and LRU cache
# --------------------------------------------------------------------------
def test_cursor_pages_are_exact_deterministic_and_cost_one_download():
    world = base_world()
    pages = [world.run("foo", max_matches=3, cursor=c) for c in (0, 3, 6)]
    assert [p["total_matches"] for p in pages] == [8, 8, 8]
    assert [p["returned"] for p in pages] == [3, 3, 2]
    assert [p["next_cursor"] for p in pages] == [3, 6, None]
    assert [p["cursor"] for p in pages] == [0, 3, 6]
    joined = [k for p in pages for k in keys(p)]
    assert joined == keys(base_world().run("foo")) and len(set(joined)) == 8
    assert len(world.archive_calls) == 1 and len(world.tree_calls) == 2   # later pages need no download
    past = world.run("foo", max_matches=3, cursor=100)
    assert past["returned"] == 0 and past["matches"] == [] and past["next_cursor"] is None
    assert past["total_matches"] == 8 and past["status"] == "matches"


def test_exact_page_boundary_has_no_trailing_cursor():
    world = base_world()
    page = world.run("foo", max_matches=8)
    assert page["returned"] == 8 and page["next_cursor"] is None
    assert world.run("foo", max_matches=7)["next_cursor"] == 7


def test_cache_key_separates_mode_case_and_query():
    world = base_world()
    world.run("foo")
    world.run("foo", mode="identifier")
    world.run("foo", case_sensitive=True)
    world.run("Foo")
    assert len(world.archive_calls) == 4
    world.run("foo")
    world.run("foo", mode="identifier", cursor=1)
    assert len(world.archive_calls) == 4


def test_cache_is_lru_bounded_at_eight_entries():
    assert sc.SEARCH_CACHE_ENTRIES == 8
    world = base_world()
    for i in range(9):
        world.run(f"q{i}")
    assert len(world.archive_calls) == 9 and len(sc._search_cache) == 8
    world.run("q8")                                      # newest is still cached
    assert len(world.archive_calls) == 9
    world.run("q0")                                      # oldest was evicted => downloaded again
    assert len(world.archive_calls) == 10 and len(sc._search_cache) == 8


def test_cache_holds_matches_and_coverage_never_file_contents():
    marker = "UNMATCHED_SECRET_FILE_BODY_MARKER"
    world = World().add_file("pcbnew/m.txt", f"{marker}\nfoo here\n".encode())
    world.add_file("pcbnew/blob.bin", b"\x00" + marker.encode())
    world.run("foo")
    assert len(sc._search_cache) == 1

    def walk(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise AssertionError("cache holds raw bytes")
        if isinstance(value, str):
            assert marker not in value
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(k), walk(v)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    for key, value in sc._search_cache.items():
        walk(key)
        matches, coverage = value
        assert isinstance(matches, list) and isinstance(coverage, dict)
        walk(value)


def test_mutating_a_returned_page_does_not_corrupt_cached_pages():
    world = base_world()
    first = world.run("foo")
    first["matches"][0]["text"] = "CHANGED"
    first["matches"].clear()
    again = world.run("foo")
    assert again["matches"][0]["text"] == "int Foo = 1;" and again["total_matches"] == 8
    assert len(world.archive_calls) == 1


def test_failed_search_is_not_cached():
    world = base_world()
    world.comment = OTHER_SHA
    with pytest.raises(sc.SourceCatalogError):
        world.run("foo")
    assert len(sc._search_cache) == 0
    world.comment = SHA
    assert world.run("foo")["status"] == "matches"
    assert len(world.archive_calls) == 2


# --------------------------------------------------------------------------
# Nothing touches the disk
# --------------------------------------------------------------------------
def test_search_with_every_member_type_writes_nothing_to_disk(isolated, tmp_path):
    world = base_world()
    world.add_symlink("pcbnew/link", "/etc/passwd")
    world.add_file("pcbnew/blob.bin", b"\x00\x01")
    world.add_submodule("pcbnew/dep")
    result = world.run("foo", max_matches=2)
    assert result["status"] == "matches"
    assert isolated == []
    assert list(tmp_path.iterdir()) == []
