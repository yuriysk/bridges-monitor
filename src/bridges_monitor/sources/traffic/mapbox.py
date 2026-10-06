"""Mapbox Directions API, профіль driving-traffic.

Маршрут між кінцями проїзної частини з annotations=congestion,distance,closure.
Завантаженість — домінантний рівень annotation.congestion з вагою за довжиною
відрізків; закриття — непорожній legs[].closures. Маршрут в об'їзд мосту дає
«немає даних».
"""

from typing import Any

from bridges_monitor.geometry import RoadProbe
from bridges_monitor.models import CongestionLevel
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.traffic.base import (
    ProbeReading,
    TrafficSource,
    dominant_congestion,
    is_detour,
)

BEARING_RANGE_DEG = 45
_LEVELS = {
    "low": CongestionLevel.FREE,
    "moderate": CongestionLevel.MODERATE,
    "heavy": CongestionLevel.HEAVY,
    "severe": CongestionLevel.STANDSTILL,
}


def parse_mapbox_route(payload: Any, probe: RoadProbe) -> ProbeReading | None:
    try:
        routes = payload.get("routes") or []
        if not routes:
            return None
        route = routes[0]
        distance = float(route["distance"])
        legs = route["legs"]
        closed = any(leg.get("closures") for leg in legs)
        weighted = [
            (_LEVELS[level], float(length))
            for leg in legs
            for level, length in zip(
                leg["annotation"]["congestion"], leg["annotation"]["distance"]
            )
            if level in _LEVELS
        ]
    except (AttributeError, TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected Mapbox payload: {e!r}") from e
    if is_detour(probe, distance):
        return None
    congestion = None if closed else dominant_congestion(weighted)
    if not closed and congestion is None:
        return None
    return ProbeReading(closed=closed, congestion=congestion)


class MapboxTrafficSource(TrafficSource):
    default_base_url = "https://api.mapbox.com"

    async def read(self, probe: RoadProbe) -> ProbeReading | None:
        (lat1, lon1), (lat2, lon2) = probe.route_endpoints
        first, last = (
            f"{round(s.bearing) % 360},{BEARING_RANGE_DEG}"
            for s in (probe.segments[0], probe.segments[-1])
        )
        payload = await self._request(
            "GET",
            f"/directions/v5/mapbox/driving-traffic/{lon1},{lat1};{lon2},{lat2}",
            params={
                "annotations": "congestion,distance,closure",
                "overview": "full",
                "bearings": f"{first};{last}",
                "access_token": self._token,
            },
        )
        return parse_mapbox_route(payload, probe)
