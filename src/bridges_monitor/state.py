"""Обчислення стану руху зі схвалених тверджень.

Правила:
- у базовій версії твердження з неофіційних джерел ігноруються завжди;
- твердження діє до valid_until; або до відбою тривоги (until_alert_end);
  або, якщо термін не вказано, stale_after / stale_after_alert від observed_at;
- статус: офіційне джерело переважає неофіційне, серед рівних — новіше;
- завантаженість: найновіше твердження; для закритого руху не показується;
- немає жодного чинного твердження — статус unknown.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from bridges_monitor.catalog import Catalog, Direction
from bridges_monitor.config import Edition, Settings
from bridges_monitor.models import (
    AlertPeriod,
    Claim,
    ModerationStatus,
    SourceKind,
    StateChange,
    StateKey,
    TrafficState,
    TrafficStatus,
)


def accepts_source(settings: Settings, kind: SourceKind) -> bool:
    return settings.edition is Edition.EXTENDED or kind is SourceKind.OFFICIAL


def initial_moderation(settings: Settings) -> ModerationStatus:
    if settings.moderation_enabled:
        return ModerationStatus.PENDING
    return ModerationStatus.APPROVED


def active_alert(alerts: Iterable[AlertPeriod], at: datetime) -> AlertPeriod | None:
    return next((a for a in alerts if a.covers(at)), None)


def claim_expires_at(
    claim: Claim, alerts: Iterable[AlertPeriod], settings: Settings
) -> datetime | None:
    """None — діє безстроково (до відбою тривоги, яка ще триває)."""
    if claim.valid_until is not None:
        return claim.valid_until
    alert = active_alert(alerts, claim.observed_at)
    if claim.until_alert_end and alert is not None:
        return alert.ended_at
    ttl = settings.stale_after_alert if alert is not None else settings.stale_after
    return claim.observed_at + ttl


def is_active(
    claim: Claim, alerts: Iterable[AlertPeriod], now: datetime, settings: Settings
) -> bool:
    if claim.moderation is not ModerationStatus.APPROVED:
        return False
    if not accepts_source(settings, claim.source_kind) or claim.observed_at > now:
        return False
    expires_at = claim_expires_at(claim, alerts, settings)
    return expires_at is None or now < expires_at


def _status_priority(claim: Claim) -> tuple[bool, datetime]:
    return claim.source_kind is SourceKind.OFFICIAL, claim.observed_at


def _resolve(claims: list[Claim]) -> TrafficState:
    status_claim = max(
        (c for c in claims if c.status is not None),
        key=_status_priority,
        default=None,
    )
    congestion_claim = max(
        (c for c in claims if c.congestion is not None),
        key=lambda c: c.observed_at,
        default=None,
    )
    if status_claim is None:
        status = TrafficStatus.UNKNOWN
    else:
        assert status_claim.status is not None
        status = status_claim.status
    if status is TrafficStatus.CLOSED:
        congestion_claim = None
    used = [c for c in (status_claim, congestion_claim) if c is not None]
    return TrafficState(
        status=status,
        restrictions=status_claim.restrictions if status_claim else (),
        congestion=congestion_claim.congestion if congestion_claim else None,
        since=max((c.observed_at for c in used), default=None),
        claim_ids=tuple(dict.fromkeys(c.id for c in used)),
    )


def compute_states(
    catalog: Catalog,
    claims: Iterable[Claim],
    alerts: Iterable[AlertPeriod],
    now: datetime,
    settings: Settings,
) -> dict[StateKey, TrafficState]:
    """Стан кожної трійки (міст, вид транспорту, напрямок), що в експлуатації."""
    alerts = list(alerts)
    by_key: dict[StateKey, list[Claim]] = defaultdict(list)
    for claim in claims:
        if is_active(claim, alerts, now, settings):
            for direction in claim.directions:
                by_key[StateKey(claim.bridge_id, claim.mode, direction)].append(claim)

    return {
        key: _resolve(by_key.get(key, []))
        for bridge in catalog.bridges
        for mode in bridge.modes
        if bridge.in_service(mode)
        for direction in Direction
        for key in [StateKey(bridge.id, mode, direction)]
    }


def diff_states(
    before: dict[StateKey, TrafficState],
    after: dict[StateKey, TrafficState],
    at: datetime,
) -> list[StateChange]:
    return [
        StateChange(key=key, before=before.get(key), after=state, at=at)
        for key, state in after.items()
        if key not in before or not before[key].same_as(state)
    ]


def find_conflicts(claim: Claim, active_claims: Iterable[Claim]) -> list[Claim]:
    """Чинні офіційні твердження, що суперечать даному (для позначки модератору)."""
    return [
        other
        for other in active_claims
        if other.id != claim.id
        and other.source_kind is SourceKind.OFFICIAL
        and (other.bridge_id, other.mode) == (claim.bridge_id, claim.mode)
        and other.directions & claim.directions
        and None not in (other.status, claim.status)
        and other.status != claim.status
    ]
