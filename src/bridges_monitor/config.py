"""Налаштування застосунку.

Пріоритет джерел (перше перемагає):
1. змінні середовища BM_*;
2. файл .env у робочому каталозі (локальна розробка);
3. файли секретів: ім'я файлу — ім'я змінної (BM_TRAFFIC__TOMTOM__TOKEN), вміст —
   значення; каталог /run/secrets (Docker secrets) або BM_SECRETS_DIR;
4. config.toml у робочому каталозі — лише налаштування без секретів.
"""

import os
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from bridges_monitor.models import SourceKind
from bridges_monitor.schedule import DEFAULT_TRAFFIC_SCHEDULE, PollSchedule

DEFAULT_SECRETS_DIR = Path("/run/secrets")


class SecretFilesSource(EnvSettingsSource):
    """Читає файли каталогу секретів як змінні середовища з тими самими іменами.

    Вбудований SecretsSettingsSource не розбирає вкладені ключі (__), тому
    файли подаються через звичайну логіку EnvSettingsSource.
    """

    def __init__(self, settings_cls: type[BaseSettings], secrets_dir: Path):
        self.secrets_dir = secrets_dir
        super().__init__(settings_cls)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        if not self.secrets_dir.is_dir():
            return {}
        return {
            (f.name if self.case_sensitive else f.name.lower()): f.read_text().strip()
            for f in self.secrets_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        }


class Edition(StrEnum):
    BASIC = "basic"  # лише офіційні джерела
    EXTENDED = "extended"  # офіційні + неофіційні, модерація обов'язкова


class AlertProvider(StrEnum):
    UKRAINEALARM = "ukrainealarm"  # офіційний API «Повітряна тривога»
    ALERTS_IN_UA = "alerts_in_ua"  # неофіційний агрегатор

    @property
    def kind(self) -> SourceKind:
        if self is AlertProvider.UKRAINEALARM:
            return SourceKind.OFFICIAL
        return SourceKind.UNOFFICIAL


class AlertSettings(BaseModel):
    # Не показувати вхідні дані (токени) у текстах помилок валідації.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider: AlertProvider = AlertProvider.UKRAINEALARM
    token: SecretStr | None = None
    region_id: str = "31"  # м. Київ
    poll_interval: timedelta = timedelta(seconds=30)
    base_url: str | None = None  # None — адреса провайдера за замовчуванням


class TrafficProvider(StrEnum):
    TOMTOM = "tomtom"
    HERE = "here"
    GOOGLE = "google"
    MAPBOX = "mapbox"


class TrafficSourceSettings(BaseModel):
    # Не показувати вхідні дані (токени) у текстах помилок валідації.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    token: SecretStr | None = None
    # Комерційні дані про затори за замовчуванням вважаються неофіційними.
    kind: SourceKind = SourceKind.UNOFFICIAL
    # None — спільний Settings.traffic_schedule. Один цикл — по запиту на кожен міст
    # і напрямок (12 для маршрутизаторів), тож розклад визначає витрату квоти.
    schedule: PollSchedule | None = None
    base_url: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BM_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="config.toml",
        extra="forbid",
        frozen=True,
        # Помилка валідації інакше показала б увесь вхідний словник разом із токенами.
        hide_input_in_errors=True,
    )

    edition: Edition = Edition.BASIC
    # None — за замовчуванням версії: вимкнено в базовій, увімкнено в розширеній.
    moderation: bool | None = None
    # Скільки діє твердження без явного терміну дії, якщо його не підтвердили.
    stale_after: timedelta = timedelta(hours=6)
    stale_after_alert: timedelta = timedelta(minutes=30)
    alerts: AlertSettings = AlertSettings()
    # Ключ — провайдер; токен: BM_TRAFFIC__<ПРОВАЙДЕР>__TOKEN.
    traffic: dict[TrafficProvider, TrafficSourceSettings] = {}
    traffic_schedule: PollSchedule = DEFAULT_TRAFFIC_SCHEDULE

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Лише справжня змінна середовища: у .env вона дала б помилку extra="forbid".
        secrets_dir = Path(os.environ.get("BM_SECRETS_DIR", DEFAULT_SECRETS_DIR))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            SecretFilesSource(settings_cls, secrets_dir),
            TomlConfigSettingsSource(settings_cls),
        )

    @model_validator(mode="after")
    def _extended_requires_moderation(self) -> Self:
        if self.edition is Edition.EXTENDED and self.moderation is False:
            raise ValueError("moderation cannot be disabled in the extended edition")
        return self

    @model_validator(mode="after")
    def _basic_requires_official_alerts(self) -> Self:
        if (
            self.edition is Edition.BASIC
            and self.alerts.provider.kind is not SourceKind.OFFICIAL
        ):
            raise ValueError(
                f"alert provider {self.alerts.provider} is unofficial; "
                "not allowed in the basic edition"
            )
        return self

    @model_validator(mode="after")
    def _basic_requires_official_traffic(self) -> Self:
        if self.edition is Edition.BASIC:
            unofficial = [
                str(p)
                for p, cfg in self.traffic.items()
                if cfg.enabled and cfg.kind is not SourceKind.OFFICIAL
            ]
            if unofficial:
                raise ValueError(
                    f"traffic providers {unofficial} are unofficial; "
                    "not allowed in the basic edition"
                )
        return self

    @property
    def moderation_enabled(self) -> bool:
        if self.moderation is None:
            return self.edition is Edition.EXTENDED
        return self.moderation
