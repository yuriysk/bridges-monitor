from typing import Any

import aiohttp

from bridges_monitor.sources.base import SourceError

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_DETAIL_CHARS = 200


def api_error_detail(body: Any) -> str | None:
    """Пояснення помилки з тіла відповіді у форматах TomTom, Google, HERE, Mapbox."""
    if not isinstance(body, dict):
        return None
    candidates = [
        body.get("error"),
        (body.get("error") or {}).get("message")
        if isinstance(body.get("error"), dict)
        else None,
        body.get("message"),
        body.get("title"),
    ]
    return next((c for c in candidates if isinstance(c, str) and c), None)


class HttpClient:
    """JSON-клієнт для джерел: лінива сесія, усі збої — SourceError.

    Повідомлення про помилки не містять URL-параметрів: деякі API передають
    ключ у query (TomTom, HERE, Mapbox), і він не повинен потрапити в логи.
    """

    def __init__(self, source_id: str, base_url: str):
        self.source_id = source_id
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        try:
            async with self._session.request(
                method, self.base_url + path, params=params, headers=headers, json=json
            ) as resp:
                if resp.status >= 400:
                    message = (
                        f"{self.source_id}: HTTP {resp.status} for {method} {path}"
                    )
                    try:
                        detail = api_error_detail(await resp.json(content_type=None))
                    except ValueError:
                        detail = None
                    if detail:
                        for secret in (
                            *(params or {}).values(),
                            *(headers or {}).values(),
                        ):
                            if len(secret) >= 8:
                                detail = detail.replace(secret, "***")
                        message += f": {detail[:MAX_DETAIL_CHARS]}"
                    raise SourceError(message)
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise SourceError(
                f"{self.source_id}: {type(e).__name__} for {method} {path}"
            ) from None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
