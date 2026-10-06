"""Цикл опитування джерел, обчислення стану й публікація змін.

- Джерело тривог опитується зі своїм інтервалом. Коли тривога починається чи
  закінчується, усі джерела заторів одразу переглядають свій розклад
  (наприклад, джерело «лише під час тривоги» опитується в момент її початку).
- Кожне джерело заторів має власний цикл; коли опитувати, вирішує plan_poll().
- Стан перераховується після нових даних, після зміни тривоги й раз на
  recompute_every (щоб застарілі твердження вчасно ставали unknown).

Дані тримаються в пам'яті: після перезапуску стан починається з unknown.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bridges_monitor.alerts import AlertTracker
from bridges_monitor.catalog import Catalog
from bridges_monitor.config import Settings
from bridges_monitor.models import (
    Claim,
    ModerationStatus,
    Report,
    StateKey,
    TrafficState,
)
from bridges_monitor.publishing import Publisher
from bridges_monitor.schedule import PollSchedule
from bridges_monitor.sources.alerts import AlertSource
from bridges_monitor.sources.base import Source, SourceError
from bridges_monitor.state import (
    accepts_source,
    claim_expires_at,
    compute_states,
    diff_states,
    initial_moderation,
    is_active,
)

log = logging.getLogger(__name__)

# Скільки зберігати завершені тривоги: твердження, зроблені під час них, однаково
# застарівають значно раніше.
ALERT_HISTORY = timedelta(days=1)


@dataclass(frozen=True)
class PollPlan:
    poll_now: bool
    # Якщо не опитувати зараз — коли переглянути план (раніше, якщо зміниться
    # стан тривоги).
    wake_at: datetime


def plan_poll(
    schedule: PollSchedule,
    last_poll: datetime | None,
    now: datetime,
    alert_active: bool,
) -> PollPlan:
    boundary = schedule.next_boundary(now)
    interval = schedule.interval_at(now, alert_active)
    if interval is None:
        return PollPlan(poll_now=False, wake_at=boundary)
    if last_poll is None or now >= last_poll + interval:
        return PollPlan(poll_now=True, wake_at=now)
    # На межі вікна інтервал може змінитися — тоді план переглядається.
    return PollPlan(poll_now=False, wake_at=min(last_poll + interval, boundary))


class Monitor:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        *,
        alert_source: AlertSource | None,
        sources: Sequence[Source],
        publisher: Publisher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        recompute_every: timedelta = timedelta(minutes=1),
    ):
        self.settings = settings
        self.catalog = catalog
        self.alert_source = alert_source
        self.sources = tuple(sources)
        self.publisher = publisher
        self.clock = clock
        self.recompute_every = recompute_every
        self.tracker = AlertTracker()
        self.claims: list[Claim] = []
        self.states: dict[StateKey, TrafficState] = {}
        self._alert_changed = asyncio.Event()
        self._dirty = asyncio.Event()

    @property
    def alert_active(self) -> bool:
        return self.tracker.current is not None

    async def run(self) -> None:
        # Базовий стан (усе unknown) не публікується — лише подальші зміни.
        self.states = self._compute(self.clock())
        async with AsyncExitStack() as stack:
            for source in (*filter(None, [self.alert_source]), *self.sources):
                await stack.enter_async_context(source)
            async with asyncio.TaskGroup() as tasks:
                if self.alert_source is not None:
                    tasks.create_task(self._alert_loop(self.alert_source))
                for source in self.sources:
                    tasks.create_task(self._source_loop(source))
                tasks.create_task(self._state_loop())

    # --- тривоги ---

    async def _alert_loop(self, source: AlertSource) -> None:
        first = True
        while True:
            try:
                snapshot = await source.fetch()
            except SourceError as e:
                log.warning("%s", e)
            else:
                if first:
                    log.info(
                        "%s: тривога %s",
                        source.source_id,
                        "є" if snapshot.active else "немає",
                    )
                    first = False
                if period := self.tracker.update(snapshot):
                    self._prune_alerts(snapshot.checked_at)
                    self._notify_alert_change()
                    await self._safe_publish(self.publisher.publish_alert(period))
            now = self.clock()
            interval = source.interval_at(now, self.alert_active)
            await asyncio.sleep(
                (interval or source.schedule.max_interval).total_seconds()
            )

    def _notify_alert_change(self) -> None:
        self._alert_changed.set()
        self._alert_changed = asyncio.Event()
        self._dirty.set()

    async def _wait(self, until: datetime) -> None:
        """Чекати до until або до зміни стану тривоги."""
        event = self._alert_changed
        timeout = max(0.0, (until - self.clock()).total_seconds())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass

    # --- джерела даних ---

    async def _source_loop(self, source: Source) -> None:
        last_poll: datetime | None = None
        while True:
            now = self.clock()
            plan = plan_poll(source.schedule, last_poll, now, self.alert_active)
            if not plan.poll_now:
                if plan.wake_at - now > timedelta(minutes=1):
                    log.info(
                        "%s: наступна перевірка розкладу о %s",
                        source.source_id,
                        plan.wake_at.astimezone(source.schedule.tz).strftime("%H:%M"),
                    )
                await self._wait(plan.wake_at)
                continue
            last_poll = now
            try:
                reports = await source.fetch()
            except SourceError as e:
                log.warning("%s", e)
                continue
            claims = self.ingest(reports)
            log.info(
                "%s: %d тверджень%s",
                source.source_id,
                claims,
                " (чекають на модерацію)"
                if self.settings.moderation_enabled and claims
                else "",
            )

    def ingest(self, reports: Iterable[Report]) -> int:
        """Додати твердження з повідомлень; повертає кількість прийнятих."""
        accepted = 0
        moderation = initial_moderation(self.settings)
        for report in reports:
            if not accepts_source(self.settings, report.source_kind):
                log.warning(
                    "%s: unofficial report dropped in %s edition",
                    report.source_id,
                    self.settings.edition,
                )
                continue
            if report.text is not None and not report.claims:
                log.info("%s: text report skipped (no parser yet)", report.source_id)
            self.claims.extend(
                c.model_copy(update={"moderation": moderation}) for c in report.claims
            )
            accepted += len(report.claims)
        self._dirty.set()
        return accepted

    # --- стан ---

    async def _state_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._dirty.wait(), self.recompute_every.total_seconds()
                )
            except TimeoutError:
                pass
            self._dirty.clear()
            await self.recompute()

    async def recompute(self) -> None:
        now = self.clock()
        new_states = self._compute(now)
        changes = diff_states(self.states, new_states, now)
        self.states = new_states
        self._prune_claims(now)
        if changes:
            await self._safe_publish(self.publisher.publish_changes(changes))

    def _compute(self, now: datetime) -> dict[StateKey, TrafficState]:
        return compute_states(
            self.catalog, self.claims, self.tracker.periods, now, self.settings
        )

    def _prune_claims(self, now: datetime) -> None:
        """Залишити чинні твердження й ті, що ще чекають на модерацію."""

        def keep(claim: Claim) -> bool:
            if claim.moderation is ModerationStatus.PENDING:
                expires = claim_expires_at(claim, self.tracker.periods, self.settings)
                return expires is None or now < expires
            return is_active(claim, self.tracker.periods, now, self.settings)

        self.claims = [c for c in self.claims if keep(c)]

    def _prune_alerts(self, now: datetime) -> None:
        self.tracker.periods = [
            p
            for p in self.tracker.periods
            if p.ended_at is None or now - p.ended_at < ALERT_HISTORY
        ]

    async def _safe_publish(self, publishing: Awaitable[None]) -> None:
        # Збій каналу доставки не повинен зупиняти моніторинг.
        try:
            await publishing
        except Exception:
            log.exception("publishing failed")
