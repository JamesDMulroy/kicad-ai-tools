"""Pinned KiCad 10.0.5 GitLab source discovery.

Read-only access to the official KiCad GitLab mirror at a single pinned tag.
No caller-supplied URL, token, repository, ref, or executable content is ever
accepted; every request targets ``gitlab.com`` over HTTPS with redirects
refused, and every tree/file read is bound to a commit SHA resolved from the
pinned tag and verified against its known short prefix.
"""
from __future__ import annotations

import base64
import functools
import gzip
import hashlib
import io
import json
import re
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from typing import Any, Callable

GITLAB_HOST = "gitlab.com"
GITLAB_API_BASE = f"https://{GITLAB_HOST}/api/v4"
PROJECT_PATH = "kicad/code/kicad"
PROJECT_ID = urllib.parse.quote(PROJECT_PATH, safe="")
PINNED_TAG = "10.0.5"
PINNED_SHA_PREFIX = "18fb9289"
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_CHUNK_BYTES = 1_048_576  # Suggested chunk size; callers may request any positive length.
CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")

# Search caps (design: docs/SOURCE-SEARCH-AND-TRUST-BOUNDARY.md section 2).
ARCHIVE_TIMEOUT_SECONDS = 60
# Response byte caps and wall-clock deadlines for the non-archive transport.
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_RAW_FILE_HARD_CEILING_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
REQUEST_DEADLINE_SECONDS = 60
ARCHIVE_DEADLINE_SECONDS = 180
_monotonic = time.monotonic
MAX_TREE_PAGES = 300
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 192 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 40_000
MAX_COVERAGE_PATHS = 200
MAX_MATCH_TEXT_CHARS = 400
BINARY_SNIFF_BYTES = 8000
SEARCH_MODES = ("literal", "identifier")
SEARCH_CACHE_ENTRIES = 8
LINK_NEXT = re.compile(r'<([^>]*)>\s*;[^,]*?\brel="?next"?', re.IGNORECASE)


class SourceCatalogError(Exception):
    """Raised for unsafe, malformed, or failed pinned-source requests."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """GitLab's public API never redirects these endpoints; treat any redirect as a hazard."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise SourceCatalogError(f"Refusing HTTP redirect ({code}) to {newurl!r}")


_opener = urllib.request.build_opener(_NoRedirect)

# (url, headers) -> (status, headers, body)
Transport = Callable[[str, "dict[str, str] | None"], "tuple[int, dict[str, str], bytes]"]


def _read_bounded(fp: Any, max_bytes: int, deadline: float, what: str, overflow_ok: bool = False) -> bytes:
    """Read ``fp`` in bounded chunks, never more than ``max_bytes + 1`` bytes.

    Raises ``response_too_large`` when more than ``max_bytes`` arrive (unless ``overflow_ok``, where
    the caller owns the over-cap check) and ``deadline_exceeded`` once the monotonic ``deadline`` passes.
    """
    limit = max_bytes + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        if _monotonic() > deadline:
            raise SourceCatalogError(f"deadline_exceeded: {what} was not received within the request deadline")
        chunk = fp.read(min(READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if _monotonic() > deadline:
        raise SourceCatalogError(f"deadline_exceeded: {what} was not received within the request deadline")
    if total > max_bytes and not overflow_ok:
        raise SourceCatalogError(f"response_too_large: {what} exceeds {max_bytes} bytes")
    return b"".join(chunks)


def _default_transport(url: str, headers: dict[str, str] | None = None,
                       max_bytes: int | None = None) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != GITLAB_HOST:
        raise SourceCatalogError(f"Refusing request to non-GitLab host {parsed.hostname!r}")
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    deadline = _monotonic() + REQUEST_DEADLINE_SECONDS
    cap = MAX_JSON_RESPONSE_BYTES if max_bytes is None else max_bytes
    try:
        with _opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = _read_bounded(response, cap, deadline, "response body")
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        body = _read_bounded(exc, MAX_ERROR_BODY_BYTES, deadline, "error response body")
        return exc.code, dict(exc.headers or {}), body
    except urllib.error.URLError as exc:
        raise SourceCatalogError(f"GitLab request failed: {exc.reason}") from exc


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _retry_after_suffix(status: int, headers: dict[str, str] | None) -> str:
    if status == 429:
        return f" (Retry-After: {_header(headers, 'Retry-After') or 'not provided'})"
    return ""


# (url, headers, max_bytes) -> (status, headers, body); body is at most max_bytes + 1 bytes.
ArchiveTransport = Callable[[str, "dict[str, str] | None", int], "tuple[int, dict[str, str], bytes]"]


def _default_archive_transport(url: str, headers: dict[str, str] | None = None,
                               max_bytes: int = MAX_ARCHIVE_BYTES) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != GITLAB_HOST:
        raise SourceCatalogError(f"Refusing request to non-GitLab host {parsed.hostname!r}")
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    deadline = _monotonic() + ARCHIVE_DEADLINE_SECONDS
    try:
        with _opener.open(request, timeout=ARCHIVE_TIMEOUT_SECONDS) as response:
            body = _read_bounded(response, max_bytes, deadline, "archive body", overflow_ok=True)
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(65536)
    except urllib.error.URLError as exc:
        raise SourceCatalogError(f"GitLab request failed: {exc.reason}") from exc


def _get_json(path_and_query: str, transport: Transport) -> tuple[Any, dict[str, str]]:
    url = f"{GITLAB_API_BASE}{path_and_query}"
    status, headers, body = transport(url, {"Accept": "application/json"})
    if status != 200:
        raise SourceCatalogError(
            f"GitLab API returned HTTP {status} for {path_and_query}{_retry_after_suffix(status, headers)}")
    try:
        return json.loads(body.decode("utf-8")), headers
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceCatalogError("GitLab API returned malformed JSON") from exc


def _normalize_repo_path(path: str) -> str:
    """Reject traversal, absolute paths, and anything that is not a plain relative repo path."""
    if not isinstance(path, str):
        raise SourceCatalogError("path must be a string")
    if path in ("", "."):
        return ""
    if "\x00" in path or "\\" in path:
        raise SourceCatalogError("path contains unsupported characters")
    if path.startswith("/"):
        raise SourceCatalogError("path must be relative to the repository root")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise SourceCatalogError(f"path segment {segment!r} is not permitted")
    return "/".join(segments)


def resolve_pinned_commit(transport: Transport = _default_transport) -> dict[str, Any]:
    """Resolve the pinned 10.0.5 tag to its full commit SHA and verify the known short prefix."""
    encoded_tag = urllib.parse.quote(PINNED_TAG, safe="")
    data, _headers = _get_json(f"/projects/{PROJECT_ID}/repository/tags/{encoded_tag}", transport)
    commit = data.get("commit") if isinstance(data, dict) else None
    sha = commit.get("id") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise SourceCatalogError("GitLab tag response did not contain a valid 40-character commit SHA")
    if not sha.lower().startswith(PINNED_SHA_PREFIX.lower()):
        raise SourceCatalogError(
            f"Resolved commit {sha} does not match the expected pinned prefix "
            f"{PINNED_SHA_PREFIX!r}; refusing to trust an unverified tag resolution")
    return {"tag": PINNED_TAG, "resolved_sha": sha, "verified_prefix": PINNED_SHA_PREFIX, "verified": True}


def list_source_tree(path: str = "", page: int = 1, per_page: int = 50,
                      transport: Transport = _default_transport) -> dict[str, Any]:
    """List the pinned tree at a repo-relative path. Paginated; never silently truncated."""
    normalized = _normalize_repo_path(path)
    if not (1 <= per_page <= 100):
        raise SourceCatalogError("per_page must be between 1 and 100")
    if page < 1:
        raise SourceCatalogError("page must be >= 1")
    pin = resolve_pinned_commit(transport)
    query = {"ref": pin["resolved_sha"], "page": str(page), "per_page": str(per_page), "recursive": "false"}
    if normalized:
        query["path"] = normalized
    data, headers = _get_json(
        f"/projects/{PROJECT_ID}/repository/tree?{urllib.parse.urlencode(query)}", transport)
    if not isinstance(data, list):
        raise SourceCatalogError("GitLab tree response was not a list")
    entries = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            entry_path = _normalize_repo_path(item.get("path"))
        except SourceCatalogError as exc:
            raise _integrity(f"tree entry path rejected: {exc}") from exc
        parent, _sep, last = entry_path.rpartition("/")
        if not entry_path or parent != normalized:
            raise _integrity(f"tree entry {entry_path!r} is not a direct child of {normalized!r}")
        if item.get("name") != last:
            raise _integrity(f"tree entry name {item.get('name')!r} does not match its path {entry_path!r}")
        entries.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "type": item.get("type"),
            "path": item.get("path"),
            "mode": item.get("mode"),
        })
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    next_page = normalized_headers.get("x-next-page") or None
    total_pages = normalized_headers.get("x-total-pages") or None
    return {
        "ref": pin,
        "path": normalized,
        "page": page,
        "per_page": per_page,
        "entries": entries,
        "next_page": int(next_page) if next_page else None,
        "total_pages": int(total_pages) if total_pages else None,
    }


def read_source_file(path: str, offset: int = 0, length: int = DEFAULT_CHUNK_BYTES,
                      transport: Transport = _default_transport) -> dict[str, Any]:
    """Stream a pinned file by byte range; returns a continuation offset until ``done`` is true."""
    normalized = _normalize_repo_path(path)
    if not normalized:
        raise SourceCatalogError("path must name a file")
    if offset < 0:
        raise SourceCatalogError("offset must be >= 0")
    if length <= 0:
        raise SourceCatalogError("length must be positive")
    if length > MAX_RAW_FILE_HARD_CEILING_BYTES:
        raise SourceCatalogError(
            f"length exceeds the {MAX_RAW_FILE_HARD_CEILING_BYTES}-byte per-read ceiling; request a smaller range")
    pin = resolve_pinned_commit(transport)
    encoded_path = urllib.parse.quote(normalized, safe="")
    meta, _headers = _get_json(
        f"/projects/{PROJECT_ID}/repository/files/{encoded_path}?ref={pin['resolved_sha']}", transport)
    if not isinstance(meta, dict):
        raise SourceCatalogError("GitLab file metadata response was malformed")
    total_size = meta.get("size")
    if not isinstance(total_size, int) or total_size < 0:
        raise SourceCatalogError("GitLab file metadata did not report a valid size")
    if total_size and offset >= total_size:
        raise SourceCatalogError("offset is beyond end of file")
    url = f"{GITLAB_API_BASE}/projects/{PROJECT_ID}/repository/files/{encoded_path}/raw?ref={pin['resolved_sha']}"
    end = offset + length - 1
    range_header = {"Range": f"bytes={offset}-{end}"} if total_size else {}
    fetch = transport
    if transport is _default_transport:
        raw_cap = min(max(length, total_size), MAX_RAW_FILE_HARD_CEILING_BYTES)
        fetch = functools.partial(_default_transport, max_bytes=raw_cap)
    status, response_headers, body = fetch(url, range_header)
    if status not in (200, 206):
        raise SourceCatalogError(f"GitLab raw file fetch returned HTTP {status}")
    if status == 200:
        # GitLab's raw endpoint can ignore Range. A 200 response represents the
        # complete file, so slice locally instead of mislabeling its bytes.
        if len(body) != total_size:
            raise SourceCatalogError("GitLab returned an incomplete full-file response")
        chunk = body[offset:offset + length]
    else:
        content_range = next((value for key, value in response_headers.items()
                              if key.lower() == "content-range"), None)
        match = CONTENT_RANGE.fullmatch(content_range or "")
        if match is None:
            raise SourceCatalogError("GitLab partial response omitted a valid Content-Range")
        start, end, reported_total = map(int, match.groups())
        if (start != offset or reported_total != total_size or end < start
                or end >= total_size or end - start + 1 != len(body)):
            raise SourceCatalogError("GitLab partial response range disagrees with file metadata")
        if len(body) > length:
            raise SourceCatalogError("GitLab partial response exceeded the requested length")
        chunk = body
    if not chunk and total_size:
        raise SourceCatalogError("GitLab returned no bytes before end of file")
    next_offset = offset + len(chunk)
    done = next_offset >= total_size
    return {
        "ref": pin,
        "path": normalized,
        "blob_id": meta.get("blob_id"),
        "content_sha256_full_file": meta.get("content_sha256"),
        "total_size": total_size,
        "offset": offset,
        "returned_bytes": len(chunk),
        "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
        "next_offset": None if done else next_offset,
        "done": done,
        "content_base64": base64.b64encode(chunk).decode("ascii"),
    }


def _too_large(detail: str) -> SourceCatalogError:
    return SourceCatalogError(f"scope_too_large: {detail}; narrow the path")


def _integrity(detail: str) -> SourceCatalogError:
    return SourceCatalogError(f"integrity_error: {detail}")


def _check_http_ok(status: int, headers: dict[str, str] | None, what: str) -> None:
    if status != 200:
        raise SourceCatalogError(f"GitLab {what} returned HTTP {status}{_retry_after_suffix(status, headers)}")


def _in_scope(path: str, scope: str) -> bool:
    return not scope or path == scope or path.startswith(scope + "/")


def _validate_search_inputs(query: Any, path: Any, mode: Any, case_sensitive: Any,
                            max_matches: Any, cursor: Any) -> str:
    if not isinstance(query, str) or not (1 <= len(query) <= 200):
        raise SourceCatalogError("query must be a string of 1-200 characters")
    if any(c in query for c in ("\x00", "\r", "\n")):
        raise SourceCatalogError("query must not contain NUL, CR, or LF")
    scope = _normalize_repo_path(path)
    if mode not in SEARCH_MODES:
        raise SourceCatalogError(
            f"mode must be one of {list(SEARCH_MODES)}; regex search is deliberately not offered")
    if not isinstance(case_sensitive, bool):
        raise SourceCatalogError("case_sensitive must be a boolean")
    for name, value, low, high in (("max_matches", max_matches, 1, 500), ("cursor", cursor, 0, None)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceCatalogError(f"{name} must be an integer")
        if value < low or (high is not None and value > high):
            raise SourceCatalogError(f"{name} must be between {low} and {high}" if high
                                     else f"{name} must be >= {low}")
    return scope


def _validate_next_link(url: str, sha: str, scope: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    expected_path = f"/api/v4/projects/{PROJECT_ID}/repository/tree"
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (parsed.scheme != "https" or parsed.hostname != GITLAB_HOST or port is not None
            or parsed.username is not None or parsed.fragment
            or urllib.parse.unquote(parsed.path) != urllib.parse.unquote(expected_path)):
        raise _integrity(f"refusing pagination Link to unexpected location {url!r}")
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key, want in (("ref", [sha]), ("recursive", ["true"]), ("path", [scope] if scope else [])):
        if params.get(key, []) != want:
            raise _integrity(f"pagination Link changed {key!r}; refusing to follow it")
    return url


def _inventory_tree(sha: str, scope: str, transport: Transport) -> tuple[dict[str, tuple[str, str]], list[str]]:
    query = {"ref": sha, "recursive": "true", "pagination": "keyset", "per_page": "100"}
    if scope:
        query["path"] = scope
    url: str | None = (f"{GITLAB_API_BASE}/projects/{PROJECT_ID}/repository/tree?"
                       f"{urllib.parse.urlencode(query)}")
    blobs: dict[str, tuple[str, str]] = {}
    submodules: list[str] = []
    pages = 0
    while url is not None:
        if pages >= MAX_TREE_PAGES:
            raise _too_large(f"tree listing exceeds {MAX_TREE_PAGES} pages")
        status, headers, body = transport(url, {"Accept": "application/json"})
        _check_http_ok(status, headers, "tree listing")
        pages += 1
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceCatalogError("GitLab API returned malformed JSON") from exc
        if not isinstance(data, list):
            raise SourceCatalogError("GitLab tree response was not a list")
        for item in data:
            if not isinstance(item, dict):
                raise _integrity("tree entry was not an object")
            entry_type, entry_mode = item.get("type"), str(item.get("mode"))
            if entry_type == "tree":
                continue
            entry_path = item.get("path")
            try:
                entry_path = _normalize_repo_path(entry_path)
            except SourceCatalogError as exc:
                raise _integrity(f"tree entry path rejected: {exc}") from exc
            if not entry_path or not _in_scope(entry_path, scope):
                raise _integrity(f"tree entry {entry_path!r} lies outside the requested scope")
            if entry_type == "commit" or entry_mode == "160000":
                submodules.append(entry_path)
            elif entry_type == "blob":
                if not isinstance(item.get("id"), str):
                    raise _integrity(f"tree blob {entry_path!r} has no id")
                blobs[entry_path] = (item["id"].lower(), entry_mode)
            else:
                raise _integrity(f"unexpected tree entry type {entry_type!r}")
        link = _header(headers, "Link")
        found = LINK_NEXT.search(link) if link else None
        url = _validate_next_link(found.group(1), sha, scope) if found else None
    return blobs, submodules


class _BoundedReader:
    """Counts decompressed tar-stream bytes so a decompression bomb is stopped early."""

    def __init__(self, raw: Any, limit: int):
        self._raw, self._limit, self._total = raw, limit, 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > 1 << 20:
            size = 1 << 20
        data = self._raw.read(size)
        self._total += len(data)
        if self._total > self._limit:
            raise _too_large(f"decompressed archive exceeds {self._limit} bytes")
        return data


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _scan_text(path: str, blob_id: str, text: str, pattern: "re.Pattern[str]", query_len: int,
               out: list[dict[str, Any]]) -> None:
    for line_no, line in enumerate(text.splitlines(), start=1):
        for hit in pattern.finditer(line):
            col0 = hit.start()
            truncated = len(line) > MAX_MATCH_TEXT_CHARS
            if truncated:
                start = max(0, min(col0 + query_len // 2 - MAX_MATCH_TEXT_CHARS // 2,
                                   len(line) - MAX_MATCH_TEXT_CHARS))
                snippet = line[start:start + MAX_MATCH_TEXT_CHARS]
            else:
                snippet = line
            out.append({"path": path, "line": line_no, "column": col0 + 1, "text": snippet,
                        "text_truncated": truncated, "blob_id": blob_id})


def _scan_archive(body: bytes, sha: str, scope: str, blobs: dict[str, tuple[str, str]],
                  pattern: "re.Pattern[str]", query_len: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the gzip tar fully in memory. Nothing is extracted, written to disk, or followed."""
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    symlinks: list[str] = []
    mismatched: list[str] = []
    unexpected: list[str] = []
    binary: list[str] = []
    non_utf8: list[str] = []
    root: str | None = None
    members = 0
    total_bytes = 0
    try:
        reader = _BoundedReader(gzip.GzipFile(fileobj=io.BytesIO(body)), MAX_DECOMPRESSED_BYTES)
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise _too_large(f"archive has more than {MAX_MEMBERS} members")
                if member.size > MAX_MEMBER_BYTES:
                    raise _too_large(f"archive member exceeds {MAX_MEMBER_BYTES} bytes")
                try:
                    name = _normalize_repo_path(member.name.rstrip("/") if member.isdir() else member.name)
                except SourceCatalogError as exc:
                    raise _integrity(f"archive member name rejected: {exc}") from exc
                head, _sep, rest = name.partition("/")
                if not head:
                    raise _integrity("archive member has no root segment")
                if root is None:
                    root = head
                elif head != root:
                    raise _integrity("archive members do not share a single root segment")
                if member.isdir():
                    if rest and not (_in_scope(rest, scope) or scope.startswith(rest + "/")):
                        raise _integrity(f"archive directory {rest!r} lies outside the requested scope")
                    continue
                if not rest or not _in_scope(rest, scope):
                    raise _integrity(f"archive member {rest!r} lies outside the requested scope")
                if rest in seen:
                    raise _integrity(f"archive member {rest!r} appears more than once")
                seen.add(rest)
                if member.issym():
                    data = member.linkname.encode("utf-8", "surrogateescape")
                    symlinks.append(rest)
                    verified = rest in blobs and _git_blob_id(data) == blobs[rest][0]
                    if rest not in blobs:
                        unexpected.append(rest)
                    elif not verified:
                        mismatched.append(rest)
                    continue
                if not member.isreg():
                    raise _integrity(f"archive member {rest!r} has unsupported type {member.type!r}")
                handle = tar.extractfile(member)
                data = handle.read(MAX_MEMBER_BYTES + 1) if handle is not None else b""
                if len(data) > MAX_MEMBER_BYTES:
                    raise _too_large(f"archive member exceeds {MAX_MEMBER_BYTES} bytes")
                if len(data) != member.size:
                    raise _integrity(f"archive member {rest!r} is shorter than its header size")
                total_bytes += len(data)
                if total_bytes > MAX_DECOMPRESSED_BYTES:
                    raise _too_large(f"decompressed archive exceeds {MAX_DECOMPRESSED_BYTES} bytes")
                blob_id = _git_blob_id(data)
                if rest not in blobs:
                    unexpected.append(rest)
                    continue
                if blob_id != blobs[rest][0]:
                    mismatched.append(rest)
                    continue  # content not proven to belong to the pinned commit: never searched
                if b"\x00" in data[:BINARY_SNIFF_BYTES]:
                    binary.append(rest)
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    non_utf8.append(rest)
                    continue
                _scan_text(rest, blob_id, text, pattern, query_len, matches)
            comment = tar.pax_headers.get("comment")
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise _integrity(f"archive could not be read: {type(exc).__name__}: {exc}") from exc
    if root is None:
        raise _integrity("archive contained no members")
    if comment != sha:
        raise _integrity("archive pax comment does not match the resolved commit SHA")
    matches.sort(key=lambda m: (m["path"], m["line"], m["column"]))
    return matches, {"archive_paths": seen, "symlinks": symlinks, "blob_mismatch": mismatched,
                     "unexpected": unexpected, "binary": binary, "non_utf8": non_utf8}


def _capped(paths: list[str]) -> list[str]:
    return sorted(paths)[:MAX_COVERAGE_PATHS]


def _build_coverage(blobs: dict[str, tuple[str, str]], submodules: list[str],
                    found: dict[str, Any]) -> dict[str, Any]:
    archive_paths: set[str] = found["archive_paths"]
    missing = sorted(set(blobs) - archive_paths)
    unexpected = sorted(found["unexpected"])
    mismatched = sorted(found["blob_mismatch"])
    coverage: dict[str, Any] = {
        "tree_blobs": len(blobs),
        "archive_files": len(archive_paths),
        "complete": not (missing or unexpected or mismatched),
        "list_cap": MAX_COVERAGE_PATHS,
    }
    for key, paths in (("missing_in_archive", missing), ("unexpected_in_archive", unexpected),
                       ("blob_mismatch", mismatched), ("binary", found["binary"]),
                       ("non_utf8", found["non_utf8"]), ("symlinks", found["symlinks"]),
                       ("submodules_not_covered", submodules)):
        coverage[key] = _capped(paths)
        coverage[f"{key}_count"] = len(paths)
    return coverage


_search_cache: "OrderedDict[tuple, tuple[list[dict[str, Any]], dict[str, Any]]]" = OrderedDict()
_search_cache_lock = threading.Lock()


def _cache_get(key: tuple):
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry is not None:
            _search_cache.move_to_end(key)
        return entry


def _cache_put(key: tuple, matches: list[dict[str, Any]], coverage: dict[str, Any]) -> None:
    with _search_cache_lock:  # match lists and coverage only, never file contents
        _search_cache[key] = (matches, coverage)
        _search_cache.move_to_end(key)
        while len(_search_cache) > SEARCH_CACHE_ENTRIES:
            _search_cache.popitem(last=False)


def search_source_code(query: str, path: str = "", mode: str = "literal", case_sensitive: bool = False,
                       max_matches: int = 100, cursor: int = 0, transport: Transport = _default_transport,
                       archive_transport: ArchiveTransport | None = None) -> dict[str, Any]:
    """Client-side search over a commit-pinned, path-scoped archive proven against the tree listing.

    Matches are exact for the whole scope (full scan, paginated by ``cursor``). ``no_match`` is only
    reported when coverage is complete; oversized scopes raise ``scope_too_large``.
    """
    scope = _validate_search_inputs(query, path, mode, case_sensitive, max_matches, cursor)
    pin = resolve_pinned_commit(transport)
    sha = pin["resolved_sha"]
    key = (sha, scope, query, mode, case_sensitive)
    cached = _cache_get(key)
    if cached is None:
        blobs, submodules = _inventory_tree(sha, scope, transport)
        archive_query = {"sha": sha}
        if scope:
            archive_query["path"] = scope
        archive_url = (f"{GITLAB_API_BASE}/projects/{PROJECT_ID}/repository/archive.tar.gz?"
                       f"{urllib.parse.urlencode(archive_query)}")
        fetch = archive_transport or _default_archive_transport
        status, headers, body = fetch(archive_url, {}, MAX_ARCHIVE_BYTES)
        _check_http_ok(status, headers, "archive download")
        if len(body) > MAX_ARCHIVE_BYTES:
            raise _too_large(f"compressed archive exceeds {MAX_ARCHIVE_BYTES} bytes")
        body_pattern = re.escape(query)
        if mode == "identifier":
            body_pattern = rf"(?<![A-Za-z0-9_]){body_pattern}(?![A-Za-z0-9_])"
        pattern = re.compile(body_pattern, 0 if case_sensitive else re.IGNORECASE)
        matches, found = _scan_archive(body, sha, scope, blobs, pattern, len(query))
        coverage = _build_coverage(blobs, submodules, found)
        _cache_put(key, matches, coverage)
    else:
        matches, coverage = cached
    total = len(matches)
    page = matches[cursor:cursor + max_matches]
    next_cursor = cursor + len(page) if cursor + len(page) < total else None
    if not coverage["complete"]:
        status_name = "incomplete"
    else:
        status_name = "matches" if total else "no_match"
    return {
        "status": status_name,
        "operation": "search_source_code",
        "ref": pin,
        "path": scope,
        "query": query,
        "mode": mode,
        "case_sensitive": case_sensitive,
        "total_matches": total,
        "cursor": cursor,
        "returned": len(page),
        "next_cursor": next_cursor,
        "matches": [dict(m) for m in page],
        "coverage": {k: list(v) if isinstance(v, list) else v for k, v in coverage.items()},
        "source_text_is_data": True,
    }
