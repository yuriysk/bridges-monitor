import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bridges_monitor.catalog import Direction, TransportMode, load_catalog
from bridges_monitor.config import Edition, Settings
from bridges_monitor.models import (
    AlertSnapshot,
    CongestionLevel,
    ModerationStatus,
    Report,
    SourceKind,
    StateKey,
    TrafficStatus,
)
from bridges_monitor.monitor import Monitor, plan_poll
from bridges_monitor.schedule import DEFAULT_TRAFFIC_SCHEDULE, PollSchedule
from bridges_monitor.sources.alerts import AlertSource
from bridges_monitor.sources.base import Source, SourceError

KYIV = ZoneInfo("Europe/Kyiv")
CATALOG = load_catalog()
PATON_LEFT = StateKey("bridge-paton", TransportMode.ROAD, Direction.TO_LEFT_BANK)
TICK = timedelta(milliseconds=20)

ALERT_ONLY = PollSchedule(
    normal=[{"start": "00:00"}],
    alert=[
        {"start": "00:00"},
        {"start": "08:00", "interval": "PT30M"},
        {"start": "20:00"},
    ],
)


def kyiv(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 6, hour, minute, tzinfo=KYIV)


# --- plan_poll ---


def test_first_poll_is_immediate():
    plan = plan_poll(DEFAULT_TRAFFIC_SCHEDULE, None, kyiv(9), alert_active=False)
    assert plan.poll_now


def test_waits_for_interval():
    plan = plan_poll(DEFAULT_TRAFFIC_SCHEDULE, kyiv(9), kyiv(9, 4), alert_active=False)
    assert not plan.poll_now
    assert plan.wake_at == kyiv(9, 10)


def test_alert_shortens_interval():
    # Без тривоги о 09:00 — кожні 10 хв, з тривогою — кожні 5.
    assert plan_poll(
        DEFAULT_TRAFFIC_SCHEDULE, kyiv(9), kyiv(9, 6), alert_active=True
    ).poll_now


def test_window_boundary_wakes_earlier():
    # 01:55 + 20 хв = 02:15, але о 02:00 починається вікно з іншим інтервалом.
    plan = plan_poll(
        DEFAULT_TRAFFIC_SCHEDULE, kyiv(1, 55), kyiv(1, 56), alert_active=False
    )
    assert plan.wake_at == kyiv(2)


def test_paused_source_sleeps_until_next_boundary():
    plan = plan_poll(ALERT_ONLY, None, kyiv(7), alert_active=True)
    assert not plan.poll_now
    assert plan.wake_at == kyiv(8)
    assert plan_poll(ALERT_ONLY, None, kyiv(9), alert_active=False).wake_at == kyiv(20)
    assert plan_poll(ALERT_ONLY, None, kyiv(9), alert_active=True).poll_now


# --- монітор ---


class Recorder:
    def __init__(self):
        self.changes = []
        self.alerts = []

    async def publish_changes(self, changes):
        self.changes.extend(changes)

    async def publish_alert(self, period):
        self.alerts.append(period)


class FakeTraffic(Source):
    def __init__(self, schedule=TICK, kind=SourceKind.OFFICIAL, results=None):
        super().__init__("fake-traffic", kind, schedule)
        self.calls = 0
        # Черга результатів: Exception — кинути, інакше рівень завантаженості.
        self.results = list(results or [])

    async def fetch(self):
        self.calls += 1
        result = self.results.pop(0) if self.results else CongestionLevel.HEAVY
        if isinstance(result, Exception):
            raise result
        now = datetime.now(UTC)
        return [
            self.make_report(
                claims=[
                    {
                        "bridge_id": "bridge-paton",
                        "mode": TransportMode.ROAD,
                        "directions": {Direction.TO_LEFT_BANK},
                        "congestion": result,
                        "observed_at": now,
                        "valid_until": now + timedelta(minutes=30),
                    }
                ]
            )
        ]


class FakeAlerts(AlertSource):
    default_base_url = "http://unused"

    def __init__(self, active_from_call: int | None):
        super().__init__("fake-alerts", SourceKind.OFFICIAL, TICK, token="t")
        self.active_from_call = active_from_call
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        active = (
            self.active_from_call is not None and self.calls >= self.active_from_call
        )
        return AlertSnapshot(active=active, checked_at=datetime.now(UTC))


async def run_for(monitor: Monitor, seconds: float) -> None:
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def make_monitor(sources, *, alerts=None, edition=Edition.BASIC, **kwargs):
    recorder = Recorder()
    monitor = Monitor(
        Settings(edition=edition),
        CATALOG,
        alert_source=alerts,
        sources=sources,
        publisher=recorder,
        **kwargs,
    )
    return monitor, recorder


async def test_publishes_traffic_changes_but_not_baseline():
    source = FakeTraffic()
    monitor, recorder = make_monitor([source])
    await run_for(monitor, 0.15)

    assert source.calls >= 3
    (change,) = recorder.changes  # повторні однакові дані змін не дають
    assert change.key == PATON_LEFT
    assert change.before.status is TrafficStatus.UNKNOWN
    assert change.after.congestion is CongestionLevel.HEAVY
    assert monitor.states[PATON_LEFT].congestion is CongestionLevel.HEAVY


async def test_alert_start_wakes_source_paused_until_alert():
    paused_without_alert = PollSchedule(
        normal=[{"start": "00:00"}],
        alert=[{"start": "00:00", "interval": "PT1H"}],
    )
    source = FakeTraffic(schedule=paused_without_alert)
    alerts = FakeAlerts(active_from_call=4)
    monitor, recorder = make_monitor([source], alerts=alerts)
    await run_for(monitor, 0.2)

    assert alerts.calls >= 4
    assert monitor.alert_active
    (period,) = recorder.alerts
    assert period.ended_at is None
    # Опитано рівно раз: одразу після початку тривоги, далі — лише через годину.
    assert source.calls == 1


async def test_source_errors_do_not_stop_polling():
    source = FakeTraffic(results=[SourceError("boom"), SourceError("boom")])
    monitor, recorder = make_monitor([source])
    await run_for(monitor, 0.15)
    assert source.calls >= 3
    assert recorder.changes


async def test_extended_edition_keeps_claims_pending():
    source = FakeTraffic(kind=SourceKind.UNOFFICIAL)
    monitor, recorder = make_monitor([source], edition=Edition.EXTENDED)
    await run_for(monitor, 0.1)
    assert monitor.claims
    assert {c.moderation for c in monitor.claims} == {ModerationStatus.PENDING}
    assert recorder.changes == []


def test_basic_edition_drops_unofficial_reports():
    monitor, _ = make_monitor([])
    unofficial = FakeTraffic(kind=SourceKind.UNOFFICIAL)
    reports: list[Report] = asyncio.run(unofficial.fetch())
    monitor.ingest(reports)
    assert monitor.claims == []


async def test_expired_claims_become_unknown_and_are_pruned():
    reports = await FakeTraffic().fetch()
    # «Зараз» — після отримання даних, інакше твердження було б «з майбутнього».
    now = [datetime.now(UTC)]
    monitor, recorder = make_monitor([], clock=lambda: now[0])
    monitor.states = monitor._compute(now[0])
    monitor.ingest(reports)
    await monitor.recompute()
    assert monitor.states[PATON_LEFT].congestion is CongestionLevel.HEAVY

    now[0] += timedelta(hours=1)
    await monitor.recompute()
    assert monitor.states[PATON_LEFT].congestion is None
    assert recorder.changes[-1].after.status is TrafficStatus.UNKNOWN
    assert monitor.claims == []


async def test_publisher_failure_does_not_stop_monitor(caplog):
    class Broken(Recorder):
        async def publish_changes(self, changes):
            raise RuntimeError("telegram down")

    source = FakeTraffic()
    monitor = Monitor(
        Settings(),
        CATALOG,
        alert_source=None,
        sources=[source],
        publisher=Broken(),
    )
    await run_for(monitor, 0.1)
    assert source.calls >= 2
    assert "publishing failed" in caplog.text


@pytest.mark.parametrize("seconds", [0.05])
async def test_sources_are_closed_on_shutdown(seconds):
    closed = []

    class Closing(FakeTraffic):
        async def close(self):
            closed.append(self.source_id)

    monitor, _ = make_monitor([Closing()])
    await run_for(monitor, seconds)
    assert closed == ["fake-traffic"]


def test_cli_reports_config_error_without_traceback(isolated_settings, capsys):
    from bridges_monitor.cli import main

    workdir, _ = isolated_settings
    (workdir / "config.toml").write_text('[alerts]\nprovider = "alerts_in_ua"\n')
    assert main(["alerts"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Помилка конфігурації:")
    assert "not allowed in the basic edition" in err
