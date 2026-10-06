from datetime import timedelta

import pytest
from pydantic import ValidationError

from bridges_monitor.config import Edition, Settings, TrafficProvider


def test_defaults_are_basic_without_moderation():
    settings = Settings()
    assert settings.edition is Edition.BASIC
    assert not settings.moderation_enabled


def test_basic_moderation_is_optional():
    assert Settings(edition=Edition.BASIC, moderation=True).moderation_enabled


def test_extended_moderation_is_on_by_default():
    assert Settings(edition=Edition.EXTENDED).moderation_enabled


def test_extended_moderation_cannot_be_disabled():
    with pytest.raises(ValidationError, match="cannot be disabled"):
        Settings(edition=Edition.EXTENDED, moderation=False)


def test_reads_toml_and_env(isolated_settings, monkeypatch):
    workdir, _ = isolated_settings
    (workdir / "config.toml").write_text(
        'edition = "extended"\nstale_after_alert = "PT15M"\n'
    )
    monkeypatch.setenv("BM_STALE_AFTER", "PT2H")
    settings = Settings()
    assert settings.edition is Edition.EXTENDED
    assert settings.stale_after_alert == timedelta(minutes=15)
    assert settings.stale_after == timedelta(hours=2)


def test_source_priority(isolated_settings, monkeypatch):
    """env > .env > файли секретів > config.toml."""
    workdir, secrets = isolated_settings
    (workdir / "config.toml").write_text(
        '[alerts]\ntoken = "toml"\nregion_id = "toml"\n'
        'base_url = "toml"\npoll_interval = "PT1M"\n'
    )
    (secrets / "BM_ALERTS__TOKEN").write_text("secret\n")
    (secrets / "BM_ALERTS__REGION_ID").write_text("secret")
    (secrets / "BM_ALERTS__BASE_URL").write_text("secret")
    (workdir / ".env").write_text(
        "BM_ALERTS__TOKEN=dotenv\nBM_ALERTS__REGION_ID=dotenv\n"
    )
    monkeypatch.setenv("BM_ALERTS__TOKEN", "env")

    alerts = Settings().alerts
    assert alerts.token.get_secret_value() == "env"
    assert alerts.region_id == "dotenv"
    assert alerts.base_url == "secret"
    assert alerts.poll_interval == timedelta(minutes=1)


def test_nested_traffic_token_from_secret_file(isolated_settings):
    workdir, secrets = isolated_settings
    (workdir / "config.toml").write_text('edition = "extended"\n')
    (secrets / "BM_TRAFFIC__TOMTOM__TOKEN").write_text("tt\n")
    settings = Settings()
    assert settings.traffic[TrafficProvider.TOMTOM].token.get_secret_value() == "tt"


def test_missing_secrets_dir_is_fine(monkeypatch, tmp_path):
    monkeypatch.setenv("BM_SECRETS_DIR", str(tmp_path / "absent"))
    assert Settings().alerts.token is None


def test_typo_in_dotenv_is_rejected(isolated_settings):
    workdir, _ = isolated_settings
    (workdir / ".env").write_text("BM_ALERT__TOKEN=x\n")
    with pytest.raises(ValidationError):
        Settings()


def test_token_is_masked_in_repr(monkeypatch):
    monkeypatch.setenv("BM_ALERTS__TOKEN", "very-secret")
    assert "very-secret" not in repr(Settings())


def test_validation_error_does_not_show_tokens(isolated_settings):
    workdir, secrets = isolated_settings
    (workdir / "config.toml").write_text('[traffic.mapbox]\nkind = "unofficial"\n')
    (secrets / "BM_TRAFFIC__MAPBOX__TOKEN").write_text("pk.SUPER-SECRET-TOKEN-VALUE")
    (workdir / ".env").write_text("BM_ALERTS__TOKEN=ALERTS-SECRET-TOKEN-VALUE\n")
    with pytest.raises(ValidationError) as exc:
        Settings()
    text = str(exc.value)
    assert "unofficial" in text
    assert "SECRET-TOKEN-VALUE" not in text


def test_nested_validation_error_does_not_show_tokens(isolated_settings):
    workdir, _ = isolated_settings
    (workdir / "config.toml").write_text(
        '[alerts]\ntoken = "ALERTS-SECRET-TOKEN-VALUE"\npoll_interval = "bad"\n'
    )
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "SECRET-TOKEN-VALUE" not in str(exc.value)
