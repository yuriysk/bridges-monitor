from collections.abc import Sequence

from bridges_monitor.config import Settings, TrafficProvider
from bridges_monitor.geometry import RoadProbe
from bridges_monitor.sources.traffic.base import ProbeReading, TrafficSource
from bridges_monitor.sources.traffic.google import GoogleRoutesTrafficSource
from bridges_monitor.sources.traffic.here import HereTrafficSource
from bridges_monitor.sources.traffic.mapbox import MapboxTrafficSource
from bridges_monitor.sources.traffic.tomtom import TomTomTrafficSource

PROVIDERS: dict[TrafficProvider, type[TrafficSource]] = {
    TrafficProvider.TOMTOM: TomTomTrafficSource,
    TrafficProvider.HERE: HereTrafficSource,
    TrafficProvider.GOOGLE: GoogleRoutesTrafficSource,
    TrafficProvider.MAPBOX: MapboxTrafficSource,
}


def build_traffic_sources(
    settings: Settings, probes: Sequence[RoadProbe]
) -> list[TrafficSource]:
    sources = []
    for provider, cfg in settings.traffic.items():
        if not cfg.enabled:
            continue
        if cfg.token is None:
            raise ValueError(
                f"traffic.{provider}.token is not configured "
                f"(BM_TRAFFIC__{provider.upper()}__TOKEN)"
            )
        sources.append(
            PROVIDERS[provider](
                f"traffic:{provider}",
                cfg.kind,
                cfg.schedule or settings.traffic_schedule,
                token=cfg.token.get_secret_value(),
                probes=probes,
                base_url=cfg.base_url,
            )
        )
    return sources


__all__ = ["ProbeReading", "TrafficSource", "build_traffic_sources"]
