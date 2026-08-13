"""engine.py — движок опроса: потребляет TELEMETRY с шины, пишет историк
(tagdb) и состояние станции (KV в контейнере), проверяет пороги алармов.

Все данные — .eml: историк = записи tagdb, станция = контейнер, алармы =
дельты KV в контейнере, события = сообщения на шине.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

from . import emlbox
from .emlbox import (ENGINE_ADDR, HMI_ADDR, bus_consume, bus_list, bus_send,
                     kv_dump, kv_set, tagdb_insert, tagdb_query, verify)

log = logging.getLogger("scada.engine")

# порог: point -> (low, high); None-граница = не проверяется
THRESHOLDS: dict[str, dict] = {
    "temp":     {"low": 5.0, "high": 30.0},
    "pressure": {"low": 0.5, "high": 4.0},
    "level":    {"low": 10.0, "high": 90.0},
    "flow":     {"low": None, "high": 80.0},
    "valve_pos": {"low": None, "high": None},
}

HISTORY_MINUTES = 5


class Engine(threading.Thread):
    def __init__(self, store: Path, stop: threading.Event | None = None, tick_s: float = 0.25):
        super().__init__(daemon=True, name="engine")
        self.store = Path(store)
        self.bus = self.store / "bus"
        self.hist = self.store / "hist"
        self.station = self.store / "station_01.eml"
        self.tick_s = tick_s
        self.stop = stop or threading.Event()

        self.lock = threading.Lock()
        self.latest: dict[str, dict] = {}          # cid -> {point: {value, ts, quality}}
        self.alarms: deque = deque(maxlen=100)      # активные/недавние алармы
        self.traffic: deque = deque(maxlen=60)      # последние события шины
        self.engine_ts: float = time.time()          # время старта (для истории)
        self.alarm_sent_at: dict[tuple, float] = {}  # anti-flood: (cid, point) -> ts

    # ------------------------------------------------------------- setup
    def ensure_layout(self) -> None:
        self.bus.mkdir(parents=True, exist_ok=True)
        self.hist.mkdir(parents=True, exist_ok=True)
        if not self.station.exists():
            emlbox.create(self.station, "station_01@system.local", "EML-SCADA station 01")
            kv_set(self.station, "config", "thresholds", THRESHOLDS)
            kv_set(self.station, "config", "created_at", time.time())
            kv_set(self.station, "state", "init", {"ts": time.time()})

    # ------------------------------------------------------------- обработка
    def _handle(self, m: dict) -> None:
        ev = m["event"]
        if ev == "TELEMETRY":
            self._on_telemetry(m)
        elif ev in ("SETPOINT_ACK", "ALARM_ACK"):
            self.traffic.appendleft({"ts": time.time(), **m})
        else:
            self.traffic.appendleft({"ts": time.time(), **m})

    def _on_telemetry(self, m: dict) -> None:
        body = m["body"]
        cid = body.get("cid", m["from"])
        quality = body.get("quality", "good")
        ts = body.get("ts", time.time())
        self.traffic.appendleft({"ts": time.time(), "cid": cid, "event": "TELEMETRY", "from": m["from"], "to": m["to"]})

        with self.lock:
            self.latest.setdefault(cid, {})
        for pname, value in (body.get("points") or {}).items():
            rec = {"value": value, "ts": ts, "quality": quality}
            with self.lock:
                self.latest[cid][pname] = rec
            # историк: запись tagdb = .eml
            tagdb_insert(self.hist, {"value": value, "quality": quality},
                         rec_id=f"{cid}_{pname}_{int(ts * 1000)}",
                         tags=[pname, cid], device=cid, ts=int(ts))
            # состояние станции в контейнере (дельты)
            kv_set(self.station, "state", f"{cid}.{pname}", rec)
            # аларм
            self._check_alarm(cid, pname, value, ts, quality)

    def _check_alarm(self, cid: str, point: str, value: float, ts: float, quality: str) -> None:
        th = THRESHOLDS.get(point)
        if not th:
            return
        level = None
        if th.get("high") is not None and value > th["high"]:
            level = "HIGH"
        elif th.get("low") is not None and value < th["low"]:
            level = "LOW"
        if quality == "bad":
            level = "BADQ"
        if not level:
            return
        key = (cid, point)
        now = time.time()
        if now - self.alarm_sent_at.get(key, 0) < 5:  # anti-flood: не чаще раза в 5с
            return
        self.alarm_sent_at[key] = now
        alarm = {"ts": ts, "cid": cid, "point": point, "value": round(value, 3),
                 "level": level, "acked": False}
        self.alarms.appendleft(alarm)
        # аларм-лог в контейнере: delta append -> hash-chain
        kv_set(self.station, "alarms", f"{int(ts * 1000)}", alarm)
        # событие на шину (HMI видит в traffic)
        bus_send(self.bus, ENGINE_ADDR, HMI_ADDR, "ALARM", alarm)
        log.warning("ALARM %s %s=%s (%s)", cid, point, round(value, 3), level)

    # ------------------------------------------------------------- API для HMI
    def snapshot(self) -> dict:
        with self.lock:
            return {"latest": {k: dict(v) for k, v in self.latest.items()},
                    "alarms": list(self.alarms),
                    "traffic": list(self.traffic)}

    def get_history(self, point: str, minutes: int = HISTORY_MINUTES) -> list[tuple[float, float]]:
        t0 = int(time.time()) - minutes * 60
        q = f'X-Tag == "{point}" AND X-Timestamp >= {t0}'
        rows = []
        for p in tagdb_query(self.hist, q):
            headers, body_b = emlbox.read_eml(p)
            ts = int(headers.get("x-timestamp", "0") or 0)
            try:
                import json
                body = json.loads(body_b.decode("utf-8"))
                rows.append((ts, float(body.get("value", 0))))
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                continue
        return sorted(rows)

    def station_alarms(self) -> dict:
        return kv_dump(self.station, "alarms")

    def run_verify(self) -> tuple[bool, list[str]]:
        return verify(self.station)

    # ------------------------------------------------------------- поток
    def run(self) -> None:
        system_addrs = {ENGINE_ADDR, f"{ENGINE_ADDR}@system.local",
                        HMI_ADDR, f"{HMI_ADDR}@system.local"}
        while not self.stop.is_set():
            try:
                for m in bus_list(self.bus):
                    if m["to"] in system_addrs:
                        self._handle(m)
                        bus_consume(m["path"])
            except Exception as e:
                log.warning("engine tick error: %s", e)
            self.stop.wait(self.tick_s)
