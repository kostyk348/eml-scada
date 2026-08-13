"""controllers.py — симуляторы контроллеров (MIME-сущности на IPC-шине).

Каждый контроллер: поток, каждую секунду генерит точки (random walk к цели)
и шлёт TELEMETRY на шину (To: scada_engine). Слушает команды (To: <cid>):
SETPOINT — меняет цель точки, отвечает ACK отправителю.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from .emlbox import bus_list, bus_consume, bus_send, ENGINE_ADDR

log = logging.getLogger("scada.ctl")

# ctl_id -> {point: dict(init, target, min, max, step, noise, unit, digits)}
CTL_SPECS: dict[str, dict] = {
    "ctl_01": {
        "temp":     {"init": 21.0, "target": 21.0, "lo": 0.0, "hi": 60.0, "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
        "pressure": {"init": 2.5,  "target": 2.5,  "lo": 0.0, "hi": 6.0,  "step": 0.05, "noise": 0.1, "unit": "MPa", "digits": 2},
        "level":    {"init": 50.0, "target": 50.0, "lo": 0.0, "hi": 100.0, "step": 1.5, "noise": 2.0, "unit": "%", "digits": 1},
    },
    "ctl_02": {
        "temp":      {"init": 24.0, "target": 24.0, "lo": 0.0, "hi": 60.0, "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
        "valve_pos": {"init": 40.0, "target": 40.0, "lo": 0.0, "hi": 100.0, "step": 4.0, "noise": 1.5, "unit": "%", "digits": 1},
        "flow":      {"init": 30.0, "target": 30.0, "lo": 0.0, "hi": 100.0, "step": 2.0, "noise": 2.5, "unit": "m³/h", "digits": 1},
    },
    "ctl_03": {
        "temp":  {"init": 18.0, "target": 18.0, "lo": 0.0, "hi": 60.0, "step": 0.4, "noise": 0.8, "unit": "°C", "digits": 1},
        "level": {"init": 30.0, "target": 30.0, "lo": 0.0, "hi": 100.0, "step": 1.5, "noise": 2.0, "unit": "%", "digits": 1},
    },
}

# Периодический «отказ» ctl_01: всплеск temp на +15 на несколько тиков (для демо аларма)
FAULT_SPIKE_EVERY = 15  # тиков
FAULT_SPIKE_TICKS = 6
FAULT_SPIKE_DELTA = 15.0


class Controller(threading.Thread):
    def __init__(self, cid: str, bus: Path, tick_s: float = 1.0, stop: threading.Event | None = None):
        super().__init__(daemon=True, name=f"ctl:{cid}")
        self.cid = cid
        self.bus = Path(bus)
        self.tick_s = tick_s
        self.stop = stop or threading.Event()
        self.points: dict[str, dict] = {}
        for pname, spec in CTL_SPECS[cid].items():
            self.points[pname] = dict(spec, value=spec["init"])
        self._tick_n = 0
        self._fault_active = 0
        self.targets: dict[str, float] = {}  # переопределённые цели (по SETPOINT)
        self.last_ack: dict = {}

    # ------------------------------------------------------------- симуляция
    def _step_point(self, pname: str, p: dict) -> None:
        target = self.targets.get(pname, p["target"])
        p["value"] += (target - p["value"]) * 0.15 + random.uniform(-p["noise"], p["noise"])
        p["value"] = max(p["lo"], min(p["hi"], p["value"]))

    def _apply_fault(self) -> None:
        # всплеск temp на ctl_01: имитация отказа датчика/процесса
        if self._fault_active > 0:
            self._fault_active -= 1
            p = self.points.get("temp")
            if p:
                p["value"] = min(p["hi"], p["value"] + FAULT_SPIKE_DELTA * 0.4)

    def _read_commands(self) -> None:
        for m in bus_list(self.bus):
            if m["to"] != self.cid:
                continue  # чужая почта: не трогаем (движок/другие контроллеры)
            if m["event"] == "SETPOINT":
                point = m["body"].get("point")
                value = m["body"].get("value")
                if point in self.points:
                    self.targets[point] = float(value)
                    self.last_ack = {"point": point, "value": float(value), "ack": True, "ts": time.time()}
                    bus_send(self.bus, self.cid, m["from"], "SETPOINT_ACK", self.last_ack)
                    log.info("%s: SETPOINT %s -> %.1f", self.cid, point, float(value))
            bus_consume(m["path"])

    def _emit_telemetry(self) -> None:
        values = {pname: round(p["value"], p["digits"]) for pname, p in self.points.items()}
        quality = "bad" if (self._tick_n % 61 == 0 and self.cid == "ctl_01") else "good"
        bus_send(self.bus, self.cid, ENGINE_ADDR, "TELEMETRY",
                 {"cid": self.cid, "points": values, "quality": quality, "ts": time.time()})

    # ------------------------------------------------------------- поток
    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._tick_n += 1
                if self._tick_n % FAULT_SPIKE_EVERY == 0 and self.cid == "ctl_01":
                    self._fault_active = FAULT_SPIKE_TICKS
                self._apply_fault()
                for pname, p in self.points.items():
                    self._step_point(pname, p)
                self._read_commands()
                self._emit_telemetry()
            except Exception as e:  # поток не должен умирать
                log.warning("%s tick error: %s", self.cid, e)
            self.stop.wait(self.tick_s)

    def setpoint(self, point: str, value: float) -> dict:
        """Внешний вызов (для HMI-кнопки): вернуть последний ACK."""
        bus_send(self.bus, ENGINE_ADDR, self.cid, "SETPOINT",
                 {"point": point, "value": value, "from_hmi": True, "ts": time.time()})
        return {"sent": point, "value": value}
