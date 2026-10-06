import os

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Справжні .env, config.toml, /run/secrets і змінні BM_* не впливають на тести."""
    for name in list(os.environ):
        if name.upper().startswith("BM_"):
            monkeypatch.delenv(name)
    workdir = tmp_path / "work"
    secrets = tmp_path / "secrets"
    workdir.mkdir()
    secrets.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("BM_SECRETS_DIR", str(secrets))
    return workdir, secrets
