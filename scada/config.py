"""config.py — единая конфигурация EML-SCADA (JSON, с дефолтами и валидацией).

Приоритет: CLI `--config` > `<store>/config.json` > встроенные дефолты.
Файл `<store>/config.json` создаётся при первом запуске, чтобы его можно было
править руками: пороги, мёртвая зона, ретенция, ротация станции, спеки
симуляторов.

Все значения проверяются на входе: конфиг с опечаткой падает громко
(ConfigError), а не превращается в тихо неверное поведение.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CONFIG_NAME = "config.json"
CONFIG_VERSION = 1


class ConfigError(ValueError):
    """Некорректная конфигурация."""


# ---------------------------------------------------------------- дефолты
DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "engine": {
        "tick_s": 0.25,
        "stale_after_s": 3.0,
    },
    "thresholds": {
        "temp": {"low": 5.0, "high": 30.0, "deadband": 0.5},
        "pressure": {"low": 0.5, "high": 4.0, "deadband": 0.05},
        "level": {"low": 10.0, "high": 90.0, "deadband": 1.0},
        "flow": {"low": None, "high": 80.0, "deadband": 1.0},
        "valve_pos": {"low": None, "high": None, "deadband": 0.0},
    },
    "alarms": {
        "realarm_s": 300.0,   # повторное напоминание по активному аларму
        "max_items": 500,     # сколько алармов держим в памяти HMI
    },
    "historian": {
        "keep_hours": 24.0,
        "max_records": 200_000,
        "prune_every_s": 60.0,
    },
    "bus": {
        "done_ttl_s": 600.0,  # сколько живут .done-файлы в спуле
        "tmp_ttl_s": 60.0,    # осиротевшие .tmp от прерванной публикации
    },
    "station": {
        "name": "station_01",
        "rotate_after_deltas": 20_000,
        "state_persist_s": 15.0,
    },
    "integrity": {
        "anchor_every_s": 300.0,
        "anchor_on_verify": True,
    },
    "hmi": {
        "host": "127.0.0.1",
        "port": 8080,
        "refresh_s": 2.0,
    },
    "sim": {
        "tick_s": 1.0,
        "faults_enabled": True,
        "fault_cid": "ctl_01",
        "fault_every_ticks": 15,
        "fault_ticks": 6,
        "fault_delta": 15.0,
        "badq_every_ticks": 61,
    },
    "controllers": {
        "ctl_01": {
            "temp": {"init": 21.0, "target": 21.0, "lo": 0.0, "hi": 60.0,
                     "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
            "pressure": {"init": 2.5, "target": 2.5, "lo": 0.0, "hi": 6.0,
                         "step": 0.05, "noise": 0.1, "unit": "MPa", "digits": 2},
            "level": {"init": 50.0, "target": 50.0, "lo": 0.0, "hi": 100.0,
                      "step": 1.5, "noise": 2.0, "unit": "%", "digits": 1},
        },
        "ctl_02": {
            "temp": {"init": 24.0, "target": 24.0, "lo": 0.0, "hi": 60.0,
                     "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
            "valve_pos": {"init": 40.0, "target": 40.0, "lo": 0.0, "hi": 100.0,
                          "step": 4.0, "noise": 1.5, "unit": "%", "digits": 1},
            "flow": {"init": 30.0, "target": 30.0, "lo": 0.0, "hi": 100.0,
                     "step": 2.0, "noise": 2.5, "unit": "m³/h", "digits": 1},
        },
        "ctl_03": {
            "temp": {"init": 18.0, "target": 18.0, "lo": 0.0, "hi": 60.0,
                     "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
            "level": {"init": 30.0, "target": 30.0, "lo": 0.0, "hi": 100.0,
                      "step": 1.5, "noise": 2.0, "unit": "%", "digits": 1},
        },
    },
}


@dataclass(frozen=True)
class Threshold:
    """Порог точки: выход за границы = аларм, возврат с мёртвой зоной = сброс."""

    low: float | None = None
    high: float | None = None
    deadband: float = 0.0

    def level_for(self, value: float) -> str | None:
        if self.high is not None and value > self.high:
            return "HIGH"
        if self.low is not None and value < self.low:
            return "LOW"
        return None

    def cleared(self, value: float) -> bool:
        """Значение вернулось в норму с учётом мёртвой зоны (антидребезг)."""
        if self.high is not None and value > self.high - self.deadband:
            return False
        if self.low is not None and value < self.low + self.deadband:
            return False
        return True


def _merge(base: dict, override: Mapping) -> dict:
    """Глубокий merge: dict-ы сливаются, остальное заменяется."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: ожидалось число, получено {value!r}")
    return float(value)


def _opt_num(value: Any, where: str) -> float | None:
    return None if value is None else _num(value, where)


def _validate(raw: dict) -> None:
    eng = raw["engine"]
    if _num(eng["tick_s"], "engine.tick_s") <= 0:
        raise ConfigError("engine.tick_s: должно быть > 0")
    if _num(eng["stale_after_s"], "engine.stale_after_s") <= 0:
        raise ConfigError("engine.stale_after_s: должно быть > 0")

    for name, th in raw["thresholds"].items():
        where = f"thresholds.{name}"
        low = _opt_num(th.get("low"), f"{where}.low")
        high = _opt_num(th.get("high"), f"{where}.high")
        db = _num(th.get("deadband", 0.0), f"{where}.deadband")
        if db < 0:
            raise ConfigError(f"{where}.deadband: должно быть >= 0")
        if low is not None and high is not None and low >= high:
            raise ConfigError(f"{where}: low ({low}) должен быть меньше high ({high})")
        # оба None — точка без алармов (например, положение клапана); это допустимо

    alarms = raw["alarms"]
    if _num(alarms["realarm_s"], "alarms.realarm_s") < 0:
        raise ConfigError("alarms.realarm_s: должно быть >= 0")
    if int(alarms["max_items"]) < 1:
        raise ConfigError("alarms.max_items: должно быть >= 1")

    hist = raw["historian"]
    if _num(hist["keep_hours"], "historian.keep_hours") <= 0:
        raise ConfigError("historian.keep_hours: должно быть > 0")
    if int(hist["max_records"]) < 1:
        raise ConfigError("historian.max_records: должно быть >= 1")
    if _num(hist["prune_every_s"], "historian.prune_every_s") <= 0:
        raise ConfigError("historian.prune_every_s: должно быть > 0")

    bus = raw["bus"]
    if _num(bus["done_ttl_s"], "bus.done_ttl_s") < 0:
        raise ConfigError("bus.done_ttl_s: должно быть >= 0")
    if _num(bus["tmp_ttl_s"], "bus.tmp_ttl_s") < 0:
        raise ConfigError("bus.tmp_ttl_s: должно быть >= 0")

    station = raw["station"]
    if not str(station["name"]).strip():
        raise ConfigError("station.name: пустое имя")
    if int(station["rotate_after_deltas"]) < 100:
        raise ConfigError("station.rotate_after_deltas: должно быть >= 100")
    if _num(station["state_persist_s"], "station.state_persist_s") <= 0:
        raise ConfigError("station.state_persist_s: должно быть > 0")

    integ = raw["integrity"]
    if _num(integ["anchor_every_s"], "integrity.anchor_every_s") <= 0:
        raise ConfigError("integrity.anchor_every_s: должно быть > 0")

    hmi = raw["hmi"]
    if not 1 <= int(hmi["port"]) <= 65535:
        raise ConfigError("hmi.port: вне диапазона 1..65535")
    if _num(hmi["refresh_s"], "hmi.refresh_s") <= 0:
        raise ConfigError("hmi.refresh_s: должно быть > 0")

    if not raw["controllers"]:
        raise ConfigError("controllers: пустой список")
    sim = raw["sim"]
    if _num(sim["tick_s"], "sim.tick_s") <= 0:
        raise ConfigError("sim.tick_s: должно быть > 0")
    for key in ("fault_every_ticks", "fault_ticks", "badq_every_ticks"):
        if int(sim[key]) < 0:
            raise ConfigError(f"sim.{key}: должно быть >= 0")
    if _num(sim["fault_delta"], "sim.fault_delta") < 0:
        raise ConfigError("sim.fault_delta: должно быть >= 0")
    if bool(sim["faults_enabled"]) and str(sim["fault_cid"]) not in raw["controllers"]:
        raise ConfigError(f"sim.fault_cid: {sim['fault_cid']!r} нет в controllers")
    for cid, points in raw["controllers"].items():
        if not points:
            raise ConfigError(f"controllers.{cid}: нет точек")
        for pname, spec in points.items():
            where = f"controllers.{cid}.{pname}"
            lo = _num(spec["lo"], f"{where}.lo")
            hi = _num(spec["hi"], f"{where}.hi")
            if lo >= hi:
                raise ConfigError(f"{where}: lo должен быть меньше hi")
            init = _num(spec["init"], f"{where}.init")
            if not lo <= init <= hi:
                raise ConfigError(f"{where}.init: {init} вне [{lo}, {hi}]")
            if not lo <= _num(spec["target"], f"{where}.target") <= hi:
                raise ConfigError(f"{where}.target вне [{lo}, {hi}]")
            if _num(spec["noise"], f"{where}.noise") < 0:
                raise ConfigError(f"{where}.noise: должно быть >= 0")
            if int(spec["digits"]) < 0:
                raise ConfigError(f"{where}.digits: должно быть >= 0")


class Config:
    """Тонкая обёртка над валидированным dict с точечным доступом."""

    def __init__(self, raw: dict, source: Path | None = None):
        self.raw = raw
        self.source = source

    # --- фабрики
    @classmethod
    def load(cls, store: Path, path: Path | None = None) -> "Config":
        explicit = path is not None
        cfg_path = Path(path) if explicit else Path(store) / CONFIG_NAME
        raw = copy.deepcopy(DEFAULTS)
        source = None
        if cfg_path.exists():
            try:
                # utf-8-sig: Блокнот и PowerShell пишут BOM, конфиг не должен из-за этого падать
                user = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as e:
                raise ConfigError(f"{cfg_path}: невалидный JSON ({e})") from e
            if not isinstance(user, dict):
                raise ConfigError(f"{cfg_path}: ожидался объект JSON")
            raw = _merge(raw, user)
            source = cfg_path
        elif explicit:
            raise ConfigError(f"{cfg_path}: файл не найден")
        _validate(raw)
        return cls(raw, source)

    def write_if_missing(self, store: Path) -> Path:
        """Кладёт дефолтный конфиг рядом со стором (самодокументируемый)."""
        path = Path(store) / CONFIG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                json.dumps(self.raw, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return path

    # --- доступ
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def thresholds(self) -> dict[str, Threshold]:
        return {name: Threshold(
            low=th.get("low"),
            high=th.get("high"),
            deadband=float(th.get("deadband", 0.0)),
        ) for name, th in self.raw["thresholds"].items()}

    @property
    def controllers(self) -> dict[str, dict[str, dict]]:
        return self.raw["controllers"]

    @property
    def tick_s(self) -> float:
        return float(self.get("engine.tick_s"))

    @property
    def stale_after_s(self) -> float:
        return float(self.get("engine.stale_after_s"))

    def to_json(self) -> str:
        return json.dumps(self.raw, indent=2, ensure_ascii=False)
