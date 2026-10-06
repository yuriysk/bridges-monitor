from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bridges_monitor.catalog import TransportMode
from bridges_monitor.models import (
    CongestionLevel,
    Extractor,
    Report,
    SourceKind,
    TrafficStatus,
)
from bridges_monitor.sources import Source


class FakeTrafficSource(Source):
    async def fetch(self) -> list[Report]:
        now = datetime.now(UTC)
        return [
            self.make_report(
                claims=[
                    {
                        "bridge_id": "bridge-paton",
                        "mode": TransportMode.ROAD,
                        "status": TrafficStatus.OPEN,
                        "congestion": CongestionLevel.HEAVY,
                        "observed_at": now,
                    }
                ]
            ),
            self.make_report(text="Рух мостом Патона відкрито", published_at=now),
        ]


def test_source_must_implement_fetch():
    with pytest.raises(TypeError):
        Source("x", SourceKind.OFFICIAL, timedelta(minutes=1))  # type: ignore[abstract]


async def test_make_report_fills_source_fields():
    async with FakeTrafficSource(
        "maps", SourceKind.UNOFFICIAL, timedelta(minutes=5)
    ) as src:
        structured, text = await src.fetch()

    (claim,) = structured.claims
    assert claim.report_id == structured.id
    assert (claim.source_id, claim.source_kind) == ("maps", SourceKind.UNOFFICIAL)
    assert claim.extracted_by is Extractor.SOURCE
    assert text.text and not text.claims


def test_report_rejects_foreign_claims():
    src = FakeTrafficSource("a", SourceKind.OFFICIAL, timedelta(minutes=1))
    report = src.make_report(
        claims=[
            {
                "bridge_id": "bridge-paton",
                "mode": TransportMode.ROAD,
                "status": TrafficStatus.CLOSED,
                "observed_at": datetime.now(UTC),
            }
        ]
    )
    with pytest.raises(ValidationError, match="does not belong"):
        Report(
            source_id="b",
            source_kind=SourceKind.OFFICIAL,
            fetched_at=report.fetched_at,
            claims=report.claims,
        )
