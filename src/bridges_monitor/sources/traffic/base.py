"""Спільна основа джерел даних про затори на мостах.

Підклас реалізує read(probe) — показник для проїзної частини мосту в одному
напрямку. Точкові провайдери опитують кожну споруду мосту (probe.segments) і
зводять результати через worst_reading(); маршрутизатори будують один маршрут
між probe.route_points. Базовий клас опитує всі проїзні частини паралельно й перетворює
показники на твердження (Claim) про завантаженість.

Закриття (status=closed) ставиться лише тоді, коли провайдер повідомляє про нього
явно; за низькою швидкістю чи об'їздом маршруту закриття не виводиться.
"""

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from bridges_monitor.catalog import TransportMode
from bridges_monitor.geometry import (
    LatLon,
    RoadProbe,
    RoadSegment,
    angle_diff_deg,
    bearing_deg,
    nearest_segment,
)
from bridges_monitor.models import (
    CONGESTION_SEVERITY,
    CongestionLevel,
    Report,
    SourceKind,
    TrafficStatus,
)
from bridges_monitor.schedule import PollSchedule
from bridges_monitor.sources.base import Source, SourceError
from bridges_monitor.sources.http import HttpClient

log = logging.getLogger(__name__)

# Межі частки поточної швидкості від швидкості вільного руху.
_SPEED_RATIO_LEVELS = (
    (0.75, CongestionLevel.FREE),
    (0.5, CongestionLevel.MODERATE),
    (0.25, CongestionLevel.HEAVY),
)
# Сегмент провайдера вважається тією самою проїзною частиною, якщо його напрямок
# відхиляється не більше ніж на стільки градусів і він проходить поруч із серединою.
MAX_BEARING_DIFF_DEG = 45
MAX_OFFSET_M = 40
# Скільки інтервалів опитування діє показник, якщо його не оновили.
VALID_FOR_POLLS = 3


class ProbeReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    congestion: CongestionLevel | None = None
    closed: bool = False
    speed_kmh: float | None = None
    free_flow_kmh: float | None = None


def congestion_from_speed_ratio(current: float, free_flow: float) -> CongestionLevel:
    ratio = current / free_flow if free_flow > 0 else 0.0
    for threshold, level in _SPEED_RATIO_LEVELS:
        if ratio >= threshold:
            return level
    return CongestionLevel.STANDSTILL


def dominant_congestion(
    weighted: Iterable[tuple[CongestionLevel, float]], min_share: float = 0.3
) -> CongestionLevel | None:
    """Найважчий рівень, який разом із ще важчими покриває щонайменше min_share шляху."""
    totals = dict.fromkeys(CongestionLevel, 0.0)
    for level, weight in weighted:
        totals[level] += weight
    total = sum(totals.values())
    if total <= 0:
        return None
    covered = 0.0
    for level in reversed(CongestionLevel):
        covered += totals[level]
        if covered / total >= min_share:
            return level
    return CongestionLevel.FREE


def shape_offset_m(segment: RoadSegment, shape: Sequence[LatLon]) -> float | None:
    """Відстань від середини споруди до лінії провайдера, якщо лінія описує саме її
    проїзну частину (поруч і в тому ж напрямку біля середини); інакше None."""
    if len(shape) < 2:
        return None
    dist, a, b = nearest_segment(segment.mid, shape)
    if dist > MAX_OFFSET_M:
        return None
    if angle_diff_deg(bearing_deg(a, b), segment.bearing) > MAX_BEARING_DIFF_DEG:
        return None
    return dist


def worst_reading(readings: Iterable[ProbeReading | None]) -> ProbeReading | None:
    """Зведення показників споруд одного мосту: закрито, якщо закрита будь-яка;
    інакше найважча завантаженість. Споруди без даних пропускаються."""
    known = [r for r in readings if r is not None]
    if not known:
        return None
    if closed := next((r for r in known if r.closed), None):
        return closed
    rated = [r for r in known if r.congestion is not None]
    if not rated:
        return None
    return max(rated, key=lambda r: CONGESTION_SEVERITY[r.congestion])


def is_detour(probe: RoadProbe, route_length_m: float) -> bool:
    """Маршрут між кінцями мосту помітно довший за міст — маршрутизатор його об'їхав."""
    return route_length_m > probe.length_m * 1.5 + 300


class TrafficSource(Source):
    default_base_url: ClassVar[str]
    concurrency: ClassVar[int] = 4

    def __init__(
        self,
        source_id: str,
        kind: SourceKind,
        schedule: PollSchedule | timedelta,
        *,
        token: str,
        probes: Sequence[RoadProbe],
        base_url: str | None = None,
    ):
        super().__init__(source_id, kind, schedule)
        self.probes = tuple(probes)
        self._token = token
        self._http = HttpClient(source_id, base_url or self.default_base_url)

    @abstractmethod
    async def read(self, probe: RoadProbe) -> ProbeReading | None:
        """Показник для проїзної частини; None — провайдер не має надійних даних."""

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._http.request_json(method, path, **kwargs)

    async def close(self) -> None:
        await self._http.close()

    async def fetch(self) -> list[Report]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(probe: RoadProbe) -> ProbeReading | SourceError | None:
            async with semaphore:
                try:
                    return await self.read(probe)
                except SourceError as e:
                    return e

        results = await asyncio.gather(*(guarded(p) for p in self.probes))
        errors = [r for r in results if isinstance(r, SourceError)]
        if errors and len(errors) == len(results):
            raise SourceError(
                f"{self.source_id}: all probes failed; first: {errors[0]}"
            )
        for error in errors:
            log.warning("%s", error)

        now = datetime.now(UTC)
        claims = [
            self._claim_fields(probe, reading, now)
            for probe, reading in zip(self.probes, results)
            if isinstance(reading, ProbeReading)
            and (reading.closed or reading.congestion is not None)
        ]
        return [self.make_report(claims=claims)] if claims else []

    def _validity_interval(self, now: datetime) -> timedelta:
        # Опитування поза розкладом (наприклад, вручну) — за найдовшим інтервалом.
        return self.schedule.longest_interval_at(now) or self.schedule.max_interval

    def _claim_fields(
        self, probe: RoadProbe, reading: ProbeReading, now: datetime
    ) -> dict[str, Any]:
        return {
            "bridge_id": probe.bridge,
            "mode": TransportMode.ROAD,
            "directions": {probe.direction},
            "status": TrafficStatus.CLOSED if reading.closed else None,
            "congestion": None if reading.closed else reading.congestion,
            "observed_at": now,
            # За найдовшим інтервалом на цей момент: новіше твердження однаково
            # перекриє старе, а дані не зникнуть між рідшими опитуваннями.
            "valid_until": now + self._validity_interval(now) * VALID_FOR_POLLS,
        }
