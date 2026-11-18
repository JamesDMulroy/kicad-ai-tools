"""Neutral board model for the adaptive engine (pure Python, no pcbnew import).

Units are integer nanometres. A Point is a tuple ``(x, y)`` of ints and a Polygon is a tuple of
Points (implicitly closed, any winding). All model objects are frozen dataclasses holding tuples,
never lists, so boards and change sets are hashable-by-value, safe to share and deterministic.

Conventions other modules rely on
---------------------------------
* ``Board.index`` maps ``uuid -> (kind, object)`` where kind is one of
  ``part|track|via|edge|keepout|zone``. ``Board.part/track/via/edge(uuid)`` raise ``KeyError``.
* ``ChangeSet`` tuples are always sorted by uuid, so equal content gives equal output.
* ``Request.from_dict`` validates shape only; unknown or missing uuids are the solver's job.
  ``Request.driver`` rebuilds the spec's ``driver`` object; ``Request.to_dict()`` round-trips.
* ``RequestError.code`` is a typed code: ``bad_request``, ``bad_driver``, ``bad_displacement``,
  ``bad_mode``, ``bad_copper``, ``bad_eligibility`` or ``duplicate_uuid``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Mapping

Point = tuple[int, int]
Polygon = tuple[Point, ...]

__all__ = [
    "Point",
    "Polygon",
    "Pad",
    "Part",
    "Track",
    "Via",
    "EdgeSeg",
    "Outline",
    "Keepout",
    "Zone",
    "Rules",
    "Contract",
    "Board",
    "PartMove",
    "TrackChange",
    "ViaMove",
    "EdgeChange",
    "ChangeSet",
    "Blocker",
    "Report",
    "Request",
    "RequestError",
    "BLOCKER_KINDS",
    "REPORT_STATUSES",
]


def _tp(pt, dx: int, dy: int) -> Point:
    return (pt[0] + dx, pt[1] + dy)


def _tpoly(poly, dx: int, dy: int) -> Polygon:
    return tuple((p[0] + dx, p[1] + dy) for p in poly)


def _pt(pt) -> list[int]:
    return [int(pt[0]), int(pt[1])]


# ----------------------------------------------------------------------------- board items


@dataclass(frozen=True)
class Pad:
    uuid: str
    net: str
    center: Point
    polygon: Polygon  # absolute coordinates

    def translated(self, dx: int, dy: int) -> "Pad":
        return dataclasses.replace(
            self, center=_tp(self.center, dx, dy), polygon=_tpoly(self.polygon, dx, dy)
        )


@dataclass(frozen=True)
class Part:
    uuid: str
    ref: str
    position: Point
    rotation_mdeg: int = 0
    side: str = "F"
    courtyard: tuple[Polygon, ...] = ()
    locked: bool = False
    pads: tuple[Pad, ...] = ()
    through: bool = False  # has through-hole pads: occupies both board sides

    def __post_init__(self) -> None:
        if self.side not in ("F", "B"):
            raise ValueError(f"Part.side must be 'F' or 'B', got {self.side!r}")

    def translated(self, dx: int, dy: int) -> "Part":
        """A copy moved by (dx, dy): position, courtyard and pads."""
        return dataclasses.replace(
            self,
            position=_tp(self.position, dx, dy),
            courtyard=tuple(_tpoly(poly, dx, dy) for poly in self.courtyard),
            pads=tuple(pad.translated(dx, dy) for pad in self.pads),
        )

    def pad_bbox(self) -> tuple[int, int, int, int] | None:
        """``(minx, miny, maxx, maxy)`` over all pad polygons, or None when there are no pads."""
        pts = [p for pad in self.pads for p in (pad.polygon or (pad.center,))]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def effective_courtyard(self, rules: "Rules") -> tuple[tuple[Polygon, ...], bool]:
        """``(courtyard polygons, missing)``.

        Returns the real courtyard when any polygon is non-empty. Otherwise the pad bounding box
        inflated by ``rules.fallback_margin_nm`` as one rectangle with ``missing=True``; with no
        pads either, ``((), True)``.
        """
        if any(len(poly) > 0 for poly in self.courtyard):
            return self.courtyard, False
        box = self.pad_bbox()
        if box is None:
            return (), True
        m = rules.fallback_margin_nm
        x0, y0, x1, y1 = box[0] - m, box[1] - m, box[2] + m, box[3] + m
        return (((x0, y0), (x1, y0), (x1, y1), (x0, y1)),), True


@dataclass(frozen=True)
class Track:
    uuid: str
    net: str
    layer: str
    start: Point
    end: Point
    width: int
    mid: Point | None = None  # not None means an arc through mid
    locked: bool = False
    group: str | None = None  # tuning-pattern group id


@dataclass(frozen=True)
class Via:
    uuid: str
    net: str
    pos: Point
    diameter: int
    drill: int
    layers: tuple[str, ...] = ()
    locked: bool = False


@dataclass(frozen=True)
class EdgeSeg:
    uuid: str
    start: Point
    end: Point
    mid: Point | None = None  # not None means an arc


@dataclass(frozen=True)
class Outline:
    polygons: tuple[Polygon, ...] = ()  # first is the outer boundary, the rest are holes
    edges: tuple[EdgeSeg, ...] = ()


@dataclass(frozen=True)
class Keepout:
    uuid: str
    polygon: Polygon
    layers: tuple[str, ...] = ()
    no_tracks: bool = False
    no_vias: bool = False
    no_footprints: bool = False
    no_pour: bool = False


@dataclass(frozen=True)
class Zone:
    uuid: str
    net: str
    layers: tuple[str, ...]
    polygon: Polygon


@dataclass(frozen=True)
class Rules:
    clearance_nm: int | None = None
    copper_edge_clearance_nm: int | None = None
    courtyard_clearance_nm: int = 0
    fallback_margin_nm: int = 0
    pair_clearance: dict[tuple[str, str], int] = field(default_factory=dict)  # sorted-key tuples

    def clearance(self, net_a: str, net_b: str) -> int | None:
        """Pair override if present, else the default; None means rule data is missing."""
        key = tuple(sorted((net_a, net_b)))
        if key in self.pair_clearance:
            return self.pair_clearance[key]
        return self.clearance_nm


@dataclass(frozen=True)
class Contract:
    permissions: dict[str, str] = field(default_factory=dict)  # uuid -> 'movable' | 'fixed'


@dataclass(frozen=True)
class Board:
    parts: tuple[Part, ...]
    tracks: tuple[Track, ...] = ()
    vias: tuple[Via, ...] = ()
    outline: Outline = field(default_factory=Outline)
    keepouts: tuple[Keepout, ...] = ()
    zones: tuple[Zone, ...] = ()
    rules: Rules = field(default_factory=Rules)
    contract: Contract | None = None

    @cached_property
    def _maps(self) -> dict[str, dict[str, Any]]:
        return {
            "part": {x.uuid: x for x in self.parts},
            "track": {x.uuid: x for x in self.tracks},
            "via": {x.uuid: x for x in self.vias},
            "edge": {x.uuid: x for x in self.outline.edges},
            "keepout": {x.uuid: x for x in self.keepouts},
            "zone": {x.uuid: x for x in self.zones},
        }

    @cached_property
    def index(self) -> dict[str, tuple[str, Any]]:
        """``uuid -> (kind, object)`` over every addressable item (first kind wins on a clash)."""
        idx: dict[str, tuple[str, Any]] = {}
        for kind, items in self._maps.items():
            for uuid, obj in items.items():
                idx.setdefault(uuid, (kind, obj))
        return idx

    def part(self, uuid: str) -> Part:
        return self._maps["part"][uuid]

    def track(self, uuid: str) -> Track:
        return self._maps["track"][uuid]

    def via(self, uuid: str) -> Via:
        return self._maps["via"][uuid]

    def edge(self, uuid: str) -> EdgeSeg:
        return self._maps["edge"][uuid]


# ----------------------------------------------------------------------------- change set


@dataclass(frozen=True)
class PartMove:
    uuid: str
    dx: int
    dy: int


@dataclass(frozen=True)
class TrackChange:
    uuid: str
    start: Point
    end: Point
    bend: Point | None = None  # segment becomes start -> bend -> end (adapter splits it)


@dataclass(frozen=True)
class ViaMove:
    uuid: str
    dx: int
    dy: int


@dataclass(frozen=True)
class EdgeChange:
    uuid: str
    start: Point
    end: Point


def _sorted_by_uuid(items) -> tuple:
    return tuple(sorted(items, key=lambda x: x.uuid))


@dataclass(frozen=True)
class ChangeSet:
    part_moves: tuple[PartMove, ...] = ()
    track_changes: tuple[TrackChange, ...] = ()
    via_moves: tuple[ViaMove, ...] = ()
    edge_changes: tuple[EdgeChange, ...] = ()

    def __post_init__(self) -> None:
        for name in ("part_moves", "track_changes", "via_moves", "edge_changes"):
            object.__setattr__(self, name, _sorted_by_uuid(getattr(self, name)))

    def is_empty(self) -> bool:
        return not (self.part_moves or self.track_changes or self.via_moves or self.edge_changes)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe content with every list sorted by uuid."""
        return {
            "part_moves": [
                {"uuid": m.uuid, "dx": int(m.dx), "dy": int(m.dy)} for m in self.part_moves
            ],
            "track_changes": [
                {
                    "uuid": t.uuid,
                    "start": _pt(t.start),
                    "end": _pt(t.end),
                    "bend": None if t.bend is None else _pt(t.bend),
                }
                for t in self.track_changes
            ],
            "via_moves": [
                {"uuid": m.uuid, "dx": int(m.dx), "dy": int(m.dy)} for m in self.via_moves
            ],
            "edge_changes": [
                {"uuid": e.uuid, "start": _pt(e.start), "end": _pt(e.end)}
                for e in self.edge_changes
            ],
        }


# ----------------------------------------------------------------------------- report


BLOCKER_KINDS = frozenset(
    {
        "frozen",
        "locked",
        "contract",
        "keepout",
        "edge",
        "ineligible",
        "copper",
        "tuned_leg",
        "leg_blocked",
    }
)
REPORT_STATUSES = frozenset({"ok", "partial", "blocked", "rejected"})


@dataclass(frozen=True)
class Blocker:
    uuid: str | None
    kind: str
    at_nm: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in BLOCKER_KINDS:
            raise ValueError(f"unknown blocker kind {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "kind": self.kind, "at_nm": self.at_nm}


@dataclass(frozen=True)
class Report:
    status: str
    requested_nm: int = 0
    achieved_nm: int = 0
    blockers: tuple[Blocker, ...] = ()
    warnings: tuple[str, ...] = ()  # e.g. 'missing_courtyard:<uuid>'
    reject_reason: str | None = None  # typed code
    legs: tuple[dict, ...] = ()
    moved_parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise ValueError(f"unknown report status {self.status!r}")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "moved_parts", tuple(self.moved_parts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested_nm": self.requested_nm,
            "achieved_nm": self.achieved_nm,
            "blockers": [b.to_dict() for b in self.blockers],
            "warnings": list(self.warnings),
            "reject_reason": self.reject_reason,
            "legs": [dict(leg) for leg in self.legs],
            "moved_parts": sorted(self.moved_parts),
        }


# ----------------------------------------------------------------------------- request


class RequestError(Exception):
    """Typed request failure: ``code`` is machine readable, ``message`` human readable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


_REQUEST_KEYS = frozenset(
    {
        "driver",
        "displacement_nm",
        "mode",
        "movers",
        "frozen",
        "priority",
        "eligibility_nm",
        "allow_partial",
        "copper",
    }
)
_MODES = ("cascade", "explicit")
_COPPER = ("adapt", "fixed")
_SIDES = ("left", "right", "top", "bottom")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _uuid_list(value: Any, name: str, code: str = "bad_request") -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise RequestError(code, f"{name} must be a list of uuid strings")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise RequestError(code, f"{name} must contain non-empty strings")
        if item in seen:
            raise RequestError("duplicate_uuid", f"{name} repeats {item}")
        seen.add(item)
    return tuple(value)


@dataclass(frozen=True)
class Request:
    driver_kind: str  # 'part' | 'edge'
    displacement_nm: tuple[int, int]
    driver_uuids: tuple[str, ...] = ()  # part drivers
    edge_uuid: str | None = None  # edge drivers
    side: str | None = None  # edge drivers: left|right|top|bottom
    mode: str = "cascade"
    movers: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()
    priority: tuple[str, ...] = ()
    eligibility_nm: int | None = None
    allow_partial: bool = True
    copper: str = "adapt"

    @property
    def driver(self) -> dict[str, Any]:
        if self.driver_kind == "part":
            return {"kind": "part", "uuids": list(self.driver_uuids)}
        return {"kind": "edge", "edge_uuid": self.edge_uuid, "side": self.side}

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "displacement_nm": [self.displacement_nm[0], self.displacement_nm[1]],
            "mode": self.mode,
            "movers": list(self.movers),
            "frozen": list(self.frozen),
            "priority": list(self.priority),
            "eligibility_nm": self.eligibility_nm,
            "allow_partial": self.allow_partial,
            "copper": self.copper,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Request":
        """Validate a JSON request. Raises ``RequestError``; never silently dedupes or defaults a
        malformed field."""
        if not isinstance(d, Mapping):
            raise RequestError("bad_request", "request must be an object")
        unknown = sorted(str(k) for k in d if k not in _REQUEST_KEYS)
        if unknown:
            raise RequestError("bad_request", f"unknown field(s): {', '.join(unknown)}")

        driver = d.get("driver")
        if not isinstance(driver, Mapping):
            raise RequestError("bad_driver", "driver must be an object")
        kind = driver.get("kind")
        driver_uuids: tuple[str, ...] = ()
        edge_uuid: str | None = None
        side: str | None = None
        if kind == "part":
            extra = sorted(str(k) for k in driver if k not in ("kind", "uuids"))
            if extra:
                raise RequestError("bad_driver", f"unknown driver field(s): {', '.join(extra)}")
            driver_uuids = _uuid_list(driver.get("uuids"), "driver.uuids", "bad_driver")
            if not driver_uuids:
                raise RequestError("bad_driver", "driver.uuids must not be empty")
        elif kind == "edge":
            extra = sorted(str(k) for k in driver if k not in ("kind", "edge_uuid", "side"))
            if extra:
                raise RequestError("bad_driver", f"unknown driver field(s): {', '.join(extra)}")
            edge_uuid = driver.get("edge_uuid")
            if not isinstance(edge_uuid, str) or not edge_uuid:
                raise RequestError("bad_driver", "driver.edge_uuid must be a non-empty string")
            side = driver.get("side")
            if side not in _SIDES:
                raise RequestError("bad_driver", f"driver.side must be one of {_SIDES}")
        else:
            raise RequestError("bad_driver", "driver.kind must be 'part' or 'edge'")

        disp = d.get("displacement_nm")
        if (
            not isinstance(disp, (list, tuple))
            or len(disp) != 2
            or not all(_is_int(v) for v in disp)
        ):
            raise RequestError("bad_displacement", "displacement_nm must be two integers")

        mode = d.get("mode", "cascade")
        if mode not in _MODES:
            raise RequestError("bad_mode", f"mode must be one of {_MODES}")

        copper = d.get("copper", "adapt")
        if copper not in _COPPER:
            raise RequestError("bad_copper", f"copper must be one of {_COPPER}")

        movers = _uuid_list(d.get("movers"), "movers")
        frozen = _uuid_list(d.get("frozen"), "frozen")
        priority = _uuid_list(d.get("priority"), "priority")

        elig = d.get("eligibility_nm")
        if elig is not None and (not _is_int(elig) or elig < 0):
            raise RequestError("bad_eligibility", "eligibility_nm must be null or an int >= 0")

        allow_partial = d.get("allow_partial", True)
        if not isinstance(allow_partial, bool):
            raise RequestError("bad_request", "allow_partial must be a boolean")

        return cls(
            driver_kind=kind,
            displacement_nm=(int(disp[0]), int(disp[1])),
            driver_uuids=driver_uuids,
            edge_uuid=edge_uuid,
            side=side,
            mode=mode,
            movers=movers,
            frozen=frozen,
            priority=priority,
            eligibility_nm=elig,
            allow_partial=allow_partial,
            copper=copper,
        )
