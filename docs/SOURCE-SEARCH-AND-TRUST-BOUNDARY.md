# Pinned source search (F03) and source/build trust boundary (F04)

Status, 2026-10-10:
- §2 search is **implemented and supported**. It was implemented, independently
  reviewed, live-exercised (development receipt, not published) and then promoted.
- §3 is the F04 trust-boundary **design**, revised after an independent advisory
  review. That review showed the first draft
  claimed rules the code does not enforce. §3.2a now lists the P0 transport
  bounds that `source_catalog.py` enforces, including the response byte caps,
  request deadlines and `list_source_tree` entry validation that review found
  missing. They are tested in `tests/test_transport_bounds.py`. §3.2b keeps the
  one documented residual (proxy environment).
- P1–P4 do not exist.

## 1. Provider facts, verified live 2026-10-10 (read-only GETs, unauthenticated)

| Probe at commit `18fb9289ff0efdca53c0352ed81a0973f0a6b58c` (tag 10.0.5) | Result |
|---|---|
| `GET /projects/:id/search?scope=blobs&ref=<sha>` | **HTTP 401.** GitLab code search needs a credential. This package accepts none, so provider-side search stays unavailable. |
| `GET /repository/tree?ref=<sha>&path=pcbnew/router&recursive=true&pagination=keyset&per_page=100` | 200, 85 entries. |
| `GET /repository/tree?...path=pcbnew&recursive=true&pagination=keyset` | 15 pages, 1,403 blobs, ~4.5 s, next page via `Link: rel="next"`. |
| `GET /repository/archive.tar.gz?sha=<sha>&path=pcbnew/router` | 200, 246,605 B, ~0.5 s. Tar pax global header `comment` = the full commit SHA. **All 85 member git blob ids (`sha1("blob <len>\0" + bytes)`) equal the tree listing ids; zero missing, zero extra.** |
| archive `path=pcbnew` / `eeschema` / `api` | 4,766,431 / 3,209,983 / 24,027 B, each ~1 s. |
| Rate limit headers | `ratelimit-limit: 500` per window; each call above cost one. |

Conclusion: search is implemented **client-side over a commit-bound, path-scoped
archive whose completeness is proven against the recursive tree listing**. The
evidence here is from one session's probes. It shows the endpoint works now; it
does not guarantee GitLab keeps it.

## 2. `search_source_code` contract

```
search_source_code(query, path, mode="literal", case_sensitive=False,
                   max_matches=100, cursor=0) -> dict
```

Inputs, all validated before any network call:
- `query`: str, 1–200 chars, no NUL/CR/LF. Searched per line.
- `path`: repo-relative scope through the existing `_normalize_repo_path`.
  `""` means the whole repository, which is allowed but subject to the size caps
  below. Large scopes fail explicitly with `scope_too_large` and say to narrow
  the path. They are never truncated.
- `mode`: `literal` (substring) or `identifier` (literal bounded by
  `[A-Za-z0-9_]` non-word edges). **Regex is deliberately not offered** in this
  slice. Python `re` cannot be interrupted, so caller-supplied patterns are a
  ReDoS hazard. Any other mode is rejected as an error.
- `max_matches`: 1–500. `cursor`: int >= 0 offset into the deterministic match list.

Algorithm:
1. `resolve_pinned_commit()`. Every later request uses the resolved 40-char SHA, never the tag.
2. **Inventory:** walk the recursive tree for `path` with keyset pagination.
   Follow `Link rel="next"` only after validating it (§3.2). Cap at
   `MAX_TREE_PAGES = 300` (30,000 entries), or fail with `scope_too_large`.
   Record blobs `{path: (id, mode)}`. Mode `160000` (submodule/commit) and type
   `commit` entries go into `coverage.submodules_not_covered`. They are out-of-tree
   dependencies, not searched, and are listed by path.
3. **Archive:** `GET archive.tar.gz?sha=<sha>&path=<path>`, which must return 200.
   Read the body bounded at `MAX_ARCHIVE_BYTES = 64 MiB` (read max+1; over the
   cap fails `scope_too_large`). Open it with `tarfile` **in memory only. Never
   extract and never write to disk.** Enforce `MAX_DECOMPRESSED_BYTES = 192 MiB`
   total, `MAX_MEMBER_BYTES = 16 MiB` per member and `MAX_MEMBERS = 40,000`.
   Exceeding any cap is `scope_too_large`, not a partial result.
4. **Identity:** the pax global header `comment` must equal the resolved SHA, or
   the call fails as an integrity error. Every member name has exactly one
   leading archive-root segment, stripped once. The remainder must pass
   `_normalize_repo_path` and lie inside `path`. Otherwise it is an integrity
   error and the whole call fails.
5. **Member types:** a regular file is hashed (git blob sha1) and compared to the
   tree id. A symlink is never followed; hash the link-target text as its blob and
   compare, then classify it as `symlink` and do not search it. A hardlink,
   device, FIFO or other type is an integrity error.
6. **Coverage:** produce `tree_blobs`, `archive_files`, `missing_in_archive`,
   `unexpected_in_archive` and `blob_mismatch` (paths). `complete` is true only if
   all three discrepancy lists are empty. The lists are reported in full up to
   200 paths each, plus exact counts.
7. **Classification:** NUL in the first 8,000 bytes makes a file `binary`.
   Strict UTF-8 decode failure is `non_utf8`. Both are skipped and counted, never
   silently dropped. Text uses `splitlines()` with 1-based line numbers.
8. **Matches:** sorted by (path, line, column). Each match carries
   `{path, line, column, text, text_truncated, blob_id}`. `text` is capped at 400
   chars, centred on the hit.
9. **Status:** `matches` when complete and total > 0; `no_match` when complete and
   total == 0; `incomplete` otherwise. `incomplete` still returns any matches
   found, with the coverage lists. **`no_match` is reported only when coverage is
   complete.** HTTP 401/403/404/429/5xx raise `SourceCatalogError` with the status
   (and `Retry-After` for 429), never a result.
10. **Pagination:** `total_matches` is exact because the scan is full. `next_cursor`
    is `cursor + returned` while more remain, else `None`. A bounded in-process LRU
    (8 entries) caches **match lists and coverage only, never file contents**,
    keyed by `(sha, path, query, mode, case_sensitive)`. A later cursor page is
    then consistent and costs no download.
11. **Data, not instructions:** the response states
    `"source_text_is_data": true`. Nothing in source is executed, imported or
    interpreted.

Capability report: `kicad_source_search_code` went from `unsupported` to
`callable_unverified` when implemented with unit tests, then to `supported` after the
recorded live exercise (development receipt, not published). That exercise
covered five scoped searches with complete coverage at the pinned SHA, and a
whole-repo scope that failed with `scope_too_large`.

## 3. F04 trust boundary

### 3.1 Permission tiers (each is a separate authorization; none implies the next)

| Tier | Operation | State |
|---|---|---|
| P0 read | tag resolution, tree, ranged file read, archive-scoped search | read-only, fixed provider; all four implemented. Transport bounds in §3.2a; documented residual in §3.2b. |
| P1 patch proposal | produce a unified diff against named blob ids; never applied | design only |
| P2 dependency retrieval | fetch pinned third-party sources by manifest + hash | **blocked**: no pinned 10.0.5 dependency manifest staged; needs owner-approved storage |
| P3 configure/build/test | disposable least-privilege environment, approved FS/network, no owner files | **blocked**: owner-approved build storage; no heavy C: build |
| P4 install/deploy | replace any installed KiCad binary or profile | **blocked**: owner authorization per deployment |

Source metadata (CMake files, scripts, `.gitlab-ci.yml`, plugin manifests) can
never choose a command, grant a tier or widen a path. Commands for P2–P4 come
only from a reviewed, hashed runner manifest, which does not exist yet.

### 3.2a P0 network rules: enforced in code today (verified against `source_catalog.py`)
- Scheme `https`, host exactly `gitlab.com`, fixed project path. The caller
  supplies no URL, host, project, ref, token or header.
- Redirects are refused (`_NoRedirect`).
- Pagination `Link` next URLs are accepted only when all of these hold: scheme
  https, host gitlab.com, no port, no userinfo, no fragment, the path is the
  project tree endpoint, `ref` equals the resolved SHA, and `path` and
  `recursive` are unchanged. Anything else is an integrity error and is never
  followed.
- The package reads no token env var and sets no `Authorization` header.
- The archive read is bounded: `_default_archive_transport` reads the body in
  `READ_CHUNK_BYTES` (64 KiB) chunks through `_read_bounded`, at most
  `max_bytes + 1` bytes (default `MAX_ARCHIVE_BYTES`), and must finish within
  `ARCHIVE_DEADLINE_SECONDS = 180`. Over the deadline raises `deadline_exceeded`.
  The caller (`search_source_code`) rejects a body over the cap as
  `scope_too_large`. A non-200 archive response body is read with a single
  `exc.read(65536)`, so it is size-bounded but not covered by the deadline.
  Decompressed, per-member and member-count caps apply. Tree page *count* is
  capped by `MAX_TREE_PAGES`.
- Non-archive responses (tag resolution, `list_source_tree`, every
  `_inventory_tree` page, `read_source_file` metadata and raw) go through
  `_default_transport`, which reads through `_read_bounded` in `READ_CHUNK_BYTES`
  chunks:
  - JSON and tree pages: `MAX_JSON_RESPONSE_BYTES = 4 MiB` (4,194,304 bytes).
  - Error bodies (HTTPError): `MAX_ERROR_BODY_BYTES = 64 KiB` (65,536 bytes).
  - Raw file responses in `read_source_file`: the cap is
    `min(max(length, total_size), MAX_RAW_FILE_HARD_CEILING_BYTES)`, with
    `MAX_RAW_FILE_HARD_CEILING_BYTES = 64 MiB` (67,108,864 bytes). It is applied
    only when the default transport is in use. A Range-ignoring 200 for a file
    larger than that cap therefore fails rather than loading into memory.
  - `read_source_file` also rejects `length` above
    `MAX_RAW_FILE_HARD_CEILING_BYTES` before any network call. Any `length`
    from 1 to 64 MiB is otherwise accepted; `DEFAULT_CHUNK_BYTES` (1 MiB) is only
    the default.
  - A body over its cap raises `response_too_large: <what> exceeds <n> bytes`.
    This includes an error body over 64 KiB, which is not truncated.
- Wall-clock deadlines: `_default_transport` sets a deadline of
  `REQUEST_DEADLINE_SECONDS = 60` before opening the request, and
  `_default_archive_transport` uses `ARCHIVE_DEADLINE_SECONDS = 180`. The
  deadline is checked (monotonic clock) before each chunk read and once after the
  read loop, and raises `deadline_exceeded`. A single blocking socket read is
  still bounded only by the per-operation timeouts `REQUEST_TIMEOUT_SECONDS = 20`
  and `ARCHIVE_TIMEOUT_SECONDS = 60`, so the deadline is enforced between chunks,
  not by interrupting a read.
- `list_source_tree` validates every server-returned entry. A non-object item is
  skipped. For an object, `path` must pass `_normalize_repo_path` and be
  non-empty, otherwise `integrity_error`. It must be a **direct child** of the
  requested (normalized) path: the part before the last `/` must equal that path.
  Otherwise `integrity_error`. `name` must equal the last path segment, otherwise
  `integrity_error`. A recursive-style or out-of-scope entry fails the whole
  call.
- The archive is opened as an in-memory stream (`tarfile` `r|`) and never
  extracted. Symlinks are not followed. Hardlinks and other non-regular types
  are rejected. The pax SHA is checked.

### 3.2b P0 documented residual
1. **Proxy environment.** `build_opener` keeps urllib's default `ProxyHandler`.
   `HTTPS_PROXY`/`HTTP_PROXY` (including userinfo) are honoured, so a host proxy
   env can route the traffic and inject `Proxy-Authorization`. TLS to
   gitlab.com is still verified through CONNECT. "Sends no credentials" holds
   only when no proxy env is set. *Decision:* this stays a documented host-level
   residual. Disabling proxies would break hosts that legitimately need one.

### 3.3 Paths
- `_normalize_repo_path` covers caller paths, `_inventory_tree` entries and
  archive members: no `..`, no absolute paths, no backslash, no NUL, no empty
  segments. `list_source_tree` entries are covered too: each must be a direct
  child of the requested path with a matching `name` (§3.2a).
- Archive members are never extracted. Symlinks and reparse-style entries are
  data, never followed, so P0 search has no filesystem path-escape surface.

### 3.4 Acceptance tests now (P0, synthetic transport, no network)
These exist in `tests/test_source_search.py` (accepted job 5e9e7522):
traversal and absolute member names, an archive-root mismatch, a pax SHA
mismatch, missing and extra members, a blob mismatch, symlink not followed,
hardlink rejected, a decompression bomb, compressed/member/count caps, binary
and non-UTF-8 counts, a submodule listed as not covered, a foreign-host or
changed-ref `Link` rejected, 429 raising with Retry-After (never `no_match`),
`no_match` only when complete, deterministic cursor pages, identifier versus
literal, case folding, and query validation.

The §3.2a transport bounds (response byte caps, request deadlines,
`list_source_tree` entry validation) are tested in
`tests/test_transport_bounds.py`.

### 3.5 P1 patch and rollback design (answers F05; design only, nothing exists)
- A patch is only a *proposal*: a unified diff whose every hunk names the base
  blob id at the pinned SHA. P1 never applies anything.
- Any later application (an authorized P3 step) happens only in a disposable,
  task-owned copy or worktree on owner-approved storage, never in an owner tree.
- Before applying, record a manifest of path → sha256 for every tracked and
  untracked file in that copy, plus the exact patch bytes and base SHA.
- To roll back, reverse-apply the same patch, then verify the copy against the
  manifest byte-for-byte. If verification fails, keep the copy as evidence, stop
  and report to a human; never overwrite it.
- These commands are forbidden in any tier: `git reset --hard`, `git clean`,
  `git checkout -- <path>`, `git stash drop`/`clear`, and any recursive delete
  outside the task-owned copy.
- Source rollback, build-artifact cleanup and installed-binary rollback are
  three separate operations with separate records (P3/P4).

### 3.6 Acceptance tests later (P1–P4, need authorization)
These come from the review (F04/F05):
- reject malicious redirects in dependency retrieval and a changed dependency
  identity;
- reject a build script reading owner files;
- prove no credential or log leakage;
- seed unrelated edited and untracked files, introduce a concurrent change, and
  cause a patch failure. Recovery must preserve them byte-for-byte, retain the
  unapplied patch and refuse an ambiguous reversal.

None of these can run under the current constraints.

## 4. What stays open
- The §3.2b proxy environment stays a documented host-level residual.
- Symbol-aware search (definitions/references) beyond identifier matching.
- Regex, pending an interruptible matcher (e.g. RE2 or a subprocess with a deadline).
- Whole-repo search in one call (today it must be split by top-level directory
  because of the 64 MiB archive cap).
- Dependency source identity (submodules and third-party trees) for full build provenance.
- Everything in P1–P4.
