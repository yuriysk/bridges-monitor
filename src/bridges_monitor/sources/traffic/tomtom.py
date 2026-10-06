"""TomTom Traffic API — Flow Segment Data v4.

GET /traffic/services/4/flowSegmentData/absolute/{zoom}/json?point=lat,lon&key=...
Повертає дані про фрагмент дороги, найближчий до точки: currentSpeed,
freeFlowSpeed, confidence, roadClosure і геометрію coordinates.coordinate[].
"""

from typing import Any

from bridges_monitor.geometry import RoadProbe, RoadSegment
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.traffic.base import (
    ProbeReading,
    TrafficSource,
    congestion_from_speed_ratio,
    shape_offset_m,
    worst_reading,
)

ZOOM = 16


def parse_tomtom_flow(payload: Any, segment: RoadSegment) -> ProbeReading | None:
    try:
        data = payload["flowSegmentData"]
        shape = [
            (float(c["latitude"]), float(c["longitude"]))
            for c in data["coordinates"]["coordinate"]
        ]
        closed = bool(data.get("roadClosure", False))
        speed = float(data["currentSpeed"])
        free_flow = float(data["freeFlowSpeed"])
    except (TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected TomTom payload: {e!r}") from e
    # Точку прив'язано до іншої проїзної частини (зустрічної чи сусідньої дороги).
    if shape_offset_m(segment, shape) is None:
        return None
    return ProbeReading(
        closed=closed,
        congestion=None if closed else congestion_from_speed_ratio(speed, free_flow),
        speed_kmh=speed,
        free_flow_kmh=free_flow,
    )


class TomTomTrafficSource(TrafficSource):
    default_base_url = "https://api.tomtom.com"

    async def read(self, probe: RoadProbe) -> ProbeReading | None:
        return worst_reading([await self._read_segment(s) for s in probe.segments])

    async def _read_segment(self, segment: RoadSegment) -> ProbeReading | None:
        lat, lon = segment.mid
        payload = await self._request(
            "GET",
            f"/traffic/services/4/flowSegmentData/absolute/{ZOOM}/json",
            params={"point": f"{lat},{lon}", "unit": "kmph", "key": self._token},
        )
        return parse_tomtom_flow(payload, segment)
