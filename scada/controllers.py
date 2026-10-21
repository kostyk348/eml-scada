"""controllers.py — симуляторы контроллеров (MIME-сущности на шине).

Контроллер живёт в своём потоке: раз в `sim.tick_s` двигает точки (slew к цели
+ шум), отправляет TELEMETRY на `scada_engine` и читает свою почту.
SETPOINT валидируется по границам точки: подтверждение (SETPOINT_ACK) несёт и
запрошенное, и применённое значение; на мусор отвечает SETPOINT_REJECT.

Дефекты (всплеск температуры + bad quality) включаются из конфига `sim.*` —
это же используют тесты, чтобы не ждать 15 тиков.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from .config import Config
from .emlbox import ENGINE_ADDR, bus_consume, bus_list, bus_send

log = logging.getLogger("scada.ctl")


class Controller(threading.Thread):
    def __init__(self, cid: str, bus: Path, cfg: Config,
                 stop: threading.Event | None = None):
        super().__init__(daemon=True, name=f"ctl:{cid}")
        self.cid = cid
        self.bus = Path(bus)
        self.cfg = cfg
        self.tick_s = float(cfg.get("sim.tick_s"))
        self.stop = stop or threading.Event()

        specs = cfg.controllers[cid]
        self.points: dict[str, dict] = {p: dict(spec, value=float(spec["init"]))
                                        for p, spec in specs.items()}
        self.targets: dict[str, float] = {}
        self.last_ack: dict = {}
        self.rejects = 0
        self.sent = 0

        sim = cfg.get("sim", {})
        self.faults_enabled = bool(sim.get("faults_enabled", True)) and sim.get("fault_cid") == cid
        self.fault_every = int(sim.get("fault_every_ticks", 0))
        self.fault_ticks = int(sim.get("fault_ticks", 0))
        self.fault_delta = float(sim.get("fault_delta", 0.0))
        # bad quality — часть имитации дефектов: только у fault_cid, иначе BADQ
        # на каждом контроллере каждые N тиков превращается в шум
        self.badq_every = int(sim.get("badq_every_ticks", 0)) if self.faults_enabled else 0

        self._tick_n = 0
        self._fault_active = 0

    # ------------------------------------------------------------- симуляция
    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _step_point(self, p: dict) -> None:
        target = float(p.get("target", p["value"]))
        slew = (target - p["value"]) * 0.15
        step = float(p.get("step", abs(slew) or 1.0))
        slew = self._clamp(slew, -step, step)
        noise = random.uniform(-float(p["noise"]), float(p["noise"]))
        p["value"] = self._clamp(p["value"] + slew + noise, float(p["lo"]), float(p["hi"]))

    def _apply_fault(self) -> None:
        if self._fault_active <= 0:
            return
        self._fault_active -= 1
        p = self.points.get("temp")
        if p:
            p["value"] = min(float(p["hi"]), p["value"] + self.fault_delta * 0.4)

    def _quality(self) -> str:
        if self.badq_every and self._tick_n % self.badq_every == 0:
            return "bad"
        return "good"

    # ------------------------------------------------------------- команды
    def _read_commands(self) -> None:
        for m in bus_list(self.bus, address=self.cid):
            try:
                if m["event"] == "SETPOINT":
                    self._on_setpoint(m)
            finally:
                bus_consume(m["path"])

    def _on_setpoint(self, m: dict) -> None:
        body = m["body"]
        point = body.get("point")
        try:
            value = float(body.get("value"))
        except (TypeError, ValueError):
            value = None
        spec = self.points.get(point) if isinstance(point, str) else None
        reason = None
        if spec is None:
            reason = f"unknown point {point!r}"
        elif value is None:
            reason = "value is not a number"
        elif not float(spec["lo"]) <= value <= float(spec["hi"]):
            reason = f"value {value} out of [{spec['lo']}, {spec['hi']}]"

        if reason is not None:
            self.rejects += 1
            bus_send(self.bus, self.cid, m["from"], "SETPOINT_REJECT",
                     {"point": point, "value": body.get("value"), "reason": reason,
                      "cid": self.cid, "ts": time.time()})
            log.warning("%s: SETPOINT отклонён (%s)", self.cid, reason)
            return

        request = {"from": m["from"], "ts": body.get("ts")}
        self.targets[point] = value
        self.points[point]["target"] = value
        self.last_ack = {"point": point, "value": value, "applied": value,
                         "ok": True, "cid": self.cid, "request": request,
                         "ack": True, "ts": time.time()}
        bus_send(self.bus, self.cid, m["from"], "SETPOINT_ACK", self.last_ack)
        log.info("%s: SETPOINT %s -> %.3g", self.cid, point, value)

    # ------------------------------------------------------------- телеметрия
    def _emit_telemetry(self) -> None:
        values = {pname: round(p["value"], int(p["digits"]))
                  for pname, p in self.points.items()}
        bus_send(self.bus, self.cid, ENGINE_ADDR, "TELEMETRY",
                 {"cid": self.cid, "points": values,
                  "quality": self._quality(), "ts": time.time()})
        self.sent += 1

    # ------------------------------------------------------------- поток
    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._tick_n += 1
                if self.faults_enabled and self.fault_every and self._tick_n % self.fault_every == 0:
                    self._fault_active = self.fault_ticks
                self._apply_fault()
                for p in self.points.values():
                    self._step_point(p)
                self._read_commands()
                self._emit_telemetry()
            except Exception as e:  # поток не должен умирать
                log.warning("%s tick error: %s", self.cid, e)
            self.stop.wait(self.tick_s)

    # ------------------------------------------------------------- API
    def setpoint(self, point: str, value: float, by: str = "hmi") -> dict:
        """Внешняя отправка (для тестов/скриптов): вернуть, что отправили."""
        req = {"point": point, "value": value, "from_hmi": True,
               "by": by, "ts": time.time()}
        bus_send(self.bus, ENGINE_ADDR, self.cid, "SETPOINT", req)
        return req
