"""Доставка змін стану споживачам. Конкретні канали (Telegram тощо) реалізують
Publisher; логіка збирання й агрегації від них не залежить."""

import logging
from collections.abc import Sequence
from typing import Protocol

from bridges_monitor.catalog import Catalog, Direction, TransportMode
from bridges_monitor.models import AlertPeriod, StateChange, TrafficState

log = logging.getLogger(__name__)

DIRECTION_UK = {
    Direction.TO_LEFT_BANK: "в бік лівого берега",
    Direction.TO_RIGHT_BANK: "в бік правого берега",
}
MODE_UK = {TransportMode.ROAD: "авто", TransportMode.METRO: "метро"}
STATUS_UK = {
    "open": "відкрито",
    "restricted": "обмежено",
    "closed": "закрито",
    "unknown": "невідомо",
}
CONGESTION_UK = {
    "free": "вільно",
    "moderate": "помірний затор",
    "heavy": "сильний затор",
    "standstill": "рух стоїть",
}


class Publisher(Protocol):
    async def publish_changes(self, changes: Sequence[StateChange]) -> None: ...

    async def publish_alert(self, period: AlertPeriod) -> None: ...


def describe_state(state: TrafficState | None) -> str:
    if state is None:
        return "—"
    text = STATUS_UK[state.status]
    if state.congestion is not None:
        text += f", {CONGESTION_UK[state.congestion]}"
    return text


class LogPublisher:
    """Тимчасовий канал: пише зміни в лог (до появи Telegram-бота)."""

    def __init__(self, catalog: Catalog):
        self._names = {b.id: b.name["uk"] for b in catalog.bridges}

    async def publish_changes(self, changes: Sequence[StateChange]) -> None:
        for change in changes:
            bridge_id, mode, direction = change.key
            log.info(
                "%s %s (%s): %s → %s",
                self._names[bridge_id],
                DIRECTION_UK[direction],
                MODE_UK[mode],
                describe_state(change.before),
                describe_state(change.after),
            )

    async def publish_alert(self, period: AlertPeriod) -> None:
        if period.ended_at is None:
            log.info("Повітряна тривога в Києві (з %s)", period.started_at)
        else:
            log.info("Відбій повітряної тривоги в Києві (%s)", period.ended_at)
