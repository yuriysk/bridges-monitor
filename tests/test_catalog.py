import tomllib

import pytest
from pydantic import ValidationError

from bridges_monitor.catalog import (
    CATALOG_RESOURCE,
    Catalog,
    TransportMode,
    load_catalog,
)


def test_bundled_catalog_is_valid():
    catalog = load_catalog()
    assert len(catalog.bridges) == 6
    for bridge in catalog.bridges:
        assert bridge.name.get("en"), f"{bridge.id}: missing English name"
        assert bridge.in_service(TransportMode.ROAD)


def test_metro_in_service():
    catalog = load_catalog()
    podil = catalog.bridge("bridge-podil")
    assert not podil.in_service(TransportMode.METRO)
    assert catalog.bridge("bridge-pivden").in_service(TransportMode.METRO)
    assert not catalog.bridge("bridge-paton").in_service(TransportMode.METRO)


def _raw() -> dict:
    return tomllib.loads(CATALOG_RESOURCE.read_text())


def test_rejects_bridge_without_road():
    raw = _raw()
    raw["bridges"][0]["modes"] = ["metro"]
    with pytest.raises(ValidationError, match="must carry road"):
        Catalog.model_validate(raw)


def test_rejects_not_in_service_outside_modes():
    raw = _raw()
    raw["bridges"][0]["not_in_service"] = ["metro"]
    raw["bridges"][0]["modes"] = ["road"]
    with pytest.raises(ValidationError, match="not_in_service"):
        Catalog.model_validate(raw)


def test_rejects_duplicate_ids():
    raw = _raw()
    raw["bridges"].append(dict(raw["bridges"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        Catalog.model_validate(raw)


def test_requires_ukrainian_name():
    raw = _raw()
    raw["bridges"][0]["name"] = {"en": "Only English"}
    with pytest.raises(ValidationError, match="must include 'uk'"):
        Catalog.model_validate(raw)
