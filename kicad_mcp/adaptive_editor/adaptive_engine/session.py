"""Preview / apply / undo for .kicad_pcb files, with DRC identity comparison and zone refill.

Runs under KiCad's python.exe. Every subprocess (kicad-cli, an optional SVG converter) is started
hidden: ``CREATE_NO_WINDOW`` with the no-crash-dialog error mode inherited, stdin ``DEVNULL``, a timeout, and the exact child is
killed on timeout. Nothing is written outside ``<board dir>/.adaptive/<session>/`` and, for ``apply``
and ``undo``, the board file itself (atomic temp + ``os.replace`` in the board's own directory).

The user's board is only ever *read*: ``plan`` and ``preview`` copy it (plus its ``.kicad_pro`` and
``.kicad_dru``) into a scratch or session directory and let pcbnew load and save those copies, so
pcbnew's project handling can never write next to the original.

Session directory
-----------------
``original.kicad_pcb`` (copy), ``candidate.kicad_pcb`` (refilled result), ``candidate.svg`` /
``candidate.png`` (render), ``drc-original.json`` / ``drc-candidate.json`` (kicad-cli reports),
``backup.kicad_pcb`` (byte-exact copy of the original, made by ``apply``) and ``session.json``
(the journal: request, report, changeset, sha256 of original / candidate / backup, status).

Outcomes
--------
``SessionRefused`` (the CLI maps it to exit 2): the request was understood but a guard said no
(``original_changed``, ``expected_sha256_mismatch``, ``drc_regressions``, ``board_changed_since_apply``,
``not_applied``, ``already_applied``...). ``SessionError`` (exit 1): something failed
(``board_not_found``, ``session_not_found``, ``drc``/``render`` infrastructure, ``replace_failed``...).
DRC that cannot run is never silently "clean": the preview is marked ``drc_regressions`` with the
reason ``drc_unavailable`` so that ``apply`` needs ``force``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from typing import Any, Callable, Mapping

from . import pcbnew_adapter as adapter
from .model import Request
from .solver import SolveResult, solve

__all__ = [
    "AdaptiveError",
    "SessionRefused",
    "SessionError",
    "capability",
    "compare_drc",
    "find_kicad_cli",
    "plan",
    "preview",
    "apply",
    "undo",
    "run_drc",
    "run_hidden",
    "sha256_file",
]

ADAPTIVE_DIR = ".adaptive"
JOURNAL = "session.json"
SIDECARS = (".kicad_pro", ".kicad_dru")
RENDER_LAYERS = "F.Cu,B.Cu,F.CrtYd,B.CrtYd,Edge.Cuts"
_CREATE_NO_WINDOW = 0x08000000


class AdaptiveError(Exception):
    """Base of the typed outcomes: ``code`` machine readable, ``message`` human readable."""

    def __init__(self, code: str, message: str = "", details: Mapping | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class SessionRefused(AdaptiveError):
    """A guard refused a valid request (CLI exit 2)."""


class SessionError(AdaptiveError):
    """Something failed (CLI exit 1)."""


# ----------------------------------------------------------------------------- files and processes


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclasses.dataclass
class HiddenResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_hidden(cmd: list[str], timeout: float, cwd: str | None = None) -> HiddenResult:
    """Run a child with no window, stdin from DEVNULL and a hard timeout. On Windows the child runs in its
    own Job Object: on timeout, and when it finishes, the WHOLE tree it started is ended (orphans included),
    and nothing it starts can raise crash or error dialogs."""
    kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, close_fds=True
    )
    old_mode = job = None
    if os.name == "nt":
        import ctypes
        kwargs["creationflags"] = _CREATE_NO_WINDOW  # console tools: no console window at all
        old_mode = ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)  # inherited: no crash dialogs
    try:
        if os.name == "nt":
            from .job import popen_contained
            proc, job = popen_contained(cmd, **kwargs)
        else:
            proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return HiddenResult(-1, "", "spawn failed: %r" % (exc,))
    finally:
        if old_mode is not None:
            ctypes.windll.kernel32.SetErrorMode(old_mode)
    try:
        out, err = proc.communicate(timeout=timeout)
        return HiddenResult(
            proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        )
    except subprocess.TimeoutExpired:
        if job is not None:
            job.terminate()  # the child and everything it started
        else:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = b"", b""
        return HiddenResult(-1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), True)
    finally:
        if job is not None:
            job.close()  # nothing the child started outlives the call


def _kicad_env():
    try:
        import kicad_env  # adaptive_editor/ on sys.path (KiCad python, plugin)
    except ImportError:
        from kicad_mcp.adaptive_editor import kicad_env
    return kicad_env


def find_kicad_cli(explicit: str | None = None) -> str | None:
    """kicad-cli: explicit argument, ``ADAPTIVE_KICAD_CLI``, next to this python, then kicad_env discovery
    (KICAD_APP_PATH / per-machine config / platform discovery), then PATH."""
    try:
        discovered = str(_kicad_env().kicad_cli())
    except Exception:  # discovery failure just removes one candidate
        discovered = None
    candidates = [
        explicit,
        os.environ.get("ADAPTIVE_KICAD_CLI"),
        os.path.join(os.path.dirname(sys.executable), "kicad-cli.exe"),
        discovered,
        shutil.which("kicad-cli"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


def _atomic_write_bytes(dest: str, source_path: str, expected_sha: str, tag: str) -> None:
    """Copy ``source_path`` over ``dest`` through a temp file in ``dest``'s directory (+ os.replace)."""
    directory = os.path.dirname(dest)
    tmp = os.path.join(directory, f".{os.path.basename(dest)}.adaptive-{tag}.tmp")
    try:
        with open(source_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256_file(tmp) != expected_sha:
            raise SessionError("write_verify_failed", "temp file does not match the expected content")
        os.replace(tmp, dest)
    except OSError as exc:
        raise SessionError("replace_failed", repr(exc))
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def _atomic_write_json(path: str, data: Mapping) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _copy_board_set(src_pcb: str, dst_pcb: str) -> None:
    shutil.copyfile(src_pcb, dst_pcb)
    src_stem, dst_stem = os.path.splitext(src_pcb)[0], os.path.splitext(dst_pcb)[0]
    for ext in SIDECARS:
        if os.path.isfile(src_stem + ext):
            shutil.copyfile(src_stem + ext, dst_stem + ext)


def _adaptive_root(board_path: str) -> str:
    return os.path.join(os.path.dirname(board_path), ADAPTIVE_DIR)


def _new_session_dir(board_path: str, prefix: str = "") -> tuple[str, str]:
    session_id = prefix + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    path = os.path.join(_adaptive_root(board_path), session_id)
    os.makedirs(path)
    return session_id, path


def _drop_dir(path: str, board_path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(OSError):
        os.rmdir(_adaptive_root(board_path))  # only succeeds when empty


@contextlib.contextmanager
def _scratch(board_path: str, prefix: str):
    _, path = _new_session_dir(board_path, prefix)
    try:
        yield path
    finally:
        _drop_dir(path, board_path)


def _abs_board(board_path: str) -> str:
    path = os.path.abspath(os.fspath(board_path))
    if not os.path.isfile(path):
        raise SessionError("board_not_found", path)
    return path


# ----------------------------------------------------------------------------- DRC


def _violation_entries(report: Mapping) -> list[tuple[tuple, dict]]:
    out = []
    for key in ("violations", "unconnected_items", "schematic_parity"):
        for v in report.get(key) or []:
            if not isinstance(v, Mapping) or v.get("excluded"):
                continue
            items = v.get("items") or []
            uuids = tuple(sorted(str(i.get("uuid", "")) for i in items if isinstance(i, Mapping)))
            ident = (str(v.get("type") or key), uuids)
            out.append(
                (
                    ident,
                    {
                        "type": ident[0],
                        "uuids": list(uuids),
                        "severity": v.get("severity"),
                        "description": v.get("description"),
                    },
                )
            )
    return out


def compare_drc(original: Mapping, candidate: Mapping) -> dict:
    """Compare violation identities (type + sorted item uuids), never just counts.

    A violation of the candidate is *new* when the candidate has more instances of that identity than
    the original. So swapping one violation for another keeps the count equal and still reports one
    new and one resolved.
    """
    o_entries, c_entries = _violation_entries(original), _violation_entries(candidate)
    o_count = Counter(ident for ident, _ in o_entries)
    c_count = Counter(ident for ident, _ in c_entries)
    extra = c_count - o_count
    gone = o_count - c_count
    new, resolved = [], []
    left = dict(extra)
    for ident, entry in c_entries:
        if left.get(ident, 0) > 0:
            left[ident] -= 1
            new.append(entry)
    left = dict(gone)
    for ident, entry in o_entries:
        if left.get(ident, 0) > 0:
            left[ident] -= 1
            resolved.append(entry)
    return {
        "original_count": len(o_entries),
        "candidate_count": len(c_entries),
        "new": new,
        "resolved": resolved,
        "regressions": bool(new),
    }


def run_drc(board_path: str, out_json: str, cli: str, timeout: float) -> dict:
    """``kicad-cli pcb drc --format json`` on ``board_path``; returns the parsed report."""
    attempts = (
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--output", out_json, board_path],
        [cli, "pcb", "drc", "--format", "json", "--output", out_json, board_path],
    )
    last = ""
    for cmd in attempts:
        if os.path.exists(out_json):
            os.remove(out_json)
        res = run_hidden(cmd, timeout)
        if res.timed_out:
            raise SessionError("drc_timeout", board_path)
        if os.path.isfile(out_json):
            try:
                with open(out_json, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError) as exc:
                last = "unreadable report: %r" % (exc,)
                continue
        last = (res.stderr or res.stdout or "")[-400:]
    raise SessionError("drc_failed", last)


def _drc_both(sdir: str, cli: str | None, runner: Callable, timeout: float) -> dict:
    """Run DRC on the session's original and candidate copies and compare."""
    try:
        if runner is run_drc and not cli:
            raise SessionError("kicad_cli_missing", "kicad-cli.exe not found")
        original = runner(os.path.join(sdir, "original.kicad_pcb"), os.path.join(sdir, "drc-original.json"), cli, timeout)
        candidate = runner(os.path.join(sdir, "candidate.kicad_pcb"), os.path.join(sdir, "drc-candidate.json"), cli, timeout)
    except AdaptiveError as exc:
        return {"available": False, "error": exc.code, "message": exc.message}
    result = compare_drc(original, candidate)
    result["available"] = True
    return result


# ----------------------------------------------------------------------------- rendering


def _convert_png(svg: str, png: str, timeout: float) -> bool:
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(url=svg, write_to=png, output_width=1400)
        return os.path.isfile(png)
    except Exception:
        pass
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        run_hidden([rsvg, "-w", "1400", "-o", png, svg], timeout)
        if os.path.isfile(png):
            return True
    inkscape = shutil.which("inkscape")
    if inkscape:
        run_hidden([inkscape, svg, "--export-type=png", "--export-filename=" + png, "-w", "1400"], timeout)
        if os.path.isfile(png):
            return True
    return False


def _render(board_path: str, sdir: str, cli: str | None, timeout: float) -> dict:
    """``kicad-cli pcb export svg`` then PNG when a converter exists; the SVG is kept either way."""
    out = {"ok": False, "svg_path": None, "png_path": None, "warnings": []}
    if not cli:
        out["warnings"].append("render_skipped:kicad_cli_missing")
        return out
    svg = os.path.join(sdir, "candidate.svg")
    attempts = (
        [cli, "pcb", "export", "svg", "--output", svg, "--layers", RENDER_LAYERS, "--mode-single",
         "--page-size-mode", "2", "--exclude-drawing-sheet", board_path],
        [cli, "pcb", "export", "svg", "--output", svg, "--layers", RENDER_LAYERS, board_path],
    )
    produced = None
    for cmd in attempts:
        res = run_hidden(cmd, timeout)
        if res.timed_out:
            out["warnings"].append("render_timeout")
            return out
        if os.path.isfile(svg):
            produced = svg
        elif os.path.isdir(svg):
            found = sorted(f for f in os.listdir(svg) if f.lower().endswith(".svg"))
            produced = os.path.join(svg, found[0]) if found else None
        if produced:
            break
    if not produced:
        out["warnings"].append("render_failed")
        return out
    out["ok"] = True
    out["svg_path"] = produced
    png = os.path.join(sdir, "candidate.png")
    if _convert_png(produced, png, timeout):
        out["png_path"] = png
    else:
        out["warnings"].append("no_svg_converter:svg_kept")
    return out


# ----------------------------------------------------------------------------- engine glue


def _request_dict(request) -> dict:
    if isinstance(request, Request):
        return request.to_dict()
    if isinstance(request, Mapping):
        try:
            return json.loads(json.dumps(request))
        except (TypeError, ValueError):
            return {}
    return {}


def _solve(model, request) -> SolveResult:
    try:
        return solve(model, request)
    except Exception as exc:
        raise SessionError("solver_failed", repr(exc))


def _solve_payload(result: SolveResult, load_warnings) -> dict:
    report = result.report
    merged = tuple(sorted(set(report.warnings) | set(load_warnings)))
    report = dataclasses.replace(report, warnings=merged)
    return {
        "status": report.status,
        "changeset": result.changeset.to_dict(),
        "report": report.to_dict(),
        "wave": list(result.wave),
        "pre_existing": [dict(p) for p in result.pre_existing],
    }


def _load(path: str) -> adapter.LoadInfo:
    try:
        return adapter.load_board_with_info(path)
    except adapter.AdapterError as exc:
        raise SessionError(exc.code, exc.message)


def plan(board_path: str, request) -> dict:
    """Solve only: no candidate, no session. Loads a scratch copy so the original is never opened by pcbnew."""
    board_path = _abs_board(board_path)
    sha = sha256_file(board_path)
    with _scratch(board_path, "_plan-") as sdir:
        copy = os.path.join(sdir, "original.kicad_pcb")
        _copy_board_set(board_path, copy)
        info = _load(copy)
        result = _solve(info.board, request)
    payload = _solve_payload(result, info.warnings)
    payload["original_sha256"] = sha
    payload["board_path"] = board_path
    return payload


def preview(
    board_path: str,
    request,
    *,
    kicad_cli: str | None = None,
    render: bool = True,
    drc_runner: Callable | None = None,
    timeout: float = 300.0,
) -> dict:
    # covers: ADE-3
    """Solve, apply to a copy, refill zones, save ``candidate.kicad_pcb`` in a new session, render, DRC.

    Returns ``{session, session_id, status, changeset, report, wave, pre_existing, candidate_path,
    png_path, svg_path, drc, drc_regressions, regression_reasons, refill, connectivity,
    original_sha256, candidate_sha256}``. With nothing to change (blocked / rejected / no-op) no
    session is kept: ``session``, ``candidate_path`` and ``drc`` are ``None``.
    """
    board_path = _abs_board(board_path)
    original_sha = sha256_file(board_path)
    cli = find_kicad_cli(kicad_cli)
    runner = drc_runner or run_drc
    session_id, sdir = _new_session_dir(board_path)
    keep = False
    try:
        original_copy = os.path.join(sdir, "original.kicad_pcb")
        candidate_path = os.path.join(sdir, "candidate.kicad_pcb")
        _copy_board_set(board_path, original_copy)
        _copy_board_set(board_path, candidate_path)
        if sha256_file(original_copy) != original_sha:
            raise SessionError("original_changed_during_preview", board_path)

        info = _load(original_copy)
        result = _solve(info.board, request)
        payload = _solve_payload(result, info.warnings)
        payload.update(original_sha256=original_sha, board_path=board_path)
        if result.changeset.is_empty():
            payload.update(
                session=None, session_id=None, candidate_path=None, png_path=None, svg_path=None,
                drc=None, drc_regressions=False, regression_reasons=[], refill=None, connectivity=None,
                candidate_sha256=None,
            )
            return payload

        before_conn = adapter.connectivity_stats(info.pcb)
        before_areas = adapter.zone_filled_areas(info.pcb)
        try:
            working = adapter.open_board(candidate_path)
            adapter.apply_changeset(working, result.changeset)
            zones_filled = adapter.fill_zones(working)
            after_areas = adapter.zone_filled_areas(working)
            after_conn = adapter.connectivity_stats(working)
            adapter.save_board(working, candidate_path)
        except adapter.AdapterError as exc:
            raise SessionError(exc.code, exc.message)
        candidate_sha = sha256_file(candidate_path)

        rendered = _render(candidate_path, sdir, cli, timeout) if render else {
            "ok": False, "svg_path": None, "png_path": None, "warnings": ["render_disabled"]}
        drc = _drc_both(sdir, cli, runner, timeout)

        reasons = []
        if not drc.get("available"):
            reasons.append("drc_unavailable")
        elif drc["regressions"]:
            reasons.append("drc_new_violations")
        b_un, a_un = before_conn["unconnected"], after_conn["unconnected"]
        if b_un is not None and a_un is not None and a_un > b_un:
            reasons.append("connectivity_worsened")
        if before_conn["pad_nets"] != after_conn["pad_nets"]:
            reasons.append("pad_nets_changed")

        refill = {
            "zones_refilled": zones_filled,
            "filled_area_before_nm2": before_areas,
            "filled_area_after_nm2": after_areas,
            "changed": sorted(u for u in after_areas if before_areas.get(u) != after_areas[u]),
        }
        connectivity = {
            "unconnected_before": b_un,
            "unconnected_after": a_un,
            "pad_nets_unchanged": before_conn["pad_nets"] == after_conn["pad_nets"],
        }
        payload.update(
            session=sdir,
            session_id=session_id,
            candidate_path=candidate_path,
            png_path=rendered["png_path"],
            svg_path=rendered["svg_path"],
            render=rendered,
            drc=drc,
            drc_regressions=bool(reasons),
            regression_reasons=reasons,
            refill=refill,
            connectivity=connectivity,
            candidate_sha256=candidate_sha,
        )
        journal = {
            "version": 1,
            "session_id": session_id,
            "board_path": board_path,
            "created_utc": _now(),
            "status": "previewed",
            "request": _request_dict(request),
            "changeset": payload["changeset"],
            "report": payload["report"],
            "drc": drc,
            "drc_regressions": bool(reasons),
            "regression_reasons": reasons,
            "sha256": {"original": original_sha, "candidate": candidate_sha, "backup": None, "applied": None},
            "files": {"candidate": candidate_path, "original_copy": original_copy},
        }
        _atomic_write_json(os.path.join(sdir, JOURNAL), journal)
        keep = True
        return payload
    except AdaptiveError:
        raise
    except Exception as exc:
        raise SessionError("preview_failed", repr(exc))
    finally:
        if not keep:
            _drop_dir(sdir, board_path)


# ----------------------------------------------------------------------------- apply / undo


def _resolve_session(session: str, board_path: str | None = None) -> str:
    """Session directory from a directory path, a journal path, or an id plus the board path."""
    path = os.path.abspath(os.fspath(session))
    if os.path.isfile(path) and os.path.basename(path) == JOURNAL:
        path = os.path.dirname(path)
    elif not os.path.isdir(path) and board_path and os.sep not in session and "/" not in session:
        path = os.path.join(_adaptive_root(os.path.abspath(board_path)), session)
    if not os.path.isfile(os.path.join(path, JOURNAL)):
        raise SessionError("session_not_found", session)
    if os.path.basename(os.path.dirname(path)) != ADAPTIVE_DIR:
        raise SessionError("bad_session", "session directory is not under a .adaptive folder")
    return path


def _read_journal(sdir: str) -> dict:
    try:
        with open(os.path.join(sdir, JOURNAL), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise SessionError("journal_unreadable", repr(exc))


def apply(session: str, expected_sha256: str, force: bool = False, board_path: str | None = None) -> dict:
    # covers: ADE-3
    """Atomically replace the board with the session's candidate.

    Refuses (``SessionRefused``) when the original's sha256 differs from the journal or from
    ``expected_sha256`` (concurrent edit), when the candidate file was altered, when the session was
    already applied, or when ``drc_regressions`` and not ``force``. A byte-exact backup is written
    first; the replace is temp file + ``os.replace`` in the board's directory.
    """
    if not isinstance(expected_sha256, str) or not expected_sha256:
        raise SessionError("bad_request", "expected_sha256 is required")
    sdir = _resolve_session(session, board_path)
    journal = _read_journal(sdir)
    board = journal["board_path"]
    if journal.get("status") != "previewed":
        raise SessionRefused("already_applied" if journal.get("status") == "applied" else "not_previewed",
                             f"session status is {journal.get('status')!r}")
    if not os.path.isfile(board):
        raise SessionError("board_not_found", board)
    shas = journal["sha256"]
    candidate = os.path.join(sdir, "candidate.kicad_pcb")
    if not os.path.isfile(candidate) or sha256_file(candidate) != shas["candidate"]:
        raise SessionRefused("candidate_modified", "candidate file differs from the previewed one")
    current = sha256_file(board)
    if current != shas["original"]:
        raise SessionRefused("original_changed", "the board changed since the preview",
                             {"current_sha256": current, "previewed_sha256": shas["original"]})
    if current != expected_sha256:
        raise SessionRefused("expected_sha256_mismatch", "expected_sha256 does not match the board",
                             {"current_sha256": current})
    if journal.get("drc_regressions") and not force:
        raise SessionRefused("drc_regressions", "the candidate introduces DRC regressions; force is required",
                             {"reasons": journal.get("regression_reasons", [])})

    backup = os.path.join(sdir, "backup.kicad_pcb")
    shutil.copyfile(board, backup)
    backup_sha = sha256_file(backup)
    if backup_sha != current:
        os.remove(backup)
        raise SessionError("backup_verify_failed", "backup is not byte-identical")
    try:
        if sha256_file(board) != current:  # last look before the replace
            raise SessionRefused("original_changed", "the board changed while applying")
        _atomic_write_bytes(board, candidate, shas["candidate"], session_tag(sdir))
    except AdaptiveError:
        if os.path.exists(backup) and sha256_file(board) == current:
            os.remove(backup)  # nothing was replaced: do not leave a stale backup
        raise
    if sha256_file(board) != shas["candidate"]:
        _atomic_write_bytes(board, backup, backup_sha, session_tag(sdir) + "r")
        raise SessionError("post_apply_verify_failed", "board does not match the candidate; backup restored")

    shas["backup"] = backup_sha
    shas["applied"] = shas["candidate"]
    journal.update(status="applied", applied_utc=_now(), forced=bool(force), backup_path=backup)
    _atomic_write_json(os.path.join(sdir, JOURNAL), journal)
    warnings = []
    if os.path.exists(board + ".lck") or os.path.exists(os.path.join(os.path.dirname(board), "~" + os.path.basename(board) + ".lck")):
        warnings.append("board_lock_present:board may be open in KiCad")
    return {
        "session": sdir,
        "session_id": journal["session_id"],
        "board_path": board,
        "status": "applied",
        "forced": bool(force),
        "original_sha256": shas["original"],
        "applied_sha256": shas["candidate"],
        "backup_sha256": backup_sha,
        "backup_path": backup,
        "warnings": warnings,
    }


def session_tag(sdir: str) -> str:
    return os.path.basename(sdir)[-8:]


def undo(session: str, board_path: str | None = None) -> dict:
    # covers: ADE-3
    """Restore the backup byte-for-byte, only if the board still equals the applied candidate."""
    sdir = _resolve_session(session, board_path)
    journal = _read_journal(sdir)
    board = journal["board_path"]
    if journal.get("status") != "applied":
        raise SessionRefused("not_applied", f"session status is {journal.get('status')!r}")
    shas = journal["sha256"]
    if not os.path.isfile(board):
        raise SessionError("board_not_found", board)
    current = sha256_file(board)
    if current != shas["applied"]:
        raise SessionRefused("board_changed_since_apply", "the board no longer equals the applied candidate",
                             {"current_sha256": current, "applied_sha256": shas["applied"]})
    backup = os.path.join(sdir, "backup.kicad_pcb")
    if not os.path.isfile(backup) or sha256_file(backup) != shas["backup"] or shas["backup"] != shas["original"]:
        raise SessionRefused("backup_corrupt", "the backup is missing or differs from the recorded sha256")
    _atomic_write_bytes(board, backup, shas["backup"], session_tag(sdir) + "u")
    restored = sha256_file(board)
    if restored != shas["original"]:
        raise SessionError("post_undo_verify_failed", "restored board does not match the original sha256")
    journal.update(status="undone", undone_utc=_now())
    _atomic_write_json(os.path.join(sdir, JOURNAL), journal)
    return {
        "session": sdir,
        "session_id": journal["session_id"],
        "board_path": board,
        "status": "undone",
        "restored_sha256": restored,
    }


# ----------------------------------------------------------------------------- capability


def capability() -> dict:
    """What this installation can do: pcbnew, kicad-cli, headless policy, operations."""
    out: dict[str, Any] = {
        "engine": "adaptive_engine",
        "phase": "A2-file-adapter",
        "operations": ["plan", "preview", "apply", "undo", "capability"],
        "live_ipc": False,
        "python": sys.executable,
    }
    try:
        pcb = adapter.get_pcbnew()
        try:
            version = pcb.GetBuildVersion()
        except Exception:
            version = None
        out["pcbnew"] = {"available": True, "version": version}
    except adapter.AdapterError as exc:
        out["pcbnew"] = {"available": False, "error": exc.code, "message": exc.message}
    out["headless_policy"] = adapter.headless_policy()
    cli = find_kicad_cli()
    cli_info: dict[str, Any] = {"path": cli, "available": bool(cli)}
    if cli:
        res = run_hidden([cli, "--version"], 30.0)
        cli_info["version"] = res.stdout.strip() if res.returncode == 0 else None
    out["kicad_cli"] = cli_info
    converters = []
    try:
        importlib.import_module("cairosvg")  # importable, not merely present

        converters.append("cairosvg")
    except Exception:
        pass
    converters += [n for n in ("rsvg-convert", "inkscape") if shutil.which(n)]
    out["svg_to_png"] = converters
    return out
