from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from bridges_monitor.catalog import Direction, TransportMode, load_catalog
from bridges_monitor.config import Edition, Settings
from bridges_monitor.models import (
    AlertPeriod,
    Claim,
    CongestionLevel,
    Extractor,
    ModerationStatus,
    Restriction,
    RestrictionType,
    SourceKind,
    StateKey,
    TrafficStatus,
)
from bridges_monitor.state import (
    compute_states,
    diff_states,
    find_conflicts,
    initial_moderation,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class InitOnlySettings(Settings):
    """Лише явно передані значення: модуль імпортується до фікстури ізоляції,
    тож справжні config.toml/.env не мають впливати на ці константи."""

    model_config = SettingsConfigDict(toml_file=None, env_file=None)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_):
        return (init_settings,)


BASIC = InitOnlySettings(edition=Edition.BASIC)
EXTENDED = InitOnlySettings(edition=Edition.EXTENDED)
CATALOG = load_catalog()
ROAD, METRO = TransportMode.ROAD, TransportMode.METRO
LEFT, RIGHT = Direction.TO_LEFT_BANK, Direction.TO_RIGHT_BANK


def claim(**fields) -> Claim:
    defaults = {
        "report_id": "r",
        "source_id": "s",
        "source_kind": SourceKind.OFFICIAL,
        "bridge_id": "bridge-paton",
        "mode": ROAD,
        "status": TrafficStatus.CLOSED,
        "observed_at": NOW - timedelta(minutes=5),
        "extracted_by": Extractor.MANUAL,
        "moderation": ModerationStatus.APPROVED,
    }
    return Claim(**{**defaults, **fields})


def states(claims, alerts=(), settings=BASIC, now=NOW):
    return compute_states(CATALOG, claims, alerts, now, settings)


def patona(result, direction=LEFT):
    return result[StateKey("bridge-paton", ROAD, direction)]


def test_no_claims_means_unknown_everywhere():
    result = states([])
    assert {s.status for s in result.values()} == {TrafficStatus.UNKNOWN}


def test_out_of_service_modes_have_no_state():
    result = states([])
    assert StateKey("bridge-podil", METRO, LEFT) not in result
    assert StateKey("bridge-podil", ROAD, LEFT) in result


def test_claim_applies_only_to_its_directions():
    result = states([claim(directions={LEFT})])
    assert patona(result, LEFT).status is TrafficStatus.CLOSED
    assert patona(result, RIGHT).status is TrafficStatus.UNKNOWN


def test_unapproved_claims_ignored():
    result = states([claim(moderation=ModerationStatus.PENDING)])
    assert patona(result).status is TrafficStatus.UNKNOWN


def test_basic_edition_ignores_unofficial_even_if_approved():
    c = claim(source_kind=SourceKind.UNOFFICIAL)
    assert patona(states([c], settings=BASIC)).status is TrafficStatus.UNKNOWN
    assert patona(states([c], settings=EXTENDED)).status is TrafficStatus.CLOSED


def test_official_beats_newer_unofficial():
    official = claim(observed_at=NOW - timedelta(minutes=30))
    unofficial = claim(
        source_kind=SourceKind.UNOFFICIAL,
        status=TrafficStatus.OPEN,
        observed_at=NOW - timedelta(minutes=1),
    )
    result = states([official, unofficial], settings=EXTENDED)
    assert patona(result).status is TrafficStatus.CLOSED
    assert find_conflicts(unofficial, [official]) == [official]


def test_newer_claim_wins_among_equals():
    old = claim(observed_at=NOW - timedelta(hours=1))
    new = claim(status=TrafficStatus.OPEN, observed_at=NOW - timedelta(minutes=1))
    assert patona(states([old, new])).status is TrafficStatus.OPEN


def test_claim_goes_stale():
    c = claim(observed_at=NOW - BASIC.stale_after - timedelta(seconds=1))
    assert patona(states([c])).status is TrafficStatus.UNKNOWN


def test_claim_during_alert_goes_stale_faster():
    alert = AlertPeriod(started_at=NOW - timedelta(hours=2))
    c = claim(observed_at=NOW - timedelta(hours=1))
    assert patona(states([c])).status is TrafficStatus.CLOSED
    assert patona(states([c], alerts=[alert])).status is TrafficStatus.UNKNOWN


def test_until_alert_end():
    observed = NOW - timedelta(hours=1)
    c = claim(observed_at=observed, until_alert_end=True)
    ongoing = AlertPeriod(started_at=observed - timedelta(minutes=1))
    ended = AlertPeriod(
        started_at=ongoing.started_at, ended_at=NOW - timedelta(minutes=1)
    )
    assert patona(states([c], alerts=[ongoing])).status is TrafficStatus.CLOSED
    assert patona(states([c], alerts=[ended])).status is TrafficStatus.UNKNOWN


def test_explicit_valid_until_overrides_staleness():
    c = claim(observed_at=NOW - timedelta(days=1), valid_until=NOW + timedelta(hours=1))
    assert patona(states([c])).status is TrafficStatus.CLOSED


def test_congestion_combines_with_status_but_hidden_when_closed():
    status = claim(
        status=TrafficStatus.RESTRICTED,
        restrictions=[Restriction(type=RestrictionType.NO_TRUCKS)],
    )
    traffic = claim(status=None, congestion=CongestionLevel.HEAVY)
    state = patona(states([status, traffic]))
    assert state.status is TrafficStatus.RESTRICTED
    assert state.congestion is CongestionLevel.HEAVY
    assert set(state.claim_ids) == {status.id, traffic.id}

    closed = claim()
    assert patona(states([closed, traffic])).congestion is None


def test_metro_state_is_separate_from_road():
    result = states([claim(bridge_id="bridge-metro", mode=METRO)])
    assert result[StateKey("bridge-metro", METRO, LEFT)].status is TrafficStatus.CLOSED
    assert result[StateKey("bridge-metro", ROAD, LEFT)].status is TrafficStatus.UNKNOWN
    assert StateKey("bridge-paton", METRO, LEFT) not in result


def test_diff_reports_only_changes():
    before = states([])
    after = states([claim(directions={LEFT})])
    changes = diff_states(before, after, NOW)
    assert [c.key for c in changes] == [StateKey("bridge-paton", ROAD, LEFT)]
    assert changes[0].before.status is TrafficStatus.UNKNOWN
    assert diff_states(after, after, NOW) == []


def test_initial_moderation_applies_to_status_claims_only():
    status_claim = claim()
    congestion_only = claim(status=None, congestion=CongestionLevel.HEAVY)
    moderated_basic = Settings(edition=Edition.BASIC, moderation=True)

    assert initial_moderation(BASIC, status_claim) is ModerationStatus.APPROVED
    assert initial_moderation(EXTENDED, status_claim) is ModerationStatus.PENDING
    assert initial_moderation(moderated_basic, status_claim) is ModerationStatus.PENDING
    for settings in (BASIC, EXTENDED, moderated_basic):
        assert (
            initial_moderation(settings, congestion_only) is ModerationStatus.APPROVED
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"status": None},
        {"status": TrafficStatus.UNKNOWN},
        {"restrictions": [Restriction(type=RestrictionType.OTHER)]},
        {"directions": set()},
    ],
)
def test_invalid_claims(fields):
    with pytest.raises(ValidationError):
        claim(**fields)
