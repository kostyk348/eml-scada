from __future__ import annotations

import json

import pytest

from scada.config import DEFAULTS, Config, ConfigError, Threshold


def test_defaults_are_valid(tmp_path):
    cfg = Config.load(tmp_path)
    assert cfg.tick_s == DEFAULTS["engine"]["tick_s"]
    assert cfg.thresholds["temp"].high == 30.0
    assert cfg.thresholds["flow"].low is None
    assert set(cfg.controllers) == {"ctl_01", "ctl_02", "ctl_03"}
    assert cfg.get("sim.faults_enabled") is True


def test_write_if_missing_roundtrip(tmp_path):
    cfg = Config.load(tmp_path)
    path = cfg.write_if_missing(tmp_path)
    assert path.exists()
    loaded = Config.load(tmp_path)
    assert loaded.raw == cfg.raw
    assert loaded.source == path


def test_partial_override_merges(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"engine": {"tick_s": 0.5}, "thresholds": {"temp": {"high": 40.0}}}), encoding="utf-8")
    cfg = Config.load(tmp_path)
    assert cfg.tick_s == 0.5
    assert cfg.thresholds["temp"].high == 40.0
    assert cfg.thresholds["temp"].low == 5.0          # дефолт сохранён
    assert cfg.thresholds["pressure"].high == 4.0


def test_invalid_configs_fail_loudly(tmp_path):
    def bad(payload):
        (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ConfigError):
            Config.load(tmp_path)

    bad({"thresholds": {"temp": {"low": 40.0, "high": 10.0}}})
    bad({"thresholds": {"temp": {"deadband": -1.0}}})
    bad({"engine": {"tick_s": 0}})
    bad({"hmi": {"port": 99999}})
    bad({"historian": {"keep_hours": 0}})
    bad({"sim": {"fault_cid": "ctl_99"}})
    bad({"controllers": {"ctl_01": {"temp": {"init": 999, "lo": 0, "hi": 60,
                                            "target": 1, "noise": 0, "step": 1, "digits": 1}}}})


def test_explicit_missing_path_fails(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path, tmp_path / "nope.json")


def test_broken_json_fails(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(tmp_path)


def test_utf8_bom_config_loads(tmp_path):
    """Блокнот/PowerShell сохраняют UTF-8 с BOM — конфиг должен читаться."""
    payload = json.dumps({"engine": {"tick_s": 0.5}})
    (tmp_path / "config.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    cfg = Config.load(tmp_path)
    assert cfg.tick_s == 0.5


def test_threshold_deadband_semantics():
    th = Threshold(low=5.0, high=30.0, deadband=1.0)
    assert th.level_for(31.0) == "HIGH"
    assert th.level_for(29.5) is None
    assert th.cleared(30.5) is False      # внутри мёртвой зоны — держим
    assert th.cleared(28.9) is True
    assert th.level_for(4.0) == "LOW"
    assert th.cleared(5.5) is False
    assert th.cleared(6.1) is True
    one_sided = Threshold(low=None, high=80.0, deadband=1.0)
    assert one_sided.level_for(81) == "HIGH"
    assert one_sided.cleared(79.5) is False
    assert one_sided.cleared(78.9) is True
    assert Threshold().level_for(1e9) is None
