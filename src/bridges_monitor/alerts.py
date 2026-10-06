"""Перетворення результатів опитування джерела тривог на періоди AlertPeriod."""

from collections.abc import Iterable

from bridges_monitor.models import AlertPeriod, AlertSnapshot


class AlertTracker:
    def __init__(self, periods: Iterable[AlertPeriod] = ()):
        self.periods = list(periods)

    @property
    def current(self) -> AlertPeriod | None:
        if self.periods and self.periods[-1].ended_at is None:
            return self.periods[-1]
        return None

    def update(self, snapshot: AlertSnapshot) -> AlertPeriod | None:
        """Повертає період, що почався або завершився; None — змін немає.

        Без початку тривоги від джерела береться час опитування; відбій фіксується
        часом першого опитування, яке показало відсутність тривоги.
        """
        current = self.current
        if snapshot.active and current is None:
            started_at = snapshot.started_at or snapshot.checked_at
            if self.periods and self.periods[-1].ended_at:
                # Не накладати на попередній період, якщо джерело дало старий початок.
                started_at = max(started_at, self.periods[-1].ended_at)
            period = AlertPeriod(started_at=started_at)
            self.periods.append(period)
            return period
        if not snapshot.active and current is not None:
            ended = current.model_copy(
                update={"ended_at": max(snapshot.checked_at, current.started_at)}
            )
            self.periods[-1] = ended
            return ended
        return None
