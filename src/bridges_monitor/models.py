"""Модель стану руху.

Повідомлення (Report) — сирі дані з джерела. Твердження (Claim) — структурований
факт про один міст і вид транспорту, витягнутий з повідомлення. Стан
(TrafficState) обчислюється зі схвалених тверджень для кожної трійки
StateKey(міст, вид транспорту, напрямок) — див. state.py.
"""

from enum import StrEnum
from typing import NamedTuple, Self
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from bridges_monitor.catalog import Direction, LocalizedText, TransportMode


def new_id() -> str:
    return uuid4().hex


class SourceKind(StrEnum):
    OFFICIAL = "official"
    UNOFFICIAL = "unofficial"


class TrafficStatus(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    CLOSED = "closed"
    UNKNOWN = "unknown"  # даних немає або вони застаріли


class RestrictionType(StrEnum):
    LANES_CLOSED = "lanes_closed"
    EMERGENCY_ONLY = "emergency_only"
    PUBLIC_TRANSPORT_ONLY = "public_transport_only"
    NO_TRUCKS = "no_trucks"
    SPEED_LIMIT = "speed_limit"
    METRO_NO_STOP = "metro_no_stop"
    METRO_REDUCED_FREQUENCY = "metro_reduced_frequency"
    OTHER = "other"


class CongestionLevel(StrEnum):
    """Завантаженість: незалежна від статусу характеристика (рух відкрито, але затор)."""

    FREE = "free"
    MODERATE = "moderate"
    HEAVY = "heavy"
    STANDSTILL = "standstill"


CONGESTION_SEVERITY = {level: i for i, level in enumerate(CongestionLevel)}


class Extractor(StrEnum):
    SOURCE = "source"  # джерело віддає структуровані дані
    RULES = "rules"
    LLM = "llm"
    MANUAL = "manual"


class ModerationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Restriction(_Model):
    type: RestrictionType
    description: LocalizedText | None = None


class Claim(_Model):
    id: str = Field(default_factory=new_id)
    report_id: str
    source_id: str
    source_kind: SourceKind
    bridge_id: str
    mode: TransportMode
    directions: frozenset[Direction] = frozenset(Direction)
    # None — твердження лише про завантаженість.
    status: TrafficStatus | None = None
    restrictions: tuple[Restriction, ...] = ()
    congestion: CongestionLevel | None = None
    observed_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    # Діє до відбою тривоги, під час якої зроблене.
    until_alert_end: bool = False
    extracted_by: Extractor
    moderation: ModerationStatus = ModerationStatus.PENDING

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status is None and self.congestion is None:
            raise ValueError("claim must state status or congestion")
        if self.status is TrafficStatus.UNKNOWN:
            raise ValueError("claim cannot assert unknown status")
        if self.restrictions and self.status is not TrafficStatus.RESTRICTED:
            raise ValueError("restrictions require restricted status")
        if not self.directions:
            raise ValueError("claim must have at least one direction")
        return self


class Report(_Model):
    id: str = Field(default_factory=new_id)
    source_id: str
    source_kind: SourceKind
    fetched_at: AwareDatetime
    published_at: AwareDatetime | None = None
    url: str | None = None
    text: str | None = None
    # Заповнюється, якщо джерело одразу віддає структуровані дані.
    claims: tuple[Claim, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.text is None and not self.claims:
            raise ValueError("report must have text or claims")
        for claim in self.claims:
            if (claim.report_id, claim.source_id, claim.source_kind) != (
                self.id,
                self.source_id,
                self.source_kind,
            ):
                raise ValueError(f"claim {claim.id} does not belong to report")
        return self


class AlertPeriod(_Model):
    """Повітряна тривога в Києві; ended_at=None — триває."""

    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None

    def covers(self, at: AwareDatetime) -> bool:
        return self.started_at <= at and (self.ended_at is None or at < self.ended_at)


class AlertSnapshot(_Model):
    """Результат одного опитування джерела тривог."""

    active: bool
    # Початок поточної тривоги, якщо джерело його повідомляє.
    started_at: AwareDatetime | None = None
    checked_at: AwareDatetime


class StateKey(NamedTuple):
    bridge_id: str
    mode: TransportMode
    direction: Direction


class TrafficState(_Model):
    status: TrafficStatus
    restrictions: tuple[Restriction, ...] = ()
    congestion: CongestionLevel | None = None
    since: AwareDatetime | None = None
    claim_ids: tuple[str, ...] = ()

    def same_as(self, other: TrafficState) -> bool:
        return (self.status, self.restrictions, self.congestion) == (
            other.status,
            other.restrictions,
            other.congestion,
        )


class StateChange(_Model):
    key: StateKey
    before: TrafficState | None
    after: TrafficState
    at: AwareDatetime
