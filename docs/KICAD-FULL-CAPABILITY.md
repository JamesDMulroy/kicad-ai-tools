# KiCad 10.0.5 capability coverage (Phase A)

Status: **partial**. Phase A (GitLab source access and plugin inventory) is described in the first
sections; the D1 offline plugin inspection layer (two tools) is described in
[D1 offline plugin inspection](#d1-offline-plugin-inspection-and-closure-check); the kicad-cli layer
(three tools, 43 registered commands) is described in
[kicad-cli coverage](#kicad-cli-coverage-43-commands-validated-generic-invocation); the L4 UI broker
(three tools, live-verified on a private Win32 desktop) is described in
[L4 UI broker](#l4-ui-broker-live-verified); the adaptive-edit tools (five tools,
live-verified) are described in [Adaptive edits](#adaptive-edits-plan--preview--apply--undo). The files involved are
`workflow_server.py`, `kicad_mcp/tools/full_capability_tools.py`, `kicad_mcp/tools/cli_tools.py`,
`kicad_mcp/utils/cli_registry.py` (with its captured data `kicad_mcp/data/kicad-cli-10.0.5-tree.json`),
`kicad_mcp/utils/source_catalog.py`, `kicad_mcp/utils/plugin_catalog.py`,
`kicad_mcp/utils/plugin_inspect.py`, `tests/test_full_capability_tools.py`, `tests/test_plugin_roots.py`,
`tests/test_plugin_inspect.py`, `tests/test_cli_tools.py`, `tests/test_source_search.py`, the L4 package
`kicad_mcp/l4/` with `kicad_mcp/tools/l4_tools.py` and `tests/test_l4.py`, the adaptive tools
`kicad_mcp/tools/adaptive_tools.py` with `tests/test_adaptive_tools.py`, `native_board_interaction_capability`
in `kicad_mcp/tools/workflow_tools.py`, and this document. `kicad_mcp/utils/boardtooling.py` and
`kicad_mcp/utils/workflow_jobs.py` are not described here.

## What is implemented (20 tools, registered on the existing `FastMCP("KiCad BoardTooling")` instance)

| Tool | Status | Notes |
|---|---|---|
| `kicad_capability_report` | supported | Returns the typed `supported` / `callable_unverified` / `blocked` / `unsupported` matrix below. |
| `kicad_source_resolve_pin` | supported, **callable_unverified live** | Resolves GitLab tag `10.0.5` → full commit SHA, verifies it starts with the known short prefix `18fb9289`. Logic is covered by mocked-transport tests; its own `repository/tags/10.0.5` endpoint has not yet been independently exercised live by this code, only cross-checked against the GitLab tags web page's displayed short commit. |
| `kicad_source_list_tree` | **supported, live-verified** | Paginated `repository/tree` listing bound to the resolved pinned SHA. No caller-supplied `ref`. The operation lists the requested `pcbnew` tree. |
| `kicad_source_read_file` | **supported, live-verified** | Reads a pinned file by byte range (`Range` header), returns `next_offset`/`done` for continuation, plus `blob_id`, GitLab's `content_sha256`, and a locally computed `chunk_sha256` for provenance. The default requested length is 1 MiB; callers may request any positive length. The GitLab raw endpoint can return HTTP 200 with the complete file body — the raw endpoint can return HTTP 200 with the complete file body even when a `Range` header was sent — which this module detects (full-body length must equal the reported size) and slices locally; genuine `206` responses are cross-checked against a parsed `Content-Range` header and rejected if it disagrees with the requested offset/length or the file's reported size. |
| `kicad_source_search_code` | **supported (live-exercised)** | Client-side `literal` or `identifier` search (no regex) over a commit-bound, path-scoped archive. Completeness is proven by git blob id equality against the recursive tree listing. Scope caps fail as `scope_too_large` and are never truncated. Submodules are listed as not covered. `no_match` is reported only when coverage is complete. The archive is read in memory and never extracted to disk. No credentials. GitLab's server-side blob search returns HTTP 401 without a credential, which this package does not accept, so search is not provider-side. |
| `kicad_plugin_inventory` | supported | Read-only D0 inventory of the host-configured roots listed in the plugin inventory section below, plus a reported `settings_root`. Never accepts a caller-supplied root. |
| `kicad_plugin_inspect(package)` | supported (offline, executes nothing) | D1 static inspection of one root-relative package inside the host-configured quarantine root (not a tool parameter), through `kicad_mcp/utils/plugin_inspect.py`. Findings are static hints, not a safety verdict. A `PluginInspectError` becomes `{"status": "rejected", "error": {"code", "message"}, "quarantine_root": {"path", "source"}}`; the tool does not raise. See the D1 section. |
| `kicad_plugin_closure_check(wheels)` | supported (offline, executes nothing) | D1 offline dependency closure check over root-relative wheels already inside the host-configured quarantine root; reads wheel `METADATA` as data, installs nothing, fetches nothing. Same root, inputs and structured rejection. See the D1 section. |
| `kicad_plugin_activate` | **blocked (explicit)** | Always returns `{"status": "unavailable", ...}`. No install, no execution, no GUI launch. |
| `kicad_cli_commands` | supported | The full typed kicad-cli 10.0.5 registry (43 leaf commands), each with a run-level status and a path role per option. Local-only, no kicad-cli process. |
| `kicad_cli_describe` | supported | One command with its options, choices, defaults, classification and path roles. Unknown commands return a structured refusal. |
| `kicad_cli_run` | supported (integration-tested subset) | Registry-validated generic invocation of one leaf command through the contained hidden runner. See the kicad-cli section for the exact refusals, backup behaviour and limits. |
| `l4_operations` | **supported (live-verified)** | Lists the L4 registry's operations, or refuses (unapproved / invalid / unreadable registry). Launches nothing. See the L4 section. |
| `l4_run(operation, project_dir, params)` | **supported (live-verified)** | Runs one registered UI-fallback operation on a clone, on a private desktop. Operations stay refused until the owner approves the registry sha256. |
| `l4_record_identifiers(project_dir)` | **supported (live-verified)** | Records UI identifiers over three launches into the evidence directory only. |
| `adaptive_capability` | **supported (live-verified)** | Engine/KiCad discovery plus the engine's own capability report, and what is not supported (live canvas cursor tracking). |
| `adaptive_plan(board_path, request)` | **supported (live-verified)** | Solve an adaptive drag / edge move; writes nothing. |
| `adaptive_preview(board_path, request, render)` | **supported (live-verified)** | Candidate copy in `<board dir>/.adaptive/<session>/`, zones refilled, DRC compared, optional render; the board is not modified. |
| `adaptive_apply(session, expected_sha256, force, board_path)` | **supported (live-verified)** | sha256-guarded replace with byte-exact backup; DRC regressions need `force`. |
| `adaptive_undo(session, board_path)` | **supported (live-verified)** | Restores the backup; refused if the board changed since apply. |

"callable_unverified live" means: the request-construction, response-parsing,
and redirect/host-allowlist logic are implemented and covered by synthetic
mocked-transport tests, but this specific code's own live call to that
GitLab endpoint has not been made or recorded. It is distinct from
"live-verified", which means this exact code's request was made against
the real `gitlab.com` API and confirmed the response.

## Source access design

- Fixed to `gitlab.com` / project `kicad/code/kicad` only. No caller-supplied
 host, token, repository, or git ref is ever accepted; `ref` is always the
 internally resolved pinned commit SHA.
- HTTP redirects are refused outright (`_NoRedirect` handler raises on any
 3xx) rather than silently followed to an arbitrary host.
- Repo-relative `path` arguments are normalized and reject absolute paths,
 `..`/`.` segments, backslashes, and NUL bytes before any network call is
 made.
- Tree listing is paginated via GitLab's `page`/`per_page`/`X-Next-Page`
 convention; nothing is silently truncated — callers must follow
 `next_page` until it is `null`.
- File reads are chunked via HTTP `Range` requests with an `offset`/`length`
 continuation pair (`next_offset`, `done`); each response reports the
 file's total size, GitLab's blob ID and `content_sha256`, and this
 package's own `chunk_sha256` of the bytes actually received. The default
 `length` is 1 MiB; a caller can choose a larger positive length.
- GitLab's raw file endpoint does not always honor `Range`: it can return
 HTTP 200 with the entire file body instead of a 206 partial response. A
 200 is only accepted if its body length equals the file's reported total
 size (otherwise rejected as an incomplete response), and the requested
 `offset`/`length` window is then sliced out of it locally. A genuine 206
 response's `Content-Range` header is parsed and must agree with the
 requested offset, the file's reported total size, and the actual body
 length, or the response is rejected rather than trusted blindly.
- Code search (`kicad_source_search_code`) is **supported (live-exercised)**: client-side
 search, not provider-side. GitLab's unauthenticated blob-search endpoint
 returns HTTP 401, so the package does not use it. Instead it searches a
 commit-bound, path-scoped repository archive, per
 [SOURCE-SEARCH-AND-TRUST-BOUNDARY.md](SOURCE-SEARCH-AND-TRUST-BOUNDARY.md) §2
 and §3.2/§3.3:
 - Modes are `literal` (substring) and `identifier` (literal bounded by
 `[A-Za-z0-9_]` edges), with optional case folding. Regex is deliberately
 not offered, because Python `re` cannot be interrupted.
 - Completeness is proven before any match is reported. The archive's member
 git blob ids must equal the recursive tree listing's ids, with no missing,
 extra, or mismatched blobs. Coverage is `complete` only when all three
 discrepancy lists are empty.
 - `no_match` is reported only when coverage is complete. Otherwise the
 status is `incomplete`, and any matches found are returned with the
 coverage lists.
 - Scope caps (tree pages, archive bytes, decompressed bytes, member bytes,
 member count) fail as `scope_too_large`, which asks the caller to narrow
 the path. Results are never truncated.
 - Submodules (mode `160000`) and commit entries are not searched. They are
 listed by path under `submodules_not_covered`.
 - The archive is read in memory only and is never extracted to disk.
 Symlinks are never followed. Hardlinks and other special entries are
 integrity errors.
 - Requests use only the fixed `gitlab.com` endpoint and send no credentials.
 No token env var is read and no `Authorization` header is sent.
 - Results state `source_text_is_data: true`. Source text is never executed,
 imported, or interpreted.
 - Limits: literal and identifier modes only (no regex). The scope must fit
 the caps, so whole-repo search fails with `scope_too_large` and must be
 split by top-level directory. Submodules are not covered.

References: [GitLab REST API — Repositories](https://docs.gitlab.com/ee/api/repositories.html),
[GitLab REST API — Repository files](https://docs.gitlab.com/ee/api/repository_files.html),
[KiCad 10.0.5 tag](https://gitlab.com/kicad/code/kicad/-/tags) (short commit `18fb9289`).

## Plugin/addon metadata inventory

Host-configured roots (listed in the table below) are scanned read-only, plus a
reported `settings_root`. None are caller-supplied. Every scanned root in the result
carries `path`, `status` (`present`/`absent`, or `link_entry_not_followed` /
`kicad_documents_home_empty` with a `reason`; `path` is null only for the latter) and
`resolved_from`, so an absent root is never ambiguous about where it looked or why. Nothing is imported, compiled,
parsed as code or executed.

Root resolution mirrors KiCad 10.0.5 (`common/paths.cpp:35-134`,
`scripting/kicadplugins.i:259-274`, `common/api/api_plugin_manager.cpp:226-260`)
as implemented. The
documents base `<docs>` is, in order: `KICAD_DOCUMENTS_HOME` if set and
non-empty (`resolved_from: KICAD_DOCUMENTS_HOME`); else the OS Documents known
folder (Windows: `SHGetKnownFolderPath(FOLDERID_Documents)` via ctypes, buffer
freed with `CoTaskMemFree`; `resolved_from: known_folder`); else
`<home>/Documents` (`resolved_from: home_fallback`, used only if the lookup fails or
no known folder exists). `KiCad/10.0` is then appended.

**Set-but-empty `KICAD_DOCUMENTS_HOME`.** KiCad tests only `wxGetEnv` (`paths.cpp:39`), so an
empty value counts as set. The inventory matches that: every documents-derived root
(`user_scripting_plugins`, `user_scripting_root_modules`, `ipc_addon_plugins`, the `user_plugins`
discovery tree, and `pcm_third_party_packages`, `pcm_installed_package_code` and the `third_party`
discovery tree when only the `<docs>/3rdparty` default applies) is reported as
`{"status": "kicad_documents_home_empty", "path": null, "resolved_from": "KICAD_DOCUMENTS_HOME",
"reason": ...}` (plus an empty `entries` or `categories` list), is not scanned and has no fallback.
Stock/app roots, the settings roots, `installed_packages_index`, and a 3rd-party root supplied by
the process environment or a saved value are unaffected.

**3rd-party root precedence.** A non-empty process `KICAD10_3RD_PARTY` wins
(`resolved_from: env`); else the value saved at `environment.vars.KICAD10_3RD_PARTY` in
`<settings root>/kicad_common.json` (the settings root honours `KICAD_CONFIG_HOME`; the file is read
as size-capped JSON data only, and an absent, linked-outside, oversize, invalid, non-object,
non-string or empty value is ignored; `resolved_from: kicad_common_saved`); else `<docs>/3rdparty`
(`resolved_from: default`). **That a saved value ranks below the process environment is inferred
from `common_settings.cpp` `addVar` (the environment value overrides the seeded default); it was not
verified live against a running KiCad.**

| Category | Root (in priority order) | Manifest file | Distinguishes |
|---|---|---|---|
| Legacy scripting plugins (`legacy_scripting_plugins`) | `$KICAD_APP_PATH/share/kicad/scripting/plugins` (else kicad_env discovery; `resolved_from` `KICAD_APP_PATH`, `kicad_env_discovery` or `not_found`) | none — names and hashes only | Top-level `.py` files (except `__init__.py`) with SHA-256; package subdirectories containing `__init__.py`, with every file under them hashed recursively (relative POSIX names, bounded); non-`.py` importables and `__pycache__` reported (see below). Subdirectories without `__init__.py` keep status `not_loaded_by_kicad` and carry `importable_namespace_dir: true` with their contents listed and hashed recursively (same caps and link rules as packages). |
| Legacy scripting root modules (`legacy_scripting_root_modules`, new) | `$KICAD_APP_PATH/share/kicad/scripting` | none | The stock scripting root itself, same scan; its `plugins` subdirectory is not listed as a package and is noted as `scanned_separately` (`plugins_subdirectory`), unless `plugins` is a symlink/junction that does not resolve to an immediate child of the root: then it is reported as `link_entry_not_followed`, there is no `plugins_subdirectory` note, and the separate `*_scripting_plugins` scan reports `status: link_entry_not_followed` with an empty `entries` list instead of scanning through the link. |
| User scripting plugins (`user_scripting_plugins`) | `<docs>/scripting/plugins` | none — names and hashes only | Same scan and entry shapes as the bundled scan (shared helper), mirroring KiCad's `LoadPlugins` rules. |
| User scripting root modules (`user_scripting_root_modules`, new) | `<docs>/scripting` | none | Top-level modules in the user scripting path itself; `plugins` noted as scanned separately. |
| Settings scripting (`settings_scripting_root_modules`, `settings_scripting_plugins`, new) | `<user settings>/scripting` and `<user settings>/scripting/plugins` | none | Same scan; the root scan notes `plugins` as scanned separately. |
| PCM third-party packages | process `$KICAD10_3RD_PARTY` if non-empty (`resolved_from: env`), else the value saved in `<settings root>/kicad_common.json` (`kicad_common_saved`), else `<docs>/3rdparty` (`default`; not `%APPDATA%`) | `metadata.json` | Package Manager (PCM) metadata, per [KiCad addon docs](https://dev-docs.kicad.org/en/addons/index.html). Immediate children only, unchanged: category directories (`plugins`, `footprints`, …) remain `no_manifest_found`. |
| Installed PCM package code (`pcm_installed_package_code`, new) | `<3rdparty>/<category>/<pkgid>/…` (same root as above) | none | Per category: each package directory with every file under it hashed recursively (subpackages included, non-importables and `__pycache__` reported, bounded), plus importable files directly inside a category (loose modules, e.g. in `<3rdparty>/plugins`; whether that path equals the loader's `PyPluginsPath(THIRDPARTY)` is unverified). |
| Installed packages index (`installed_packages_index`, new) | `<user settings>/installed_packages.json` | — | Read as data only: size capped, parsed with `json`; status `ok` (sha256 + parsed data), `absent`, `invalid` (sha256 when readable, with the error) or `link_entry_not_followed`. |
| IPC addon plugins | `<docs>/plugins` | `plugin.json` | IPC API addon manifests, per [IPC API for addon developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/). Immediate children only, unchanged. |
| IPC `plugin.json` discovery (`ipc_plugin_json_discovery`, new) | the stock plugins path `$KICAD_APP_PATH/share/kicad/plugins`, the 3rd-party root and `<docs>/plugins` (keys `stock_plugins`, `third_party`, `user_plugins`) | `plugin.json` | Recursive search (depth and entry bounded) in all three trees, mirroring `API_PLUGIN_MANAGER`'s traversal (recursion itself is general wxDir behaviour, not receipted). Each found manifest: relative path, sha256, parsed data or `invalid_metadata`, and the same compatibility heuristic. |
| Settings root (`settings_root`) | `$KICAD_CONFIG_HOME/10.0` if `KICAD_CONFIG_HOME` is set and non-empty (`resolved_from: KICAD_CONFIG_HOME`), else `%APPDATA%/kicad/10.0` | none | Presence and `resolved_from`; its `scripting` directories and `installed_packages.json` are scanned as above. That KiCad appends the `10.0` version directory under `KICAD_CONFIG_HOME` is assumed to mirror the APPDATA branch; the excerpt that would confirm it is not in this repository. |

For each manifest found, the inventory reports: parse validity (invalid JSON
or non-object root is reported structurally, never raises), a SHA-256 of the
manifest file, and a `compatibility` block checking any declared
`versions[].kicad_version` entries against the pinned `10.0` release line —
`checked: false` when the manifest declares no version list at all, so an
absent compatibility signal is never conflated with a confirmed match.

**Link entries are reported, not dropped.** An entry that resolves outside its
scanned root, to a non-immediate child, or that is dangling or unresolvable is
never followed or hashed, and is reported as
`{type, filename | package_dir | relative_path, "status": "link_entry_not_followed", "reason"}`
in the scripting scans, the PCM/IPC scans, the installed-code scan, inside packages
(relative `filename`) and in `plugin.json` discovery (where a link resolving outside
its tree, or back to an already-visited directory, is reported the same way). A link
resolving to an immediate child of the same root is still followed and hashed through
its resolved path.

**Non-`.py` importables and caches are reported.** In a scripting directory scan,
top-level `.pyc`, `.pyd` and `.so` files appear with `"status": "non_py_importable"`
and a hash (not imported). A top-level `__pycache__` directory appears as
`"status": "pycache_present"` with its file names and hashes. Inside a package, the
recursive `files` list, `non_py_importables`, `subdirectories`,
`subdirectories_not_descended`, `pycache_dirs` and a `truncated` flag are returned.
The caps are `MAX_PACKAGE_FILES`, `MAX_PACKAGE_DEPTH`, `MAX_TREE_BYTES`,
`MAX_DISCOVERY_DEPTH` and `MAX_DISCOVERY_ENTRIES`.

**`.pyw` is Python source (D0 and D1).** CPython imports `x.pyw` as `x` on Windows, so D0 treats it
like `.py` (top-level hash entry without a `non_py_importable` status, package-internal hash,
`loose_files` entry in a PCM category without the `non_py_importable` flag, file-type
`link_entry_not_followed` record) and D1 puts it in `importables.python_sources`, `ast.parse`s it
(static hints, entry-point classes), and counts it for kind detection and bare-file input. Evidence:
`test_pyw_user_scripting_file_is_source`, `test_pyw_inside_package_is_hashed_without_non_py_flag`,
`test_pyw_pcm_loose_file_and_link_entry`, `test_pyw_sentinel_writes_no_marker`
(`tests/test_plugin_roots.py`); `test_pyw_is_python_source_in_packages`,
`test_lone_pyw_module_kind_and_bare_file_input`, `test_bare_pyw_file_is_a_legacy_module`,
`test_pyw_sentinel_is_never_executed` (`tests/test_plugin_inspect.py`).

**Unreadable roots.** `_scan_addon_dir` (PCM and IPC roots) now returns
`{"status": "present", "path", "resolved_from", "entries": [], "error": "<ExcType>: <msg>"}` when the
root exists but cannot be listed, like the other scanners, so `inventory_plugins()` does not raise
(`test_unreadable_addon_root_returns_structured_error`).

**Hashing is streamed and capped.** Files are hashed in 1 MiB blocks with a
module-level cap `MAX_HASH_BYTES` (64 MiB). A file over the cap is not opened; its
entry carries `"sha256": null`, its `size` and `"status": "oversize"` (recorded as
`hash_status` where the entry already has a status). The `sha256` value is unchanged
for normal files.

**Evidence (offline, `tmp_path` fixtures only, nothing run in a live KiCad):**
`tests/test_plugin_roots.py` covers each closure above, including
`test_user_scripting_symlink_escape_reported_as_link_entry`,
`test_pcm_ipc_and_discovery_report_link_entries`,
`test_package_files_are_recursive_with_relative_names`,
`test_top_level_non_py_importables_and_pycache_are_reported`,
`test_oversize_file_is_marked_and_not_opened`,
`test_settings_scripting_roots_scanned`, `test_user_scripting_root_modules_scanned_and_plugins_noted`,
`test_legacy_scripting_root_modules_scanned`, `test_settings_root_prefers_kicad_config_home`,
`test_installed_packages_index_*`, `test_pcm_installed_package_code_enumerated_per_category`,
`test_ipc_plugin_json_discovered_recursively_in_all_three_trees`, and the sentinel test
`test_inventory_never_imports_sentinels_in_any_scanned_location`, whose sentinel modules
(including a `.pth` hook and `setup.py`) write a marker if imported or run and whose markers
must stay absent. Symlink tests skip where symlinks cannot be created; stand-in-classifier
tests cover the same contract there.

**Previous defect (fixed, D0).** Earlier versions looked for 3rd-party
packages under `%APPDATA%/kicad/10.0/3rdparty` and IPC plugins under
`<Documents>/KiCad/10.0/plugins`, and did not scan user scripting plugins. On
the Documents known folder may be redirected, so those
roots were wrong and the `edge_plating` scripting package was missed. Per the
lead's observation (not re-verified by this code), the real directories exist
under that Documents folder; `inventory_plugins()` now resolves them through
`<docs>`. This fix is covered by offline tests with a faked resolver and
`tmp_path` only (`tests/test_plugin_roots.py`); it has not been checked
against a running KiCad. Nothing is imported, compiled or executed.

**D0 gaps closed since.** Of the design's "What D0 still does not do" list
(the isolation design), the following are closed in code and covered by
`tests/test_plugin_roots.py` (offline `tmp_path` fixtures only; nothing was run against a live KiCad):
- link entries reported as `link_entry_not_followed` rather than dropped, including a symlinked or
 junctioned `plugins` subdirectory in a scripting root (reported in the root scan, and its separate
 `*_scripting_plugins` scan refuses to follow it);
- top-level modules in `<docs>/scripting`, `<user settings>/scripting`,
 `<user settings>/scripting/plugins` and the stock scripting root;
- `KICAD_CONFIG_HOME` for the settings root;
- `installed_packages.json` (read as data);
- installed PCM package code under `<category>/<pkgid>`;
- recursive `plugin.json` discovery in the stock, 3rd-party and user trees;
- `.pyw` source files (treated as Python source like `.py`: hashed, no `non_py_importable` flag, in
 scripting scans, packages, PCM loose files and link entries), and the non-source importables
 (`.pyc`, `.pyd`, `.so`), `__pycache__`, subpackage contents, and the contents of namespace
 directories (subdirectories without `__init__.py`: listed and hashed, status still
 `not_loaded_by_kicad`, marked `importable_namespace_dir: true`);
- a streamed, capped hash;
- the saved 3rd-party value in `<settings root>/kicad_common.json` (Configure Paths) as a source below
 the process environment and above the `<docs>/3rdparty` default;
- a set-but-empty `KICAD_DOCUMENTS_HOME`: documents-derived roots are reported as
 `kicad_documents_home_empty` and not scanned, with no fallback.

D1 additionally reports file-dialog calls (`wx.FileDialog`/`wx.DirDialog`, `tkinter.filedialog`
functions, `QFileDialog.*`, pcbnew dialog helpers) as the `file_dialog` static hint (see the D1 section).

**Known divergences from KiCad (remaining).**
- **Saved-value precedence is inferred, not verified live.** That a Configure-Paths value saved in
 `kicad_common.json` (`environment.vars.KICAD10_3RD_PARTY`) ranks below a non-empty process
 environment value is inferred from the structure of `common_settings.cpp` `addVar`, not excerpted
 end-to-end and not observed in a running KiCad. That `Pgm().GetLocalEnvVariables()`
 (`api_plugin_manager.cpp:237`) reads that same saved map, and the `10.0` subdirectory appended under
 `KICAD_CONFIG_HOME`, are likewise unverified. Only `environment.vars.KICAD10_3RD_PARTY` is read from
 the file; other saved variables are not.
- **Known-folder equivalence is unverified.** That `SHGetKnownFolderPath(FOLDERID_Documents)`
 returns the same folder as KiCad's own Documents lookup is not established.
- `KICAD_APP_PATH` is a convention of this package, not a KiCad variable, and its stock-path
 equivalence to KiCad's scripting path is unverified.
- The legacy loader's `PyPluginsPath(THIRDPARTY)` path is not receipted; whether `<3rdparty>/plugins`
 equals it is unverified.
- D0 suffix matching: `.py` and `.pyw` (both Python source) are matched case-sensitively, like the
 loader's `.py` match; `.pyc`/`.pyd`/`.so` are matched case-insensitively. (D1 lower-cases names, so
 it recognises `.py`/`.pyw` case-insensitively.)

**Plugin activation, installation, and development builds are explicitly
out of scope and unimplemented.** KiCad 10's IPC API requires a running GUI
PCB Editor session and does not cover schematic editing or CLI
plotting/export; this package never launches a GUI, never installs a
package, and never imports or executes plugin code. `kicad_plugin_activate`
always returns a structured `unavailable` response.

## D1 offline plugin inspection and closure check

Two tools, `kicad_plugin_inspect(package)` and `kicad_plugin_closure_check(wheels)`, call
`kicad_mcp/utils/plugin_inspect.py` (`resolve_quarantine_root`, `inspect_package`, `closure_check`,
`PluginInspectError`), per sections A3 and A3c of
the project's internal isolation design.

- **Executes nothing.** No import, compile, `exec`, install, pip, network or GUI. Package files are
 read as data (archive members in memory, `ast.parse` on text only).
- **Host-configured, task-owned quarantine root (A3).** The root is not a tool parameter and the
 caller cannot choose it. It is `KICAD_MCP_QUARANTINE_ROOT` if set and non-empty (reported
 `source: KICAD_MCP_QUARANTINE_ROOT`), else `<LOCALAPPDATA>/kicad-mcp/quarantine` (`source:
 LOCALAPPDATA_default`; the OS LocalAppData known folder if `LOCALAPPDATA` is empty, else refused), created if absent. It is
 resolved once per tool call by `resolve_quarantine_root`; if it is unusable, not a directory or not
 absolute the call returns `quarantine_root_invalid` (with no root). Inputs (`package`, each entry of
 `wheels`) are names **relative to that root**: absolute, drive-letter, UNC, rooted and `..` paths,
 the root itself, and symlink, junction or reparse-point escapes are rejected, and every result
 (success or rejection, once the root resolved) reports `quarantine_root: {path, source}`. The
 library functions `inspect_package(path, quarantine_root)` and `closure_check(quarantine_root,
 wheels)` keep an explicit root argument for tests only; the wrappers' `root` keyword is likewise
 test-only and absent from both MCP schemas. The tools never place a package where KiCad looks.
- **Static hints, not a safety verdict.** Flagged findings (network imports, subprocess, `ctypes`,
 `os.system`/`popen`, `eval`/`exec`, writes outside the package, dynamic imports and file-dialog
 calls) are data, found with `ast.parse` of text only. The `file_dialog` hint matches call names:
 `wx.FileDialog`/`wx.DirDialog` (and the `*Selector` helpers), `tkinter.filedialog` functions such as
 `askopenfilename`, `asksaveasfilename` and `askdirectory`, `QFileDialog.*`, and pcbnew file-dialog
 helpers; aliases that hide a name are not resolved. No scan makes a package safe to execute.
- **Dependency blocking.** `declared_dependencies` keeps `has_unpinned` and `unpinned_count` and adds
 `blocks_execution` (bool) and `blocks_reasons`. It is true when any requirement is unpinned
 (`unpinned_requirement`) or when `invalid_lines` is non-empty (`unverifiable_requirement_lines`):
 option lines (`-r`, `-e`, `--index-url`, `--extra-index-url`), line continuations, unparseable
 lines, and non-UTF-8 or unread requirements files all block, because they cannot be checked as
 exact pins. A file of only such lines has `has_unpinned: false` but `blocks_execution: true`. A
 static signal only; nothing is executed either way. Evidence: `test_requirements_pinned_vs_unpinned`,
 `test_all_pinned_requirements_do_not_flag_unpinned`, `test_only_unpinned_requirements_block_with_that_reason`,
 `test_invalid_requirement_lines_alone_block`, `test_non_utf8_requirements_file_blocks`.
- **Offline closure check.** Resolves and checks wheels already present in the quarantine root from
 their `METADATA`; it does not fetch a missing wheel and does not install anything.
- **Structured rejection.** A `PluginInspectError` (which carries a `.code`) is returned as
 `{"status": "rejected", "error": {"code", "message"}, "quarantine_root": {"path", "source"}}`; the
 tools do not raise it. Codes: `quarantine_root_invalid`, `path_invalid` (empty, NUL, non-text),
 `path_not_relative` (absolute, drive, UNC, rooted), `path_traversal` (`..` segment),
 `path_outside_quarantine` (the root itself, or a symlink, junction or reparse escape),
 `path_not_found`, `path_wrong_kind`, `wheel_outside_quarantine` (a wheel entry that escapes the
 root), `wheel_invalid` (not a regular `.whl` file), `wheels_invalid` (not a list), `limit_invalid`
 (bad cap argument); default `plugin_inspect_error`. Member-level archive problems are not
 exceptions: `status: "rejected"` with `rejection: {member, reason, code, detail}`, where `code` is
 `cap_exceeded` (`over_*_cap`, `archive_too_large`, `central_directory_too_large`,
 `zip64_not_supported`) or `archive_member_rejected` (everything else); `reason` is unchanged.
- **Evidence.** `tests/test_plugin_inspect.py` (inspection and closure logic, `resolve_quarantine_root`
 for env, default, creation and invalid roots, relative-only/absolute/drive/UNC/traversal/symlink
 escape rejections asserted by real `.code`, cap and member `code` values, and sentinel fixtures
 whose markers must stay absent) and, for the tool wrappers, `tests/test_full_capability_tools.py`
 (`test_plugin_inspect_tools_registered_and_listed_as_supported`,
 `test_plugin_tool_schemas_have_no_quarantine_root`,
 `test_plugin_inspect_wrapper_rejections_carry_real_codes`,
 `test_plugin_closure_check_wrapper_rejections_carry_real_codes`,
 `test_plugin_wrappers_report_invalid_host_root_without_a_root`,
 `test_plugin_inspect_wrappers_pass_through_success`,
 `test_plugin_inspect_default_root_is_created_under_localappdata`,
 `test_plugin_inspect_tools_callable_through_the_server`). These use the real module with
 `KICAD_MCP_QUARANTINE_ROOT` set to a `tmp_path` directory and no monkeypatched exceptions.

**Not implemented.** D2 retrieval (fetching a package archive or dependency wheel), D3 development and
X1–X4 (any plugin execution, install, update or removal, including in an isolated profile) are **not
implemented**. `kicad_plugin_activate` stays blocked. The design document describes them; none of
them exists as code or as a capability here. L4's exact status is in the next section.

## L4 UI broker (live-verified)

Files: `kicad_mcp/l4/` (`registry.py`, `monitor.py`, `hidden_desktop.py`, `uia_helper.py`, `broker.py`,
`registry/ui-registry-10.0.5.json`), `kicad_mcp/tools/l4_tools.py`, `tests/test_l4.py`, per
`docs/L4-UI-BROKER-SPEC.md`. Tools: `l4_operations`, `l4_run(operation, project_dir, params)`,
`l4_record_identifiers(project_dir)`; callers supply an operation name and typed values only.

**What the code does (design intent, implemented).**
- *Closed registry.* A versioned registry enumerates, per operation, the window/dialog/control
 identifiers, a typed parameter schema (enum, bounded number, clone-relative path), a postcondition on
 disk, a per-dialog `safe_to_cancel` flag and an `api_alternative`. Unknown keys, operations,
 parameters, parameter types and step actions are rejected before any input is sent. A registry whose
 sha256 is not approved, or whose `kicad_build` differs from `kicad-cli version`, refuses everything.
 Approval comes from the release file `kicad_mcp/l4/registry/APPROVED.sha256` (
 approval of the shipped registry) or from `KICAD_MCP_L4_APPROVED_REGISTRY_SHA256` (comma-separated,
 for a registry re-recorded locally); any byte change to a registry leaves it unapproved.
- *Launch.* The only process-creation path is `CreateProcessW` with `STARTUPINFOW.lpDesktop` on a private
 desktop (`hidden_desktop.create_process_on_desktop`), with a task-owned seeded profile
 (`KICAD_CONFIG_HOME`, `KICAD_DOCUMENTS_HOME`, `KICAD10_3RD_PARTY`, `KICAD_CACHE_HOME`, `TEMP`, `TMP`,
 `LOCALAPPDATA`, `APPDATA`), inherited `KICAD*_3RD_PARTY` stripped, and the exact environment block
 that is passed to `CreateProcessW` parsed back and verified first. That verification proves the values
 were *passed*; it does not read the child's own environment block back, and it does not prove KiCad
 *reads* them. A windowless `pythonw` placement self-check runs before every GUI launch and refuses it
 if the child does not report the private desktop. The UIA helper is itself spawned on that desktop
 with `pythonw.exe`, and talks over a file channel (no stdout).
- *Guard.* A watchdog polls the input desktop every 50 ms for any window of the task's process tree and
 for foreground changes attributable to it, and kills the instance on a hit; WTS console connect/lock
 state is snapshotted before and after and compared; zero input-desktop samples counts as a failure.
 This is polling, not a `SetWinEventHook` stream and not a WTS notification subscription, so a window
 that lives for less than one polling interval could be missed.
- *Menus.* wxMSW menus are native HMENUs; UIA ExpandCollapse on them fails on a non-input desktop
 (`COR_E_INVALIDOPERATION`). The helper walks `GetMenu`/`GetSubMenu` by name inside a synthetic menu loop
 (`WM_ENTERMENULOOP`, `WM_INITMENUPOPUP` so wx runs update-UI and reports the real enabled state,
 `WM_UNINITMENUPOPUP`, `WM_EXITMENULOOP`) and then posts `WM_COMMAND` with the leaf's id. Without the
 closing messages wx leaves the menu loop open and quasi-modal dialogs never appear. Owned dialogs
 are found as Window-type children of the task's top-level windows (UIA nests them under the owner).
- *Run.* A task-owned clone of a plain (no links) project directory; UIA patterns only (no SendInput);
 the helper only sees windows of the target PID; an unexpected dialog is captured and, unless the
 registry marks it `safe_to_cancel`, the instance is killed by exact process handle; postcondition
 checked on disk; cleanup by handle; evidence directory kept; original project hashed before/after.
- *Recording.* `record_identifiers` opens the candidate dialogs in three independent launches and
 admits an identifier only if it resolves to the same control in all three (name + control type +
 ancestry suffix; an AutomationId is accepted only if stable (non-numeric, or a wx stock id 5000-5999 such as wxID_CANCEL=5101) and unique); unstable ones are
 dropped and reported. The candidate UI names in `broker.CANDIDATES` are hypotheses about KiCad 10.0.5's
 strings, not facts; a wrong guess shows up as a dropped identifier, never as an admitted one.

**Live tests run KiCad on a private Win32 desktop.**
- `tests/test_l4.py`, all tiers: all pass (the earlier skip of `pcb_board_setup_read` is gone: it is
 recorded and live-verified, see below). The live tests cover:
 - placement check;
 - 3-launch recording;
 - open-and-save (original byte-identical, the clone is rewritten and loads with kicad-cli);
 - Gerber export through the Plot dialog, written into the clone only;
 - a decoy second instance (on its own private desktop) left untouched;
 - unapproved registry refused without launching.

 Every run's monitor verdict: 0 task windows on the input desktop, more than 0 input-desktop
 samples, 0 foreground samples attributed to the task, and no WTS difference.
- Shipped registry `kicad_mcp/l4/registry/ui-registry-10.0.5.json`:
 - written by `python -m kicad_mcp.l4.broker record <synthetic project> --out ...`, never edited by
 hand;
 - 6810 bytes, LF only (`.gitattributes` marks it `-text`);
 - **sha256 `9d37d8d8f90521f833cabaf8c79206ba714b3bf68e9b897a9063962cd1c5f87f`** (re-recorded
 ; the previous `a6944b95…` admitted only the first two operations);
 - admits `pcb_open_and_save`, `pcb_export_gerbers_dialog` and `pcb_board_setup_read`. The Plot dialog's
 Close button is addressed by wx stock id 5101, because two buttons are named "Close".

 With that sha supplied explicitly to the call, `l4_run pcb_export_gerbers_dialog` produced 9
 Gerbers in the clone, left the original unchanged, and recorded 520 monitor samples with 0 hits.
- **Approved by the release:** the owner directed approval on , so the hash ships in
 `kicad_mcp/l4/registry/APPROVED.sha256`. Any byte change to the registry no longer matches, and every
 operation is then refused. `KICAD_MCP_L4_APPROVED_REGISTRY_SHA256` can add approvals for a locally
 re-recorded registry.
- **Board Setup page navigation ():** Board Setup opens on its Board Editor Layers page, and
 the copper-layer control is on Board Stackup > Physical Stackup. `pcb_board_setup_read` now selects that
 page's tree item first (a recorded `stackup_page` control; the recorder resolves a control marked
 `on_page` only after selecting its page), then reads the value and cancels. The registry was
 re-recorded over three launches with all three operations admitted and nothing dropped, and its new
 hash ships in `APPROVED.sha256`. Live: the operation reads `2` on the 2-layer synthetic board and
 leaves the original unchanged.
- Residual risks, disclosed:
 - the watchdog polls every 50 ms (no `SetWinEventHook`), the same mechanism as the proven
 `hidden_kicad.py` tripwire; it is used for synthetic-project use;
 - B-4 is a same-user alternate desktop, so it is for synthetic projects only and never for plugin
 execution.

**Unit evidence (no GUI), `tests/test_l4.py`:**
- registry schema: closed keys, bad references, missing `safe_to_cancel` cancel control, and name-keyed
 maps;
- unknown operation / parameter / type, plus typed enum and number values;
- path-escape cases, including a symlink;
- unapproved, malformed and changed-bytes sha256 refusal, and version-mismatch refusal;
- refusals happen before any launch;
- monitor logic;
- 3-launch stability logic, including wx stock ids vs dynamic numeric ids;
- launch-precondition environment-block verification;
- helper-side selector checks;
- source scans.

## Adaptive edits (plan / preview / apply / undo)

Files: `kicad_mcp/tools/adaptive_tools.py`, `tests/test_adaptive_tools.py`. The engine ships in this package at
`kicad_mcp/adaptive_editor/` (override with `KICAD_MCP_ADAPTIVE_ENGINE`) and runs under
KiCad's own `python.exe` (pcbnew) through `secure_subprocess.run_contained`. The server never imports
pcbnew.
- Request (integer nm), per `adaptive-editor/ADAPTIVE-ENGINE-SPEC.md` §3:
 - a part driver (`uuids`) or an edge driver (`edge_uuid`, `side`);
 - `displacement_nm`;
 - `mode` cascade/explicit;
 - `movers`/`frozen`/`priority`;
 - `eligibility_nm`, `allow_partial`;
 - `copper` adapt/fixed.

 Unknown fields are refused before the engine runs.
- Live evidence: `tests/test_adaptive_tools.py`, 19 passed. On a real pcbnew board: plan writes
 nothing; preview leaves the board unchanged; apply changes it; **undo restores it byte-identically**.
 A stale sha256 is refused.
- In the PCB Editor itself, the same engine ships as the AdaptiveEditor IPC plugin (`Adaptive Drag`,
 `Adaptive Edge Move`). It previews in its panel and applies as one KiCad undo step. It is
 live-tested, including KiCad discovering the installed plugin and building its venv; see
 `kicad_mcp/adaptive_editor/` §5.
- `native_board_interaction_capability(operation)`: `footprint_drag` and `edge_move` (alias
 `edge_shove`) are supported via these routes. `canvas_mouse_tracking` is **not supported**: KiCad
 10.0.5's IPC API does not render uncommitted changes, so following the cursor on the KiCad canvas
 needs native pcbnew code.

## kicad-cli coverage (43 commands, validated generic invocation)

Source of truth: `kicad_mcp/data/kicad-cli-10.0.5-tree.json`, the captured `--help` text of every
kicad-cli 10.0.5 node (read-only data). `kicad_mcp/utils/cli_registry.py` parses its 43 leaf commands
(fp 2, jobset 1, pcb 27, sch 10, sym 2, version 1) into typed commands and options.

**Coverage statement.** CLI coverage is complete at the command level: 43 of 43 leaf commands are
registered, described and option-validated, and are reachable through one validated generic
invocation, `kicad_cli_run`. That is **not** 43 runnable commands, and it is not a claim about what each
option does beyond what kicad-cli's own `--help` states: per-option semantics are not claimed, and only
a subset of commands has been executed against the real kicad-cli (see Evidence). Run-level status of
the 43 commands (the counts reconcile: 40 + 1 + 1 + 1 = 43):

| Run-level status | Count | Commands |
|---|---|---|
| supported (validated, runnable) | 40 | every command not listed below |
| callable_unverified | 1 | `pcb render` — offscreen 3D context; no proof it never shows a window; not integration-tested |
| blocked | 1 | `jobset run` — jobsets can name arbitrary outputs and run commands or copy jobs; listed, refused by `kicad_cli_run` |
| deprecated | 1 | `pcb export hpgl` — kicad-cli says "No longer supported as of KiCad 10.0."; refused at run level. `sch export hpgl` is a different command and is supported |

So 41 of 43 are runnable at run level (40 supported + `pcb render` unverified), 1 is blocked and 1 is
deprecated. `kicad_capability_report` returns these counts as `kicad_cli_command_coverage`.

**What `kicad_cli_run` does.** Every refusal below is a structured `{"ok": false, "status": "refused",
"error": {"code": ...}}` result returned before anything is launched.

- *No free argv.* argv is `[kicad-cli path, *command words, *registry-validated option tokens,
 --output <output_path>, <input>]`. Unknown, duplicate, wrongly typed, out-of-choice, control-character
 and leading-dash option values are refused by the registry. `--output` is reserved: it is accepted
 only as `output_path`, never inside `options`. kicad-cli is located with `get_kicad_cli_path`.
- *Paths.* Input and output paths must be absolute, contain no `..`, and are resolved through
 symlinks/junctions. Every path-like value option has an explicit role (a test fails if one is
 unclassified; unclassified ones are refused): `--drawing-sheet` must be an existing file inside the
 input's project directory; `--report-path` and `--report-file` must be new files inside the output
 root; `--models-dir` must be one plain folder name; `--variant` names use only letters, digits,
 `.`, `_`, `-`. A `${` in a path is refused except a literal `${VARIANT}` in `output_path`.
- *Outputs.* Commands that write output require an absolute `output_path` that does not exist yet
 (a directory output may be an existing empty directory; a missing directory is created by the tool, its
 parent must exist). The output may not be, contain or sit inside the input. The process runs with the
 output root as its working directory. `--force` on a non-mutating command (3D exports overwrite with
 it) is refused. `fp upgrade` and `sym upgrade` may omit `output_path` (in-place upgrade).
- *Mutation.* `sch/pcb/fp/sym upgrade`, `pcb import`, and `pcb drc` with `--save-board` need
 `allow_mutation=True`; without it they are refused and nothing is touched. With it, before launch the
 tool makes a byte-exact backup next to the input, named `<name>.bak-<utc timestamp>`: `copy2` for a
 file, plus a sibling backup of every rewritable KiCad file in the project directory (`sch upgrade` saves
 every sheet; upgrades and `--save-board` can touch `.kicad_pro`/`.kicad_prl`); `copytree` of the whole
 directory for directory inputs. Each copy's sha256 is verified against the source, a manifest
 `<name>.bak-<timestamp>.manifest.json` is written next to it, and any backup failure aborts the run.
 The backup paths are returned in the result. (The backup lives next to the input, inside the project
 tree, it is not stored on another volume.)
- *Side effects on every run.* The project's KiCad files (`.kicad_*`, lib tables, lock files) are hashed
 before and after, for read-only runs too. A change during a run that was not authorised is returned
 as error `unexpected_project_change` with before/after hashes (no backup exists for such a run). One
 known KiCad 10.0.5 side effect is tolerated: on a non-mutating run with a file input, an *added*
 `<input stem>.kicad_prl` in the input's own directory (KiCad creates project-local settings on
 load) is the only tolerated addition. It is reported in `kicad_side_effects` (path, `sha256_after`,
 reason), is not counted in `changed_project_files`, and is never deleted. Stems of other
 `.kicad_pro` files are not accepted. A `.kicad_prl` with any other stem (even beside a matching
 `.kicad_pro`), any modification (including of an existing `.kicad_prl`), any deletion, any other
 added file and a `.kicad_prl` in a subdirectory still fail as `unexpected_project_change`. Output
 files that are new or changed in the output root are listed with size and sha256 (capped at 200,
 with the total and a truncated flag).
- *Runner.* `secure_subprocess.run_contained` only (hidden window, owned job object, exact-process
 cleanup), with an explicit timeout (default 180 s, at most 600 s) and an evidence directory. A timeout
 returns `timed_out: true` and error `timeout`. stdout and stderr are each returned bounded (64 KiB
 head+tail with a truncated flag) with the full byte count and sha256; the evidence directory is kept
 only when the run failed or an output was truncated.

**Known limits.** Option values that start with `-` (for example `pcb render --rotate -45,0,45`) are
refused by the registry (`leading_dash`): whether kicad-cli accepts the `--option=value` form was not
verified, so no such value reaches argv. `run_contained` still reads the evidence files fully into memory
once the process has ended, so the 64 KiB bound applies to what is returned, not to peak memory of a very
chatty child. Locks and caches that kicad-cli writes elsewhere (outside the project directory and the
output root) are not observed. Where `--models-dir` creates its folder is not verified. Backups are never
cleaned up by the tool.

**Additional implemented and tested behaviour.** File-input mutations back up every non-lock project file the
tool hashes (lib tables, `.kicad_dru`, `.kicad_wks` included), and an authorised mutation that modifies or
removes a file with no backup returns `changed_without_backup`. `--theme`, `--preset`, `--format-preset`
and `--default-font` must be plain names, and `--define-var` values containing a dollar-brace expansion
are refused. This behaviour is covered by unit tests with a stand-in runner, including the
`changed_without_backup` test, the deleted-project-file test and the other-stem `.kicad_prl` test in
`tests/test_full_capability_tools.py`.

**Evidence.** Unit tests (`tests/test_cli_tools.py` for the registry; refusal, backup, side-effect,
output-bounding and timeout tests in `tests/test_full_capability_tools.py` using a stand-in runner) and
integration tests that run the real kicad-cli (as discovered by `kicad_env`) hidden on `tmp_path` text
fixtures (skipped when it is absent): `test_cli_run_version_real_kicad`,
`test_cli_run_pcb_export_svg_real_kicad`, `test_cli_run_sch_export_netlist_real_kicad` and
`test_cli_run_sch_upgrade_backup_is_byte_exact_real_kicad`. Commands outside those four are validated
and runnable, not individually exercised.

## Explicit denominator

- 20 of 20 tools registered are typed and return a structured response for every input, including
 rejected/unsafe input. The 3 L4 tools and the 5 adaptive tools were added after the earlier 12 and are
 counted under their own bullets below.
- Of the 7 Phase A tools (12 = 7 Phase A + 3 kicad-cli + 2 D1; plus 3 L4 + 5 adaptive = 20): 3 (`kicad_source_list_tree`, `kicad_source_read_file`,
 `kicad_source_search_code`) are implemented, unit-tested against a mocked
 transport, and have been live-verified against the real
 `gitlab.com` API (including recovering from a real Range-handling defect in
 GitLab's raw endpoint; search was exercised on scoped paths only, per the
 receipt cited in the search row above).
- 1 (`kicad_source_resolve_pin`) is **callable_unverified live**:
 implemented and unit-tested against a synthetic transport, with no recorded
 live exercise of its own GitLab call. It is cross-checked only against the
 GitLab tags web page's displayed short commit.
- 1 (`kicad_plugin_activate`) is blocked: intentionally unimplemented
 and always reports `unavailable`.
- 2 (`kicad_capability_report`, `kicad_plugin_inventory`) are local-only,
 fully exercised by tests, no network dependency.
- 3 of 12 (`kicad_cli_commands`, `kicad_cli_describe`, `kicad_cli_run`) are the kicad-cli tools:
 `kicad_cli_commands` and `kicad_cli_describe` are local-only; `kicad_cli_run` is exercised by
 integration tests against the real kicad-cli on a subset of commands (see the kicad-cli section).
- 2 of 12 (`kicad_plugin_inspect`, `kicad_plugin_closure_check`) are the D1 offline plugin tools:
 local-only, no network, execute nothing; their wrapper contract is tested against the real
 `plugin_inspect` with a tmp_path quarantine root (see the D1 section).
- 3 of 20 (`l4_operations`, `l4_run`, `l4_record_identifiers`) are the L4 UI-broker tools, all
 **supported (live-verified)**. All 3 of their operations are recorded, release-approved
 (`registry/APPROVED.sha256`) and live-verified (**supported**). See the L4 section.
- 5 of 20 (`adaptive_capability`, `adaptive_plan`, `adaptive_preview`, `adaptive_apply`,
 `adaptive_undo`) are the adaptive-edit tools, all **supported (live-verified)**. See the Adaptive
 edits section.
- Totals: 3 live-verified + 1 callable_unverified-live + 1 blocked + 2 local-only + 3 kicad-cli +
 2 D1 + 3 L4 + 5 adaptive = 20.
- Not counted because not implemented: D2 retrieval, D3 and X1–X4.
- kicad-cli commands: 43 registered = 40 supported + 1 callable_unverified (`pcb render`) +
 1 blocked (`jobset run`) + 1 deprecated (`pcb export hpgl`); 41 runnable at run level.
- This is **not** a claim of full KiCad 10.0.5 API or GitLab coverage: no
 commit history, merge request, issue, or wiki data is exposed; no
 authenticated GitLab access is supported. The GitLab and plugin-inventory tools perform no write
 of any kind; the only writes in this package are the output files, and for authorised mutating
 commands the in-place rewrite plus backup, performed by `kicad_cli_run` as described above.
