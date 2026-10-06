"""Командний рядок: діагностичні команди для ручної перевірки джерел."""

import argparse
import asyncio
import contextlib
import logging
import sys

from pydantic import ValidationError

from bridges_monitor.catalog import load_catalog
from bridges_monitor.config import Settings, TrafficProvider
from bridges_monitor.geometry import load_road_probes
from bridges_monitor.monitor import Monitor
from bridges_monitor.publishing import LogPublisher
from bridges_monitor.sources.alerts import build_alert_source
from bridges_monitor.sources.base import SourceError
from bridges_monitor.sources.traffic import build_traffic_sources

log = logging.getLogger("bridges_monitor")

DIRECTION_LABELS = {"to_left_bank": "→ лівий", "to_right_bank": "→ правий"}


async def _traffic(provider: TrafficProvider) -> int:
    settings = Settings()
    catalog = load_catalog()
    sources = [
        s
        for s in build_traffic_sources(settings, load_road_probes(catalog))
        if s.source_id == f"traffic:{provider}"
    ]
    if not sources:
        print(f"Провайдер {provider} не ввімкнено в config.toml", file=sys.stderr)
        return 2
    (source,) = sources
    names = {b.id: b.name["uk"] for b in catalog.bridges}

    async with source:
        results = await asyncio.gather(
            *(source.read(p) for p in source.probes), return_exceptions=True
        )

    failures = 0
    for probe, result in zip(source.probes, results, strict=True):
        label = f"{names[probe.bridge]:30} {DIRECTION_LABELS[probe.direction]:9}"
        if isinstance(result, SourceError):
            failures += 1
            print(f"{label} помилка: {result}")
        elif isinstance(result, BaseException):
            raise result
        elif result is None:
            print(
                f"{label} немає надійних даних (сегмент не збігся з мостом, маршрут в об'їзд або рівень невідомий)"
            )
        elif result.closed:
            print(f"{label} ЗАКРИТО")
        else:
            speed = (
                f"{result.speed_kmh:5.0f} / {result.free_flow_kmh:3.0f} км/год"
                if result.speed_kmh is not None and result.free_flow_kmh is not None
                else ""
            )
            print(f"{label} {result.congestion:10} {speed}")
    return 1 if failures == len(results) else 0


async def _alerts() -> int:
    settings = Settings()
    async with build_alert_source(settings) as source:
        try:
            snapshot = await source.fetch()
        except SourceError as e:
            print(f"помилка: {e}", file=sys.stderr)
            return 1
    print(
        f"Провайдер: {settings.alerts.provider} ({source.kind}), "
        f"регіон {source.region_id}"
    )
    if snapshot.active:
        started = (
            f", з {snapshot.started_at.astimezone(source.schedule.tz):%d.%m %H:%M}"
            if snapshot.started_at
            else ""
        )
        print(f"Повітряна тривога: Є{started}")
    else:
        print("Повітряна тривога: немає")
    return 0


async def _run() -> int:
    settings = Settings()
    catalog = load_catalog()
    alert_source = None
    if settings.alerts.token is None:
        log.warning(
            "BM_ALERTS__TOKEN не задано: працюю без даних про тривоги "
            "(розклади в режимі «без тривоги»)"
        )
    else:
        alert_source = build_alert_source(settings)
    sources = build_traffic_sources(settings, load_road_probes(catalog))
    log.info(
        "Версія %s, модерація %s; тривоги: %s; затори: %s",
        settings.edition,
        "увімкнена" if settings.moderation_enabled else "вимкнена",
        alert_source.source_id if alert_source else "—",
        ", ".join(s.source_id for s in sources) or "—",
    )
    monitor = Monitor(
        settings,
        catalog,
        alert_source=alert_source,
        sources=sources,
        publisher=LogPublisher(catalog),
    )
    await monitor.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridges-monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    traffic = commands.add_parser(
        "traffic", help="одноразово опитати провайдера заторів і показати результат"
    )
    traffic.add_argument(
        "provider", type=TrafficProvider, choices=list(TrafficProvider)
    )
    commands.add_parser(
        "alerts", help="одноразово опитати джерело тривог і показати стан"
    )
    commands.add_parser("run", help="запустити моніторинг (зупинка — Ctrl+C)")
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (ValidationError, ValueError) as e:
        # Помилки конфігу (токени в них не потрапляють: hide_input_in_errors).
        print(f"Помилка конфігурації: {e}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "traffic":
        return asyncio.run(_traffic(args.provider))
    if args.command == "alerts":
        return asyncio.run(_alerts())
    if args.command == "run":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        with contextlib.suppress(KeyboardInterrupt):
            return asyncio.run(_run())
        return 0
    return 2
