"""Джерела стану повітряної тривоги в Києві.

ukrainealarm.com — офіційний API застосунку «Повітряна тривога».
alerts.in.ua — неофіційний агрегатор. Обидва потребують токен.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from bridges_monitor.config import AlertProvider, Settings
from bridges_monitor.models import AlertSnapshot, SourceKind
from bridges_monitor.schedule import PollSchedule
from bridges_monitor.sources.base import BaseSource, SourceError
from bridges_monitor.sources.http import HttpClient

KYIV_REGION_ID = "31"  # однаковий в обох API для «м. Київ»


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AlertSource(BaseSource[AlertSnapshot]):
    """Джерело тривог: fetch() повертає поточний стан для одного регіону."""

    default_base_url: str

    def __init__(
        self,
        source_id: str,
        kind: SourceKind,
        schedule: PollSchedule | timedelta,
        *,
        token: str,
        region_id: str = KYIV_REGION_ID,
        base_url: str | None = None,
    ):
        super().__init__(source_id, kind, schedule)
        self.region_id = region_id
        self._token = token
        self._http = HttpClient(source_id, base_url or self.default_base_url)

    async def _get_json(self, path: str, headers: dict[str, str]) -> Any:
        return await self._http.request_json("GET", path, headers=headers)

    async def close(self) -> None:
        await self._http.close()


def parse_ukrainealarm(
    payload: Any, region_id: str, checked_at: datetime
) -> AlertSnapshot:
    """Відповідь GET /api/v3/alerts/{regionId}: список регіонів з activeAlerts.

    Формат відтворено з документації клієнтських бібліотек; на живому API з
    токеном ще не перевірено. Початок тривоги — lastUpdate тривоги типу AIR.
    """
    try:
        regions = [r for r in payload if str(r["regionId"]) == region_id]
        alerts = [
            a
            for r in regions
            for a in r.get("activeAlerts") or []
            if a["type"] == "AIR"
        ]
        starts = [_parse_dt(a["lastUpdate"]) for a in alerts if a.get("lastUpdate")]
    except (TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected ukrainealarm payload: {e!r}") from e
    return AlertSnapshot(
        active=bool(alerts), started_at=min(starts, default=None), checked_at=checked_at
    )


def parse_alerts_in_ua(
    payload: Any, region_id: str, checked_at: datetime
) -> AlertSnapshot:
    """Відповідь GET /v1/alerts/active.json: {"alerts": [{location_uid, alert_type, started_at, ...}]}."""
    try:
        alerts = [
            a
            for a in payload["alerts"]
            if str(a["location_uid"]) == region_id and a["alert_type"] == "air_raid"
        ]
        starts = [_parse_dt(a["started_at"]) for a in alerts if a.get("started_at")]
    except (TypeError, KeyError, ValueError) as e:
        raise SourceError(f"unexpected alerts.in.ua payload: {e!r}") from e
    return AlertSnapshot(
        active=bool(alerts), started_at=min(starts, default=None), checked_at=checked_at
    )


class UkraineAlarmSource(AlertSource):
    default_base_url = "https://api.ukrainealarm.com"

    async def fetch(self) -> AlertSnapshot:
        payload = await self._get_json(
            f"/api/v3/alerts/{self.region_id}", {"Authorization": self._token}
        )
        return parse_ukrainealarm(payload, self.region_id, datetime.now(UTC))


class AlertsInUaSource(AlertSource):
    """Ліміт API: ~8–10 запитів на хвилину з однієї IP-адреси."""

    default_base_url = "https://api.alerts.in.ua"

    async def fetch(self) -> AlertSnapshot:
        payload = await self._get_json(
            "/v1/alerts/active.json", {"Authorization": f"Bearer {self._token}"}
        )
        return parse_alerts_in_ua(payload, self.region_id, datetime.now(UTC))


_PROVIDERS: dict[AlertProvider, type[AlertSource]] = {
    AlertProvider.UKRAINEALARM: UkraineAlarmSource,
    AlertProvider.ALERTS_IN_UA: AlertsInUaSource,
}


def build_alert_source(settings: Settings) -> AlertSource:
    cfg = settings.alerts
    if cfg.token is None:
        raise ValueError("alerts.token is not configured (BM_ALERTS__TOKEN)")
    return _PROVIDERS[cfg.provider](
        f"alerts:{cfg.provider}",
        cfg.provider.kind,
        cfg.poll_interval,
        token=cfg.token.get_secret_value(),
        region_id=cfg.region_id,
        base_url=cfg.base_url,
    )
