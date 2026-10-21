from __future__ import annotations

import json

import pytest

from scada import emlbox
from scada.config import Config


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: требует собранный emlbox из mime-os")


@pytest.fixture(scope="session")
def emlbox_bin() -> str:
    try:
        return emlbox.emlbox_bin()
    except emlbox.EmlboxMissing as e:  # pragma: no cover - зависит от машины
        pytest.skip(str(e))


@pytest.fixture()
def store(tmp_path, emlbox_bin):
    path = tmp_path / "store"
    path.mkdir()
    return path


@pytest.fixture()
def make_cfg(tmp_path):
    """Пишет частичный config.json (мержится с дефолтами) и грузит Config."""
    def _make(**sections) -> Config:
        path = tmp_path / "cfgstore"
        path.mkdir(exist_ok=True)
        (path / "config.json").write_text(json.dumps(sections), encoding="utf-8")
        return Config.load(path)
    return _make


def write_config(store, **sections) -> Config:
    store.mkdir(parents=True, exist_ok=True)
    (store / "config.json").write_text(json.dumps(sections), encoding="utf-8")
    return Config.load(store)
