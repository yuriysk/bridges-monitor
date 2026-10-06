"""Google Maps Platform — Routes API (computeRoutes).

Окремого API заторів Google не має, тому будується маршрут між кінцями
проїзної частини з TRAFFIC_AWARE і порівнюються duration (з урахуванням заторів)
і staticDuration (без них). Про закриття Google явно не повідомляє, тому
маршрут в об'їзд мосту дає «немає даних», а не «закрито».
"""

from typing import Any

from bridges_monitor.geometry import LatLon, RoadProbe
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.traffic.base import (
    ProbeReading,
    TrafficSource,
    congestion_from_speed_ratio,
    is_detour,
)

FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"


def _waypoint(point: LatLon, heading: float) -> dict[str, Any]:
    return {
        "location": {
            "latLng": {"latitude": point[0], "longitude": point[1]},
            "heading": round(heading) % 360,
        }
    }


def _seconds(value: str) -> float:
    if not value.endswith("s"):
        raise ValueError(f"bad duration {value!r}")
    return float(value[:-1])


def parse_google_route(payload: Any, probe: RoadProbe) -> ProbeReading | None:
    try:
        routes = payload.get("routes") or []
        if not routes:
            return None
        route = routes[0]
        distance = float(route["distanceMeters"])
        duration = _seconds(route["duration"])
        static = _seconds(route["staticDuration"])
    except (AttributeError, TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected Google Routes payload: {e!r}") from e
    if is_detour(probe, distance) or duration <= 0:
        return None
    free_flow_kmh = distance / static * 3.6 if static > 0 else None
    speed_kmh = distance / duration * 3.6
    return ProbeReading(
        # Швидкість обернено пропорційна часу: static/duration = поточна/вільна.
        congestion=congestion_from_speed_ratio(static, duration),
        speed_kmh=speed_kmh,
        free_flow_kmh=free_flow_kmh,
    )


class GoogleRoutesTrafficSource(TrafficSource):
    default_base_url = "https://routes.googleapis.com"

    async def read(self, probe: RoadProbe) -> ProbeReading | None:
        origin, destination = probe.route_endpoints
        payload = await self._request(
            "POST",
            "/directions/v2:computeRoutes",
            headers={"X-Goog-Api-Key": self._token, "X-Goog-FieldMask": FIELD_MASK},
            json={
                "origin": _waypoint(origin, probe.segments[0].bearing),
                "destination": _waypoint(destination, probe.segments[-1].bearing),
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
            },
        )
        return parse_google_route(payload, probe)
