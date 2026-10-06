"""Розклад опитування джерел: інтервал залежить від місцевого часу й тривоги.

Вікно діє від свого start до start наступного вікна (останнє — до кінця доби).
Перше вікно має починатися о 00:00. Вікно без interval означає «не опитувати».
"""

from datetime import datetime, time, timedelta
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DEFAULT_TIMEZONE = "Europe/Kyiv"


class PollWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: time
    # None — у цьому вікні не опитувати (у TOML: просто не вказувати interval).
    interval: timedelta | None = None

    @field_validator("interval")
    @classmethod
    def _positive(cls, value: timedelta | None) -> timedelta | None:
        if value is not None and value <= timedelta(0):
            raise ValueError("interval must be positive")
        return value


def _check_windows(windows: tuple[PollWindow, ...], name: str) -> None:
    if not windows:
        raise ValueError(f"{name}: at least one window is required")
    if windows[0].start != time(0, 0):
        raise ValueError(f"{name}: the first window must start at 00:00")
    starts = [w.start for w in windows]
    if starts != sorted(set(starts)):
        raise ValueError(f"{name}: window starts must be strictly increasing")


class PollSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str = DEFAULT_TIMEZONE
    normal: tuple[PollWindow, ...]  # повітряної тривоги немає
    alert: tuple[PollWindow, ...]  # повітряна тривога є

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {value!r}") from e
        return value

    @model_validator(mode="after")
    def _valid_windows(self) -> Self:
        _check_windows(self.normal, "normal")
        _check_windows(self.alert, "alert")
        if all(w.interval is None for w in (*self.normal, *self.alert)):
            raise ValueError("schedule never polls; disable the source instead")
        return self

    @classmethod
    def constant(cls, interval: timedelta) -> PollSchedule:
        windows = (PollWindow(start=time(0, 0), interval=interval),)
        return cls(normal=windows, alert=windows)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _windows(self, alert_active: bool) -> tuple[PollWindow, ...]:
        return self.alert if alert_active else self.normal

    def interval_at(self, at: datetime, alert_active: bool) -> timedelta | None:
        """None — у цей час (у цьому режимі) не опитувати."""
        local = at.astimezone(ZoneInfo(self.timezone)).time()
        return next(
            w.interval
            for w in reversed(self._windows(alert_active))
            if w.start <= local
        )

    def longest_interval_at(self, at: datetime) -> timedelta | None:
        intervals = [
            i for i in (self.interval_at(at, False), self.interval_at(at, True)) if i
        ]
        return max(intervals, default=None)

    @property
    def max_interval(self) -> timedelta:
        """Найдовший інтервал розкладу (валідатор гарантує, що він є)."""
        return max(w.interval for w in (*self.normal, *self.alert) if w.interval)

    def next_boundary(self, at: datetime) -> datetime:
        """Найближчий після at початок будь-якого вікна (в обох режимах): коли
        опитування призупинене, саме тоді варто перевірити розклад знову."""
        tz = ZoneInfo(self.timezone)
        local = at.astimezone(tz)
        starts = sorted({w.start for w in (*self.normal, *self.alert)})
        for day_offset in (0, 1):
            day = local.date() + timedelta(days=day_offset)
            for start in starts:
                candidate = datetime.combine(day, start, tzinfo=tz)
                if candidate > local:
                    return candidate
        raise AssertionError("unreachable: the first window starts at 00:00")

    def polls_per_day(self, alert_active: bool) -> float:
        """Скільки опитувань за добу, якщо режим (тривога чи ні) не змінюється."""
        windows = self._windows(alert_active)
        starts = [_seconds(w.start) for w in windows]
        ends = [*starts[1:], 24 * 3600]
        return sum(
            (end - start) / w.interval.total_seconds()
            for w, start, end in zip(windows, starts, ends, strict=True)
            if w.interval is not None
        )


def _seconds(t: time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _windows(*spec: tuple[int, int]) -> tuple[PollWindow, ...]:
    return tuple(
        PollWindow(start=time(hour, 0), interval=timedelta(minutes=minutes))
        for hour, minutes in spec
    )


# Типовий розклад для джерел заторів (за київським часом).
DEFAULT_TRAFFIC_SCHEDULE = PollSchedule(
    normal=_windows((0, 20), (2, 30), (5, 15), (7, 10), (11, 15), (16, 10), (20, 15)),
    alert=_windows((0, 20), (2, 30), (5, 15), (7, 5), (11, 10), (16, 5), (20, 15)),
)
