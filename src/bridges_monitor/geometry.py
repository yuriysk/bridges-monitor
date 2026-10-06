"""Геометрія проїзних частин мостів (road_geometry.toml) і базові геодезичні функції.

Точки — (lat, lon) у WGS84. Для відстаней на масштабі міста достатньо
рівнокутної (equirectangular) апроксимації.
"""

import itertools
import math
import tomllib
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from bridges_monitor.catalog import Catalog, Direction

GEOMETRY_RESOURCE = files("bridges_monitor") / "data" / "road_geometry.toml"
EARTH_RADIUS_M = 6_371_000

type LatLon = tuple[float, float]


def distance_m(a: LatLon, b: LatLon) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return EARTH_RADIUS_M * math.hypot(dx, dy)


def bearing_deg(a: LatLon, b: LatLon) -> float:
    """Азимут від a до b: 0 — північ, 90 — схід."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return math.degrees(math.atan2(dx, dy)) % 360


def angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def path_length_m(points: Sequence[LatLon]) -> float:
    return sum(distance_m(a, b) for a, b in itertools.pairwise(points))


def point_along(points: Sequence[LatLon], fraction: float) -> LatLon:
    """Точка на ламаній на відстані fraction від її довжини."""
    target = path_length_m(points) * fraction
    for a, b in itertools.pairwise(points):
        step = distance_m(a, b)
        if step and target <= step:
            return lerp(a, b, target / step)
        target -= step
    return points[-1]


def lerp(a: LatLon, b: LatLon, t: float) -> LatLon:
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def nearest_segment(
    p: LatLon, points: Sequence[LatLon]
) -> tuple[float, LatLon, LatLon]:
    """Найближчий до p відрізок ламаної: (відстань у метрах, початок, кінець)."""

    def to_xy(q: LatLon) -> tuple[float, float]:
        lat0 = math.radians(p[0])
        return (
            math.radians(q[1] - p[1]) * math.cos(lat0) * EARTH_RADIUS_M,
            math.radians(q[0] - p[0]) * EARTH_RADIUS_M,
        )

    if len(points) < 2:
        raise ValueError("path needs at least two points")
    best = (math.inf, points[0], points[1])
    for a, b in itertools.pairwise(points):
        (ax, ay), (bx, by) = to_xy(a), to_xy(b)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        dist = math.hypot(ax + t * dx, ay + t * dy)
        if dist < best[0]:
            best = (dist, a, b)
    return best


class RoadSegment(BaseModel):
    """Одна споруда у складі мосту (наприклад, міст через Десенку) в одному напрямку."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    osm_ways: tuple[int, ...]
    # Початок, середина, кінець у напрямку руху.
    points: tuple[LatLon, LatLon, LatLon]

    @property
    def start(self) -> LatLon:
        return self.points[0]

    @property
    def mid(self) -> LatLon:
        return self.points[1]

    @property
    def end(self) -> LatLon:
        return self.points[2]

    @property
    def bearing(self) -> float:
        return bearing_deg(self.start, self.end)

    @property
    def length_m(self) -> float:
        return path_length_m(self.points)


class RoadProbe(BaseModel):
    """Автомобільна проїзна частина мосту в одному напрямку."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bridge: str
    direction: Direction
    # Споруди мосту в порядку руху; між ними — звичайна дорога.
    segments: tuple[RoadSegment, ...]
    # Точки на 20% першої і 80% останньої споруди (лінії з OSM) — для
    # маршрутизаторів. Мають лежати саме на проїзній частині: точки з інтерполяції
    # по прямій на вигнутих мостах потрапляють на сусідні дороги, і маршрут іде
    # в об'їзд.
    route_points: tuple[LatLon, LatLon]

    @model_validator(mode="after")
    def _direction_matches_geometry(self) -> Self:
        if not self.segments:
            raise ValueError(f"{self.bridge}/{self.direction}: no segments")
        eastward = self.direction is Direction.TO_LEFT_BANK
        pairs = [(s.start, s.end) for s in self.segments]
        pairs.append(self.route_points)
        for a, b in pairs:
            if (b[1] > a[1]) != eastward:
                raise ValueError(
                    f"{self.bridge}/{self.direction}: geometry points the wrong way"
                )
        return self

    @property
    def start(self) -> LatLon:
        return self.segments[0].start

    @property
    def end(self) -> LatLon:
        return self.segments[-1].end

    @property
    def bearing(self) -> float:
        return bearing_deg(self.start, self.end)

    @property
    def length_m(self) -> float:
        """Довжина споруд плюс відстані між ними (дорога між спорудами)."""
        gaps = sum(
            distance_m(a.end, b.start) for a, b in itertools.pairwise(self.segments)
        )
        return sum(s.length_m for s in self.segments) + gaps

    @property
    def route_endpoints(self) -> tuple[LatLon, LatLon]:
        return self.route_points


def load_road_probes(
    catalog: Catalog, path: Path | None = None
) -> tuple[RoadProbe, ...]:
    raw = path.read_bytes() if path else GEOMETRY_RESOURCE.read_bytes()
    probes = tuple(
        RoadProbe.model_validate(r) for r in tomllib.loads(raw.decode())["road"]
    )
    seen: set[tuple[str, Direction]] = set()
    known = {b.id for b in catalog.bridges}
    for probe in probes:
        if probe.bridge not in known:
            raise ValueError(f"road geometry: unknown bridge {probe.bridge!r}")
        key = (probe.bridge, probe.direction)
        if key in seen:
            raise ValueError(f"road geometry: duplicate {key}")
        seen.add(key)
    return probes
