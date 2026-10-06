from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from bridges_monitor.config import Edition, Settings, TrafficProvider
from bridges_monitor.schedule import (
    DEFAULT_TRAFFIC_SCHEDULE,
    PollSchedule,
    PollWindow,
)

KYIV = ZoneInfo("Europe/Kyiv")
S = DEFAULT_TRAFFIC_SCHEDULE


def kyiv(hour: int, minute: int = 0, day: int = 5, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KYIV)


@pytest.mark.parametrize(
    ("hour", "minute", "normal", "alert"),
    [
        (0, 0, 20, 20),
        (1, 59, 20, 20),
        (2, 0, 30, 30),
        (4, 59, 30, 30),
        (5, 0, 15, 15),
        (7, 0, 10, 5),
        (10, 59, 10, 5),
        (11, 0, 15, 10),
        (15, 59, 15, 10),
        (16, 0, 10, 5),
        (19, 59, 10, 5),
        (20, 0, 15, 15),
        (23, 59, 15, 15),
    ],
)
def test_default_traffic_schedule(hour, minute, normal, alert):
    at = kyiv(hour, minute)
    assert S.interval_at(at, alert_active=False) == timedelta(minutes=normal)
    assert S.interval_at(at, alert_active=True) == timedelta(minutes=alert)


def test_uses_kyiv_local_time_for_utc_input_across_dst():
    # 05:00 UTC: улітку (UTC+3) це 08:00 у Києві, узимку (UTC+2) — 07:00.
    summer = datetime(2026, 7, 1, 5, 0, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 4, 59, tzinfo=UTC)
    assert S.interval_at(summer, alert_active=True) == timedelta(minutes=5)
    assert S.interval_at(winter, alert_active=True) == timedelta(minutes=15)


def test_polls_per_day_and_mapbox_quota():
    assert S.polls_per_day(alert_active=False) == pytest.approx(104)
    assert S.polls_per_day(alert_active=True) == pytest.approx(162)
    # 16 проїзних частин; навіть із тривогою цілу добу — у межах 100 000/місяць.
    assert S.polls_per_day(alert_active=True) * 16 * 31 < 100_000


def test_longest_interval():
    assert S.longest_interval_at(kyiv(8)) == timedelta(minutes=10)


def test_constant_schedule():
    s = PollSchedule.constant(timedelta(seconds=30))
    assert s.interval_at(kyiv(8), alert_active=True) == timedelta(seconds=30)
    assert s.polls_per_day(alert_active=False) == pytest.approx(2880)


def w(hour: int, minutes: int) -> dict:
    return {"start": time(hour, 0), "interval": timedelta(minutes=minutes)}


@pytest.mark.parametrize(
    ("normal", "error"),
    [
        ([], "at least one window"),
        ([w(1, 10)], "must start at 00:00"),
        ([w(0, 10), w(5, 10), w(3, 10)], "strictly increasing"),
        ([w(0, 10), w(0, 20)], "strictly increasing"),
        ([w(0, 0)], "must be positive"),
    ],
)
def test_invalid_schedules(normal, error):
    with pytest.raises(ValidationError, match=error):
        PollSchedule(normal=normal, alert=[w(0, 10)])


def test_unknown_timezone():
    with pytest.raises(ValidationError, match="unknown timezone"):
        PollSchedule(timezone="Mars/Olympus", normal=[w(0, 1)], alert=[w(0, 1)])


def test_schedule_from_toml_and_provider_override(isolated_settings):
    workdir, _ = isolated_settings
    (workdir / "config.toml").write_text(
        'edition = "extended"\n'
        "[traffic_schedule]\n"
        'normal = [{ start = "00:00", interval = "PT30M" }, '
        '{ start = "08:00", interval = "PT5M" }]\n'
        'alert = [{ start = "00:00", interval = "PT1M" }]\n'
        "[traffic.mapbox]\n"
        "[traffic.here.schedule]\n"
        'normal = [{ start = "00:00", interval = "PT1H" }]\n'
        'alert = [{ start = "00:00", interval = "PT1H" }]\n'
    )
    settings = Settings()
    assert settings.edition is Edition.EXTENDED
    assert settings.traffic_schedule.normal[1] == PollWindow(
        start=time(8, 0), interval=timedelta(minutes=5)
    )
    assert settings.traffic[TrafficProvider.MAPBOX].schedule is None
    here = settings.traffic[TrafficProvider.HERE].schedule
    assert here.interval_at(kyiv(8), alert_active=False) == timedelta(hours=1)


def test_default_settings_use_default_traffic_schedule():
    assert Settings().traffic_schedule == DEFAULT_TRAFFIC_SCHEDULE


GOOGLE_LIKE = PollSchedule(
    normal=[{"start": "00:00"}],
    alert=[
        {"start": "00:00"},
        {"start": "08:00", "interval": "PT30M"},
        {"start": "20:00"},
    ],
)


@pytest.mark.parametrize(
    ("hour", "alert", "expected"),
    [
        (7, True, None),
        (8, True, timedelta(minutes=30)),
        (19, True, timedelta(minutes=30)),
        (20, True, None),
        (12, False, None),
    ],
)
def test_paused_windows(hour, alert, expected):
    assert GOOGLE_LIKE.interval_at(kyiv(hour, 30), alert_active=alert) == expected


def test_paused_windows_quota():
    assert GOOGLE_LIKE.polls_per_day(alert_active=False) == 0
    assert GOOGLE_LIKE.polls_per_day(alert_active=True) == pytest.approx(24)
    assert GOOGLE_LIKE.polls_per_day(alert_active=True) * 12 * 31 < 10_000


def test_longest_and_max_interval_with_pauses():
    assert GOOGLE_LIKE.longest_interval_at(kyiv(3)) is None
    assert GOOGLE_LIKE.longest_interval_at(kyiv(9)) == timedelta(minutes=30)
    assert GOOGLE_LIKE.max_interval == timedelta(minutes=30)


def test_schedule_that_never_polls_is_rejected():
    with pytest.raises(ValidationError, match="never polls"):
        PollSchedule(normal=[{"start": "00:00"}], alert=[{"start": "00:00"}])


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (kyiv(7, 30), kyiv(8)),
        (kyiv(8), kyiv(20)),
        (kyiv(21), kyiv(0, day=6)),
    ],
)
def test_next_boundary(at, expected):
    assert GOOGLE_LIKE.next_boundary(at) == expected


def test_next_boundary_across_dst_change():
    # 25.10.2026 о 04:00 Київ переходить на зимовий час; межа 08:00 — за місцевим.
    at = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    assert GOOGLE_LIKE.next_boundary(at) == datetime(2026, 10, 25, 8, 0, tzinfo=KYIV)
    assert GOOGLE_LIKE.next_boundary(at).utcoffset() == timedelta(hours=2)
