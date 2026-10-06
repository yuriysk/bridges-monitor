from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from bridges_monitor.catalog import Direction, load_catalog
from bridges_monitor.config import (
    Edition,
    Settings,
    TrafficProvider,
    TrafficSourceSettings,
)
from bridges_monitor.geometry import RoadProbe, distance_m, load_road_probes
from bridges_monitor.models import CongestionLevel, SourceKind, TrafficStatus
from bridges_monitor.schedule import DEFAULT_TRAFFIC_SCHEDULE, PollSchedule
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.http import api_error_detail
from bridges_monitor.sources.traffic import build_traffic_sources
from bridges_monitor.sources.traffic.base import (
    ProbeReading,
    TrafficSource,
    congestion_from_speed_ratio,
    dominant_congestion,
    shape_offset_m,
    worst_reading,
)
from bridges_monitor.sources.traffic.google import (
    GoogleRoutesTrafficSource,
    parse_google_route,
)
from bridges_monitor.sources.traffic.here import HereTrafficSource, parse_here_flow
from bridges_monitor.sources.traffic.mapbox import (
    MapboxTrafficSource,
    parse_mapbox_route,
)
from bridges_monitor.sources.traffic.tomtom import (
    TomTomTrafficSource,
    parse_tomtom_flow,
)

CATALOG = load_catalog()
PROBES = load_road_probes(CATALOG)
PATON_LEFT = next(
    p
    for p in PROBES
    if p.bridge == "bridge-paton" and p.direction is Direction.TO_LEFT_BANK
)
PATON_RIGHT = next(
    p
    for p in PROBES
    if p.bridge == "bridge-paton" and p.direction is Direction.TO_RIGHT_BANK
)
METRO_LEFT = next(
    p
    for p in PROBES
    if p.bridge == "bridge-metro" and p.direction is Direction.TO_LEFT_BANK
)
(PATON_SEG,) = PATON_LEFT.segments
SAME = list(PATON_SEG.points)
OPPOSITE = list(PATON_RIGHT.segments[0].points)
EXTENDED = Edition.EXTENDED


# --- геометрія ---


def test_every_bridge_has_both_directions():
    covered = {(p.bridge, p.direction) for p in PROBES}
    expected = {(b.id, d) for b in CATALOG.bridges for d in Direction}
    assert covered == expected
    assert len(PROBES) == 12


def test_multi_structure_bridges():
    assert len(METRO_LEFT.segments) == 2
    first, second = METRO_LEFT.segments
    # Довжина = обидві споруди + дорога між ними через Гідропарк.
    gap = distance_m(first.end, second.start)
    assert METRO_LEFT.length_m == pytest.approx(first.length_m + gap + second.length_m)


@pytest.mark.parametrize(
    ("points", "route_points"),
    [
        (OPPOSITE, PATON_LEFT.route_points),
        (SAME, PATON_LEFT.route_points[::-1]),
    ],
)
def test_probe_rejects_reversed_geometry(points, route_points):
    with pytest.raises(ValidationError, match="wrong way"):
        RoadProbe(
            bridge="x",
            direction=Direction.TO_LEFT_BANK,
            segments=[{"osm_ways": (1,), "points": tuple(points)}],
            route_points=route_points,
        )


def test_route_points_lie_inside_first_and_last_structure():
    for probe in PROBES:
        a, b = probe.route_points
        first, last = probe.segments[0], probe.segments[-1]
        assert distance_m(first.start, a) == pytest.approx(
            first.length_m * 0.2, rel=0.3
        )
        assert distance_m(b, last.end) == pytest.approx(last.length_m * 0.2, rel=0.3)


def test_shape_matching_checks_direction_and_distance():
    assert shape_offset_m(PATON_SEG, SAME) == pytest.approx(0, abs=1)
    assert shape_offset_m(PATON_SEG, OPPOSITE) is None
    far = [(lat + 0.01, lon) for lat, lon in SAME]
    assert shape_offset_m(PATON_SEG, far) is None


# --- рівні завантаженості ---


@pytest.mark.parametrize(
    ("current", "free", "level"),
    [
        (60, 60, CongestionLevel.FREE),
        (35, 60, CongestionLevel.MODERATE),
        (20, 60, CongestionLevel.HEAVY),
        (5, 60, CongestionLevel.STANDSTILL),
        (5, 0, CongestionLevel.STANDSTILL),
    ],
)
def test_congestion_from_speed_ratio(current, free, level):
    assert congestion_from_speed_ratio(current, free) is level


def test_dominant_congestion():
    free, heavy = CongestionLevel.FREE, CongestionLevel.HEAVY
    assert dominant_congestion([(free, 80), (heavy, 20)]) is free
    assert dominant_congestion([(free, 60), (heavy, 40)]) is heavy
    assert dominant_congestion([]) is None


# --- розбір відповідей провайдерів ---


def tomtom_payload(shape, speed=20, free=60, closure=False):
    return {
        "flowSegmentData": {
            "frc": "FRC1",
            "currentSpeed": speed,
            "freeFlowSpeed": free,
            "confidence": 0.9,
            "roadClosure": closure,
            "coordinates": {
                "coordinate": [{"latitude": a, "longitude": b} for a, b in shape]
            },
        }
    }


def test_tomtom_parse():
    reading = parse_tomtom_flow(tomtom_payload(SAME), PATON_SEG)
    assert reading.congestion is CongestionLevel.HEAVY
    assert reading.speed_kmh == 20
    assert parse_tomtom_flow(tomtom_payload(OPPOSITE), PATON_SEG) is None
    closed = parse_tomtom_flow(tomtom_payload(SAME, closure=True), PATON_SEG)
    assert closed.closed and closed.congestion is None


def here_result(shape, jam, traversability="open"):
    return {
        "location": {
            "description": "міст Патона",
            "shape": {"links": [{"points": [{"lat": a, "lng": b} for a, b in shape]}]},
        },
        "currentFlow": {
            "speed": 5.0,
            "freeFlow": 16.0,
            "jamFactor": jam,
            "confidence": 0.9,
            "traversability": traversability,
        },
    }


def test_here_parse_picks_matching_direction():
    payload = {"results": [here_result(OPPOSITE, 1.0), here_result(SAME, 7.5)]}
    reading = parse_here_flow(payload, PATON_SEG)
    assert reading.congestion is CongestionLevel.HEAVY
    assert reading.speed_kmh == pytest.approx(18)
    assert parse_here_flow({"results": []}, PATON_SEG) is None
    closed = {"results": [here_result(SAME, 10, traversability="closed")]}
    assert parse_here_flow(closed, PATON_SEG).closed


def test_google_parse():
    payload = {
        "routes": [
            {"distanceMeters": 1400, "duration": "200s", "staticDuration": "100s"}
        ]
    }
    reading = parse_google_route(payload, PATON_LEFT)
    assert reading.congestion is CongestionLevel.MODERATE
    detour = {
        "routes": [
            {"distanceMeters": 9000, "duration": "900s", "staticDuration": "600s"}
        ]
    }
    assert parse_google_route(detour, PATON_LEFT) is None
    assert parse_google_route({}, PATON_LEFT) is None


def mapbox_payload(congestion, distances, closures=(), total=1400):
    return {
        "routes": [
            {
                "distance": total,
                "legs": [
                    {
                        "annotation": {"congestion": congestion, "distance": distances},
                        "closures": list(closures),
                    }
                ],
            }
        ]
    }


def test_mapbox_parse():
    payload = mapbox_payload(["low", "heavy", "unknown"], [700, 600, 100])
    assert parse_mapbox_route(payload, PATON_LEFT).congestion is CongestionLevel.HEAVY
    closed = mapbox_payload(
        ["low"], [1400], closures=[{"geometry_index_start": 0, "geometry_index_end": 1}]
    )
    assert parse_mapbox_route(closed, PATON_LEFT).closed
    detour = mapbox_payload(["low"], [9000], total=9000)
    assert parse_mapbox_route(detour, PATON_LEFT) is None
    unknown = mapbox_payload(["unknown"], [1400])
    assert parse_mapbox_route(unknown, PATON_LEFT) is None


@pytest.mark.parametrize(
    ("parse", "target"),
    [
        (parse_tomtom_flow, PATON_SEG),
        (parse_here_flow, PATON_SEG),
        (parse_google_route, PATON_LEFT),
        (parse_mapbox_route, PATON_LEFT),
    ],
)
def test_unexpected_payload_raises_source_error(parse, target):
    with pytest.raises(SourceError):
        parse({"routes": [{}], "results": [{}], "flowSegmentData": {}}, target)


def test_worst_reading():
    free = ProbeReading(congestion=CongestionLevel.FREE)
    heavy = ProbeReading(congestion=CongestionLevel.HEAVY)
    closed = ProbeReading(closed=True)
    assert worst_reading([free, None, heavy]) is heavy
    assert worst_reading([heavy, closed]) is closed
    assert worst_reading([None, None]) is None


# --- базовий клас: твердження та збої ---


class FakeTraffic(TrafficSource):
    default_base_url = "http://unused"

    def __init__(self, readings, schedule=timedelta(minutes=10), **kwargs):
        super().__init__(
            "fake",
            SourceKind.UNOFFICIAL,
            schedule,
            token="t",
            probes=[PATON_LEFT, PATON_RIGHT],
            **kwargs,
        )
        self.readings = readings

    async def read(self, probe):
        result = self.readings[probe.direction]
        if isinstance(result, Exception):
            raise result
        return result


async def test_fetch_builds_claims_per_direction():
    src = FakeTraffic(
        {
            Direction.TO_LEFT_BANK: ProbeReading(congestion=CongestionLevel.HEAVY),
            Direction.TO_RIGHT_BANK: ProbeReading(closed=True),
        }
    )
    (report,) = await src.fetch()
    left, right = report.claims
    assert left.bridge_id == "bridge-paton"
    assert left.directions == {Direction.TO_LEFT_BANK}
    assert (left.status, left.congestion) == (None, CongestionLevel.HEAVY)
    assert left.valid_until - left.observed_at == timedelta(minutes=30)
    assert (right.status, right.congestion) == (TrafficStatus.CLOSED, None)


async def test_claim_validity_follows_schedule():
    src = FakeTraffic(
        dict.fromkeys(Direction, ProbeReading(congestion=CongestionLevel.FREE)),
        schedule=DEFAULT_TRAFFIC_SCHEDULE,
    )
    (report,) = await src.fetch()
    claim = report.claims[0]
    expected = DEFAULT_TRAFFIC_SCHEDULE.longest_interval_at(claim.observed_at) * 3
    assert claim.valid_until - claim.observed_at == expected


async def test_claim_validity_ignores_paused_mode():
    paused_now = PollSchedule(
        normal=[{"start": "00:00"}],
        alert=[{"start": "00:00", "interval": "PT30M"}],
    )
    src = FakeTraffic(
        dict.fromkeys(Direction, ProbeReading(congestion=CongestionLevel.FREE)),
        schedule=paused_now,
    )
    (report,) = await src.fetch()
    claim = report.claims[0]
    assert claim.valid_until - claim.observed_at == timedelta(minutes=90)


async def test_fetch_tolerates_partial_failures():
    src = FakeTraffic(
        {
            Direction.TO_LEFT_BANK: ProbeReading(congestion=CongestionLevel.FREE),
            Direction.TO_RIGHT_BANK: SourceError("boom"),
        }
    )
    (report,) = await src.fetch()
    assert len(report.claims) == 1


async def test_fetch_without_data_returns_nothing_and_all_failures_raise():
    assert await FakeTraffic(dict.fromkeys(Direction)).fetch() == []
    failing = FakeTraffic(dict.fromkeys(Direction, SourceError("boom")))
    with pytest.raises(SourceError, match="all probes failed"):
        await failing.fetch()


# --- HTTP: куди передається ключ, і що він не потрапляє в помилки ---


async def _serve(method, path, payload, status=200):
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["query"] = dict(request.query)
        seen["headers"] = dict(request.headers)
        seen["path"] = request.path
        if request.can_read_body:
            seen["json"] = await request.json()
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_route(method, path, handler)
    server = TestServer(app)
    await server.start_server()
    return server, seen


def _source(cls, server):
    return cls(
        "t",
        SourceKind.UNOFFICIAL,
        timedelta(minutes=10),
        token="SECRET-KEY-123",
        probes=[PATON_LEFT],
        base_url=str(server.make_url("/")),
    )


async def test_tomtom_request():
    server, seen = await _serve(
        "GET",
        "/traffic/services/4/flowSegmentData/absolute/16/json",
        tomtom_payload(SAME),
    )
    try:
        async with _source(TomTomTrafficSource, server) as src:
            reading = await src.read(PATON_LEFT)
    finally:
        await server.close()
    assert reading.congestion is CongestionLevel.HEAVY
    assert seen["query"]["key"] == "SECRET-KEY-123"
    assert seen["query"]["point"] == f"{PATON_SEG.mid[0]},{PATON_SEG.mid[1]}"


async def test_here_request():
    server, seen = await _serve(
        "GET", "/v7/flow", {"results": [here_result(SAME, 2.0)]}
    )
    try:
        async with _source(HereTrafficSource, server) as src:
            reading = await src.read(PATON_LEFT)
    finally:
        await server.close()
    assert reading.congestion is CongestionLevel.FREE
    assert seen["query"]["apiKey"] == "SECRET-KEY-123"
    assert seen["query"]["in"].startswith("circle:")


async def test_google_request():
    payload = {
        "routes": [
            {"distanceMeters": 1400, "duration": "100s", "staticDuration": "100s"}
        ]
    }
    server, seen = await _serve("POST", "/directions/v2:computeRoutes", payload)
    try:
        async with _source(GoogleRoutesTrafficSource, server) as src:
            reading = await src.read(PATON_LEFT)
    finally:
        await server.close()
    assert reading.congestion is CongestionLevel.FREE
    assert seen["headers"]["X-Goog-Api-Key"] == "SECRET-KEY-123"
    assert seen["json"]["routingPreference"] == "TRAFFIC_AWARE"
    assert seen["json"]["origin"]["location"]["heading"] == round(PATON_SEG.bearing)


async def test_mapbox_request():
    server, seen = await _serve(
        "GET",
        "/directions/v5/mapbox/driving-traffic/{coords}",
        mapbox_payload(["low"], [1400]),
    )
    try:
        async with _source(MapboxTrafficSource, server) as src:
            reading = await src.read(PATON_LEFT)
    finally:
        await server.close()
    assert reading.congestion is CongestionLevel.FREE
    assert seen["query"]["access_token"] == "SECRET-KEY-123"
    lon1 = seen["path"].rsplit("/", 1)[1].split(",")[0]
    assert float(lon1) == pytest.approx(PATON_LEFT.route_endpoints[0][1])


async def test_http_error_does_not_leak_token():
    server, _ = await _serve(
        "GET",
        "/traffic/services/4/flowSegmentData/absolute/16/json",
        {"error": "Point too far from nearest existing segment. key=SECRET-KEY-123"},
        status=400,
    )
    try:
        async with _source(TomTomTrafficSource, server) as src:
            with pytest.raises(SourceError) as exc:
                await src.read(PATON_LEFT)
    finally:
        await server.close()
    assert "HTTP 400" in str(exc.value)
    assert "Point too far" in str(exc.value)
    assert "SECRET-KEY-123" not in str(exc.value)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"error": "Point too far"}, "Point too far"),
        ({"error": {"code": 403, "message": "API key invalid"}}, "API key invalid"),
        ({"title": "Unauthorized", "status": 401}, "Unauthorized"),
        (
            {"message": "Not Authorized - Invalid Token"},
            "Not Authorized - Invalid Token",
        ),
        (["unexpected"], None),
    ],
)
def test_api_error_detail(body, detail):
    assert api_error_detail(body) == detail


async def test_point_provider_reads_every_structure_and_takes_worst():
    """Міст Метро: перша споруда вільна, Русанівський метроміст — затор."""
    first, second = METRO_LEFT.segments
    queried = []

    async def handler(request: web.Request) -> web.Response:
        lat, lon = map(float, request.query["point"].split(","))
        queried.append((lat, lon))
        segment = first if (lat, lon) == first.mid else second
        speed = 60 if segment is first else 10
        return web.json_response(tomtom_payload(list(segment.points), speed=speed))

    app = web.Application()
    app.router.add_get("/traffic/services/4/flowSegmentData/absolute/16/json", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with _source(TomTomTrafficSource, server) as src:
            reading = await src.read(METRO_LEFT)
    finally:
        await server.close()
    assert queried == [first.mid, second.mid]
    assert reading.congestion is CongestionLevel.STANDSTILL


# --- конфіг ---


def test_basic_edition_rejects_unofficial_traffic():
    traffic = {TrafficProvider.TOMTOM: TrafficSourceSettings(token="t")}
    with pytest.raises(ValidationError, match="unofficial"):
        Settings(edition=Edition.BASIC, traffic=traffic)
    disabled = {TrafficProvider.TOMTOM: TrafficSourceSettings(enabled=False)}
    assert Settings(edition=Edition.BASIC, traffic=disabled)


def test_build_traffic_sources():
    settings = Settings(
        edition=EXTENDED,
        traffic={
            TrafficProvider.TOMTOM: TrafficSourceSettings(token="a"),
            TrafficProvider.HERE: TrafficSourceSettings(token="b", enabled=False),
        },
    )
    (src,) = build_traffic_sources(settings, PROBES)
    assert isinstance(src, TomTomTrafficSource)
    assert len(src.probes) == 12
    assert src.schedule == DEFAULT_TRAFFIC_SCHEDULE

    missing = Settings(
        edition=EXTENDED, traffic={TrafficProvider.MAPBOX: TrafficSourceSettings()}
    )
    with pytest.raises(ValueError, match="BM_TRAFFIC__MAPBOX__TOKEN"):
        build_traffic_sources(missing, PROBES)


def test_env_token(monkeypatch):
    monkeypatch.setenv("BM_EDITION", "extended")
    monkeypatch.setenv("BM_TRAFFIC__TOMTOM__TOKEN", "from-env")
    settings = Settings()
    token = settings.traffic[TrafficProvider.TOMTOM].token
    assert token.get_secret_value() == "from-env"
