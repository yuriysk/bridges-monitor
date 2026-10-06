"""Довідник автомобільних мостів Києва.

Міст — один об'єкт з назвою кількома мовами й видами транспорту: автомобільний
рух є на кожному мосту, метро — на деяких. Стан руху ведеться для трійки
(міст, вид транспорту, напрямок) — див. state.py.
"""

import tomllib
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

DEFAULT_LANGUAGE = "uk"
CATALOG_RESOURCE = files("bridges_monitor") / "data" / "bridges.toml"


class TransportMode(StrEnum):
    ROAD = "road"
    METRO = "metro"


class Direction(StrEnum):
    TO_LEFT_BANK = "to_left_bank"
    TO_RIGHT_BANK = "to_right_bank"


def _require_default_language(names: dict[str, str]) -> dict[str, str]:
    if not names.get(DEFAULT_LANGUAGE):
        raise ValueError(f"name must include '{DEFAULT_LANGUAGE}'")
    return names


LocalizedText = Annotated[dict[str, str], AfterValidator(_require_default_language)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bridge(_Model):
    id: str
    name: LocalizedText
    modes: frozenset[TransportMode]
    # Види транспорту, які конструктивно є, але ще не введені в експлуатацію.
    not_in_service: frozenset[TransportMode] = frozenset()

    @model_validator(mode="after")
    def _consistent(self) -> Bridge:
        if TransportMode.ROAD not in self.modes:
            raise ValueError(f"{self.id}: every bridge must carry road traffic")
        if extra := self.not_in_service - self.modes:
            raise ValueError(
                f"{self.id}: not_in_service has modes not in modes: {sorted(extra)}"
            )
        return self

    def in_service(self, mode: TransportMode) -> bool:
        return mode in self.modes and mode not in self.not_in_service


class Catalog(_Model):
    bridges: tuple[Bridge, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> Catalog:
        if len({b.id for b in self.bridges}) != len(self.bridges):
            raise ValueError("duplicate bridge id")
        return self

    def bridge(self, bridge_id: str) -> Bridge:
        return next(b for b in self.bridges if b.id == bridge_id)


def load_catalog(path: Path | None = None) -> Catalog:
    raw = path.read_bytes() if path else CATALOG_RESOURCE.read_bytes()
    return Catalog.model_validate(tomllib.loads(raw.decode()))
