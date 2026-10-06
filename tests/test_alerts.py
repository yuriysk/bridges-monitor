from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from bridges_monitor.alerts import AlertTracker
from bridges_monitor.config import AlertProvider, AlertSettings, Edition, Settings
from bridges_monitor.models import AlertSnapshot, SourceKind
from bridges_monitor.sources.alerts import (
    AlertsInUaSource,
    UkraineAlarmSource,
    build_alert_source,
    parse_alerts_in_ua,
    parse_ukrainealarm,
)
from bridges_monitor.sources.base import SourceError

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

UKRAINEALARM_ACTIVE = [
    {
        "regionId": "31",
        "regionType": "State",
        "regionName": "м. Київ",
        "activeAlerts": [
            {
                "regionId": "31",
                "regionType": "State",
                "type": "AIR",
                "lastUpdate": "2026-10-05T11:40:00Z",
            }
        ],
    }
]
UKRAINEALARM_QUIET = [{"regionId": "31", "regionName": "м. Київ", "activeAlerts": []}]

ALERTS_IN_UA_ACTIVE = {
    "alerts": [
        {
            "id": 1,
            "location_title": "м. Київ",
            "location_type": "oblast",  # так у живих даних, хоча документація каже city
            "location_uid": "31",
            "alert_type": "air_raid",
            "started_at": "2026-10-05T11:40:00.000Z",
            "finished_at": None,
        },
        {
            "location_uid": "14",
            "alert_type": "air_raid",
            "started_at": "2026-10-05T10:00:00.000Z",
        },
    ]
}


def test_parse_ukrainealarm():
    snap = parse_ukrainealarm(UKRAINEALARM_ACTIVE, "31", NOW)
    assert snap.active
    assert snap.started_at == datetime(2026, 10, 5, 11, 40, tzinfo=UTC)
    assert not parse_ukrainealarm(UKRAINEALARM_QUIET, "31", NOW).active


def test_parse_ukrainealarm_ignores_non_air_alerts():
    payload = [{"regionId": "31", "activeAlerts": [{"type": "ARTILLERY"}]}]
    assert not parse_ukrainealarm(payload, "31", NOW).active


def test_parse_alerts_in_ua_filters_region():
    snap = parse_alerts_in_ua(ALERTS_IN_UA_ACTIVE, "31", NOW)
    assert snap.active
    assert snap.started_at == datetime(2026, 10, 5, 11, 40, tzinfo=UTC)
    assert not parse_alerts_in_ua({"alerts": []}, "31", NOW).active


@pytest.mark.parametrize("parse", [parse_ukrainealarm, parse_alerts_in_ua])
def test_unexpected_payload_raises_source_error(parse):
    with pytest.raises(SourceError):
        parse({"unexpected": True}, "31", NOW)


async def _serve(path: str, payload, status: int = 200):
    seen_headers = {}

    async def handler(request: web.Request) -> web.Response:
        seen_headers.update(request.headers)
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_get(path, handler)
    server = TestServer(app)
    await server.start_server()
    return server, seen_headers


async def test_ukrainealarm_fetch_sends_key():
    server, headers = await _serve("/api/v3/alerts/31", UKRAINEALARM_ACTIVE)
    try:
        async with UkraineAlarmSource(
            "ua",
            SourceKind.OFFICIAL,
            timedelta(seconds=30),
            token="secret",
            base_url=str(server.make_url("/")),
        ) as src:
            snap = await src.fetch()
    finally:
        await server.close()
    assert snap.active
    assert headers["Authorization"] == "secret"


async def test_alerts_in_ua_fetch_sends_bearer():
    server, headers = await _serve("/v1/alerts/active.json", ALERTS_IN_UA_ACTIVE)
    try:
        async with AlertsInUaSource(
            "aiu",
            SourceKind.UNOFFICIAL,
            timedelta(seconds=30),
            token="secret",
            base_url=str(server.make_url("/")),
        ) as src:
            snap = await src.fetch()
    finally:
        await server.close()
    assert snap.active
    assert headers["Authorization"] == "Bearer secret"


async def test_http_error_raises_source_error():
    server, _ = await _serve("/api/v3/alerts/31", {"error": "x"}, status=401)
    try:
        async with UkraineAlarmSource(
            "ua",
            SourceKind.OFFICIAL,
            timedelta(seconds=30),
            token="bad",
            base_url=str(server.make_url("/")),
        ) as src:
            with pytest.raises(SourceError):
                await src.fetch()
    finally:
        await server.close()


def test_build_alert_source():
    settings = Settings(alerts=AlertSettings(token="t"))
    src = build_alert_source(settings)
    assert isinstance(src, UkraineAlarmSource)
    assert src.kind is SourceKind.OFFICIAL
    with pytest.raises(ValueError, match="token"):
        build_alert_source(Settings())


def test_basic_edition_rejects_unofficial_alert_provider():
    unofficial = AlertSettings(provider=AlertProvider.ALERTS_IN_UA)
    with pytest.raises(ValidationError, match="unofficial"):
        Settings(edition=Edition.BASIC, alerts=unofficial)
    assert Settings(edition=Edition.EXTENDED, alerts=unofficial)


def snap(active: bool, minutes: int, started_minutes: int | None = None):
    started = (
        None if started_minutes is None else NOW + timedelta(minutes=started_minutes)
    )
    return AlertSnapshot(
        active=active, started_at=started, checked_at=NOW + timedelta(minutes=minutes)
    )


def test_tracker_opens_and_closes_period():
    tracker = AlertTracker()
    assert tracker.update(snap(False, 0)) is None

    opened = tracker.update(snap(True, 1, started_minutes=0))
    assert opened.started_at == NOW and opened.ended_at is None
    assert tracker.update(snap(True, 2)) is None
    assert tracker.current == opened

    closed = tracker.update(snap(False, 30))
    assert closed.ended_at == NOW + timedelta(minutes=30)
    assert tracker.current is None
    assert tracker.periods == [closed]


def test_tracker_without_start_uses_check_time_and_avoids_overlap():
    tracker = AlertTracker()
    tracker.update(snap(True, 0))
    tracker.update(snap(False, 10))
    reopened = tracker.update(snap(True, 20, started_minutes=5))
    assert reopened.started_at == NOW + timedelta(minutes=10)
    assert len(tracker.periods) == 2
