"""HERE Traffic API v7 — flow.

GET /v7/flow?in=circle:lat,lng;r=м&locationReferencing=shape&apiKey=...
results[]: location.shape.links[].points[] {lat, lng} і currentFlow
{speed, freeFlow (м/с), jamFactor 0–10, traversability}.
Формат підтверджено на живих даних (Варшава); для України HERE даних не має.
"""

from typing import Any

from bridges_monitor.geometry import RoadProbe, RoadSegment
from bridges_monitor.models import CongestionLevel
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.traffic.base import (
    ProbeReading,
    TrafficSource,
    shape_offset_m,
    worst_reading,
)

SEARCH_RADIUS_M = 50


def congestion_from_jam_factor(jam_factor: float) -> CongestionLevel:
    if jam_factor < 4:
        return CongestionLevel.FREE
    if jam_factor < 7:
        return CongestionLevel.MODERATE
    if jam_factor < 9.5:
        return CongestionLevel.HEAVY
    return CongestionLevel.STANDSTILL


def parse_here_flow(payload: Any, segment: RoadSegment) -> ProbeReading | None:
    best: tuple[float, dict[str, Any]] | None = None
    try:
        for result in payload["results"]:
            shape = [
                (float(p["lat"]), float(p["lng"]))
                for link in result["location"]["shape"]["links"]
                for p in link["points"]
            ]
            offset = shape_offset_m(segment, shape)
            if offset is not None and (best is None or offset < best[0]):
                best = (offset, result["currentFlow"])
        if best is None:
            return None
        flow = best[1]
        closed = flow.get("traversability") == "closed"
        jam_factor = float(flow["jamFactor"])
        speed = flow.get("speed")
        free_flow = flow.get("freeFlow")
    except (TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected HERE payload: {e!r}") from e
    return ProbeReading(
        closed=closed,
        congestion=None if closed else congestion_from_jam_factor(jam_factor),
        speed_kmh=None if speed is None else float(speed) * 3.6,
        free_flow_kmh=None if free_flow is None else float(free_flow) * 3.6,
    )


class HereTrafficSource(TrafficSource):
    default_base_url = "https://data.traffic.hereapi.com"

    async def read(self, probe: RoadProbe) -> ProbeReading | None:
        return worst_reading([await self._read_segment(s) for s in probe.segments])

    async def _read_segment(self, segment: RoadSegment) -> ProbeReading | None:
        lat, lon = segment.mid
        payload = await self._request(
            "GET",
            "/v7/flow",
            params={
                "in": f"circle:{lat},{lon};r={SEARCH_RADIUS_M}",
                "locationReferencing": "shape",
                "apiKey": self._token,
            },
        )
        return parse_here_flow(payload, segment)
