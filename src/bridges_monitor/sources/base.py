from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from bridges_monitor.models import Claim, Extractor, Report, SourceKind, new_id
from bridges_monitor.schedule import PollSchedule


class SourceError(Exception):
    """Джерело тимчасово недоступне або повернуло неочікувані дані."""


class BaseSource[T](ABC):
    """Спільний інтерфейс усіх джерел: id, тип (офіційне чи ні), розклад опитування,
    fetch() і звільнення ресурсів. Конкретні різновиди: Source, AlertSource."""

    def __init__(
        self,
        source_id: str,
        kind: SourceKind,
        schedule: PollSchedule | timedelta,
    ):
        """schedule: розклад або сталий інтервал опитування."""
        self.source_id = source_id
        self.kind = kind
        self.schedule = (
            PollSchedule.constant(schedule)
            if isinstance(schedule, timedelta)
            else schedule
        )

    def interval_at(self, at: datetime, alert_active: bool) -> timedelta | None:
        """Через скільки опитати знову, якщо опитано зараз (at).

        None — зараз не опитувати; перевірити знову в schedule.next_boundary(at)
        або коли зміниться стан тривоги.
        """
        return self.schedule.interval_at(at, alert_active)

    @abstractmethod
    async def fetch(self) -> T:
        """Опитати джерело; у разі збою — SourceError."""

    async def close(self) -> None:
        """Звільнити ресурси (HTTP-сесії, клієнти); за замовчуванням нічого."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


class Source(BaseSource[list[Report]]):
    """Джерело даних про стан мостів (офіційні канали, сайти, карти заторів тощо).

    Підклас реалізує лише fetch(): нові повідомлення з часу попереднього виклику.
    Текстові джерела повертають повідомлення з text, а твердження з них витягує
    окремий парсер. Структуровані джерела (наприклад, дані про завантаженість)
    одразу додають твердження через make_report(claims=...).
    """

    def make_report(
        self,
        *,
        text: str | None = None,
        url: str | None = None,
        published_at: datetime | None = None,
        claims: Iterable[Mapping[str, Any]] = (),
    ) -> Report:
        """Створює повідомлення від імені цього джерела.

        claims — поля Claim без report_id/source_id/source_kind: їх заповнює базовий клас.
        """
        report_id = new_id()
        return Report(
            id=report_id,
            source_id=self.source_id,
            source_kind=self.kind,
            fetched_at=datetime.now(UTC),
            published_at=published_at,
            url=url,
            text=text,
            claims=tuple(
                Claim(
                    report_id=report_id,
                    source_id=self.source_id,
                    source_kind=self.kind,
                    **{"extracted_by": Extractor.SOURCE, **fields},
                )
                for fields in claims
            ),
        )
