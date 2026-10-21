"""engine.py — движок EML-SCADA.

Потребляет TELEMETRY со своей почты на шине, пишет историк (tagdb .eml),
держит состояние станции в контейнере (KV-дельты), считает алармы и
публикует события ALARM/ALARM_CLEAR/ALARM_ACK на шину.

Ключевые решения:
* состояние в контейнер пишется пачкой раз в `station.state_persist_s`
  (а не 8 дельт/с на каждую точку) — иначе tail index растёт квадратично;
* живой график HMI берётся из кольцевого буфера в памяти, tagdb читается
  только для глубокой истории;
* все записи в контейнеры идут под одним lock: kv_set не конкурент-безопасен.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import emlbox, retention
from .alarms import AlarmEngine
from .config import Config
from .emlbox import ENGINE_ADDR, HMI_ADDR, bus_consume, bus_list, bus_send, kv_get, kv_set, tagdb_insert, tagdb_query
from .integrity import AnchorLog, Report, deep_verify

log = logging.getLogger("scada.engine")

RING_MAX = 3600          # 1 час при 1 Гц на точку
TRAFFIC_MAX = 200


@dataclass
class PointRec:
    value: float
    ts: float
    quality: str
    unit: str = ""
    digits: int = 2

    def as_dict(self, now: float | None = None, stale_after_s: float = 3.0) -> dict:
        now = time.time() if now is None else now
        return {"value": self.value, "ts": self.ts, "quality": self.quality,
                "unit": self.unit, "digits": self.digits,
                "stale": (now - self.ts) > stale_after_s}


class Engine(threading.Thread):
    def __init__(self, store: Path, cfg: Config, stop: threading.Event | None = None):
        super().__init__(daemon=True, name="engine")
        self.store = Path(store)
        self.cfg = cfg
        self.bus = self.store / "bus"
        self.hist = self.store / "hist"
        self.archive = self.store / "archive"
        self.station = self.store / f"{cfg.get('station.name')}.eml"
        self.tick_s = cfg.tick_s

        self.stop = stop or threading.Event()
        self.lock = threading.Lock()          # состояние в памяти
        self.kv_lock = threading.RLock()      # записи в контейнер станции
        self.verify_lock = threading.Lock()

        self.alarms = AlarmEngine(cfg.thresholds,
                                  realarm_s=float(cfg.get("alarms.realarm_s")),
                                  max_items=int(cfg.get("alarms.max_items")))
        self.anchors = AnchorLog(self.store / "anchor.eml", store=self.store)

        self.latest: dict[str, dict[str, PointRec]] = {}
        self.series: dict[str, deque] = {}
        self.traffic: deque = deque(maxlen=TRAFFIC_MAX)
        self.stats: Counter = Counter()
        self.started_at = time.time()
        self.last_snapshot: dict | None = None

        self._deltas = 0
        self._last_persist = 0.0
        self._last_prune = time.time()
        self._last_anchor = time.time()
        self._last_error_log = 0.0

    # ------------------------------------------------------------- setup
    def ensure_layout(self) -> None:
        emlbox.check_binary()
        for d in (self.bus, self.hist, self.archive):
            d.mkdir(parents=True, exist_ok=True)
        if not self.station.exists():
            self._new_station()
        self.anchors.ensure()
        self._restore_state()

    def _new_station(self) -> None:
        if not emlbox.create(self.station, f"{self.cfg.get('station.name')}@system.local",
                             "EML-SCADA station"):
            raise RuntimeError(f"не удалось создать станцию {self.station}")
        with self.kv_lock:
            kv_set(self.station, "config", "version", "1.0")
            kv_set(self.station, "config", "created_at", time.time())
            kv_set(self.station, "config", "thresholds", self.cfg.get("thresholds"))
            self.stats["station_created"] += 1

    def _restore_state(self) -> None:
        """Поднимает последний снимок состояния после рестарта."""
        units = {p: (spec.get("unit", ""), int(spec.get("digits", 2)))
                 for pts in self.cfg.controllers.values() for p, spec in pts.items()}
        try:
            for cid in self.cfg.controllers:
                snap = kv_get(self.station, "state", cid)
                if not isinstance(snap, dict):
                    continue
                for pname, rec in snap.items():
                    try:
                        unit, digits = units.get(pname, ("", 2))
                        self.latest.setdefault(cid, {})[pname] = PointRec(
                            value=float(rec["value"]), ts=float(rec.get("ts", 0.0)),
                            quality=str(rec.get("quality", "good")), unit=unit, digits=digits)
                    except (KeyError, TypeError, ValueError):
                        continue
            restored = sum(len(v) for v in self.latest.values())
            if restored:
                log.info("state restored from container: %d point(s)", restored)
                self.stats["state_restored"] = restored
        except Exception as e:  # восстановление не должно мешать старту
            log.warning("state restore failed: %s", e)

    # ------------------------------------------------------------- обработка
    def _traffic(self, event: str, msg: dict, **extra) -> None:
        item = {"ts": time.time(), "event": event,
                "from": msg.get("from", ""), "to": msg.get("to", "")}
        item.update(extra)
        self.traffic.appendleft(item)

    def _handle(self, m: dict) -> None:
        ev = m["event"]
        if ev == "TELEMETRY":
            self._on_telemetry(m)
        elif ev == "ALARM_ACK":
            self._on_alarm_ack(m)
        else:
            self._traffic(ev or "<?>", m)
            self.stats["events_other"] += 1
            if ev == "SETPOINT_ACK":
                self.stats["setpoint_ack"] += 1
            elif ev == "SETPOINT_REJECT":
                self.stats["setpoint_reject"] += 1

    def _on_telemetry(self, m: dict) -> None:
        body = m["body"]
        cid = str(body.get("cid") or m["from"].split("@")[0] or "unknown")
        points = body.get("points")
        if not isinstance(points, dict) or not points:
            self.stats["bad_msg"] += 1
            log.warning("TELEMETRY без points от %s", cid)
            return
        quality = str(body.get("quality", "good"))
        ts = float(body.get("ts") or time.time())
        self._traffic("TELEMETRY", m, cid=cid)
        self.stats["telemetry"] += 1

        events: list[dict] = []
        valid: dict[str, float] = {}
        with self.lock:
            box = self.latest.setdefault(cid, {})
            for pname, raw in points.items():
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    self.stats["bad_msg"] += 1
                    continue
                valid[pname] = value
                spec = self.cfg.controllers.get(cid, {}).get(pname, {})
                rec = PointRec(value=value, ts=ts, quality=quality,
                               unit=spec.get("unit", ""), digits=int(spec.get("digits", 2)))
                box[pname] = rec
                key = f"{cid}.{pname}"
                ring = self.series.get(key)
                if ring is None:
                    ring = self.series[key] = deque(maxlen=RING_MAX)
                ring.append((ts, value))

        for pname, value in valid.items():
            # историк: одна запись tagdb на точку
            ok = tagdb_insert(self.hist, {"value": value, "quality": quality},
                              rec_id=f"{cid}_{pname}_{int(ts * 1000)}",
                              tags=[pname, cid], device=cid, ts=int(ts))
            self.stats["hist_writes" if ok else "hist_fail"] += 1
            events.extend(self.alarms.evaluate(cid, pname, value, quality, ts))

        for ev in events:
            self._emit_alarm(ev)
        self._persist_state()

    def _emit_alarm(self, ev: dict) -> None:
        kind = ev["event"]
        self.stats["alarm_raise" if kind == "ALARM" else "alarm_clear"] += 1
        log.info("%s %s %s=%s (%s)", kind, ev["cid"], ev["point"], ev["value"], ev["level"])
        self.traffic.appendleft({"ts": ev["ts"], "event": kind, "from": ENGINE_ADDR,
                                 "to": HMI_ADDR, "cid": ev["cid"],
                                 "point": ev["point"], "level": ev["level"]})
        with self.kv_lock:
            kv_set(self.station, "alarms", ev["id"], ev)
            self._deltas += 1
        bus_send(self.bus, ENGINE_ADDR, HMI_ADDR, kind, ev)

    def _on_alarm_ack(self, m: dict) -> None:
        body = m.get("body") or {}
        acked = self.ack(alarm_id=body.get("id"), all_=bool(body.get("all")),
                         by=str(body.get("by") or m.get("from") or "bus"))
        self._traffic("ALARM_ACK", m, count=len(acked))

    # ------------------------------------------------------------- состояние
    def _persist_state(self, force: bool = False) -> None:
        every = float(self.cfg.get("station.state_persist_s"))
        now = time.time()
        if not force and now - self._last_persist < every:
            return
        with self.lock:
            snapshot = {cid: {p: {"value": r.value, "ts": r.ts, "quality": r.quality}
                              for p, r in pts.items() if not (now - r.ts) > 3600}
                        for cid, pts in self.latest.items()}
        for cid, pts in snapshot.items():
            if not pts:
                continue
            with self.kv_lock:
                if kv_set(self.station, "state", cid, pts) is not None:
                    self._deltas += 1
        self._last_persist = now
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        limit = int(self.cfg.get("station.rotate_after_deltas"))
        if self._deltas < limit:
            return
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            target = self.archive / f"{self.station.stem}_{stamp}.eml"
            self.station.rename(target)
            try:
                # якорь ставим уже на финальное имя: архивный файл неизменяем
                self.anchors.append(target)
            except Exception as e:
                log.error("anchor after rotation failed: %s", e)
                self.stats["anchor_fail"] += 1
            self._deltas = 0
            self._new_station()
            self._persist_state(force=True)
            self.stats["station_rotated"] += 1
            self.traffic.appendleft({"ts": time.time(), "event": "STATION_ROTATE",
                                     "from": ENGINE_ADDR, "to": HMI_ADDR,
                                     "file": target.name})
            log.info("station rotated -> %s", target.name)
        except Exception as e:
            log.error("station rotation failed: %s", e)
            self.stats["rotate_fail"] += 1

    # ------------------------------------------------------------- API
    def snapshot(self) -> dict:
        now = time.time()
        stale_after = self.cfg.stale_after_s
        with self.lock:
            latest = {cid: {p: r.as_dict(now, stale_after) for p, r in pts.items()}
                      for cid, pts in self.latest.items()}
            series_head = {k: list(v)[-1] for k, v in self.series.items() if v}
        alarm_stats = self.alarms.stats()
        return {
            "now": now,
            "started_at": self.started_at,
            "uptime_s": round(now - self.started_at, 3),
            "store": str(self.store),
            "station": self.station.name,
            "latest": latest,
            "series_head": series_head,
            "alarms": self.alarms.recent(100),
            "alarm_stats": alarm_stats,
            "traffic": list(self.traffic)[:50],
            "stats": dict(self.stats),
            "config": {"thresholds": self.cfg.get("thresholds"),
                       "stale_after_s": stale_after},
        }

    def get_series(self, cid: str, point: str, minutes: float = 5.0) -> list[tuple[float, float]]:
        """Живой график: кольцевой буфер (без обращений к диску)."""
        ring = self.series.get(f"{cid}.{point}")
        if not ring:
            return []
        cutoff = time.time() - minutes * 60
        return [(ts, v) for ts, v in ring if ts >= cutoff]

    def get_history(self, point: str, cid: str | None = None,
                    minutes: float = 60.0) -> list[tuple[float, float]]:
        """Глубокая история из tagdb (header-only scan по индексу)."""
        t0 = int(time.time() - minutes * 60)
        q = f'X-Tag == "{point}" AND X-Timestamp >= {t0}'
        if cid:
            q += f' AND X-Device-ID == "{cid}"'
        rows = []
        for p in tagdb_query(self.hist, q):
            headers, body_b = emlbox.read_eml(p)
            try:
                ts = int(headers.get("x-timestamp", "0") or 0)
                body = json.loads(body_b.decode("utf-8"))
                rows.append((float(ts), float(body.get("value", 0))))
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError, OSError):
                continue
        return sorted(rows)

    def ack(self, alarm_id: str | None = None, all_: bool = False,
            by: str = "hmi") -> list[dict]:
        try:
            acked = self.alarms.ack(alarm_id=alarm_id, all_=all_, by=by)
        except ValueError:
            return []
        out = []
        for a in acked:
            rec = a.as_dict()
            out.append(rec)
            with self.kv_lock:
                kv_set(self.station, "alarms", a.id, rec)
                self._deltas += 1
            bus_send(self.bus, ENGINE_ADDR, HMI_ADDR, "ALARM_ACK", rec)
        self.stats["alarm_ack"] += len(out)
        return out

    def setpoint(self, cid: str, point: str, value: float, by: str = "hmi") -> dict:
        spec = self.cfg.controllers.get(cid, {}).get(point)
        if spec is None:
            raise ValueError(f"неизвестная точка {cid}.{point}")
        lo, hi = float(spec["lo"]), float(spec["hi"])
        if not lo <= value <= hi:
            raise ValueError(f"{cid}.{point}: {value} вне [{lo}, {hi}]")
        req = {"point": point, "value": value, "from_hmi": True,
               "by": by, "ts": time.time()}
        bus_send(self.bus, HMI_ADDR if by == "hmi" else ENGINE_ADDR, cid, "SETPOINT", req)
        self.stats["setpoints"] += 1
        return req

    # ------------------------------------------------------------- целостность
    def verify(self, deep: bool = True, anchor: bool | None = None) -> dict:
        """Проверка всех контейнеров стора + журнала якорей + (опц.) новый якорь."""
        if anchor is None:
            anchor = deep and bool(self.cfg.get("integrity.anchor_on_verify"))
        with self.verify_lock:
            files = [self.station] + sorted(self.archive.glob("*.eml"))
            reports: list[Report] = []
            for f in files:
                if not f.exists():
                    continue
                rec = self.anchors.latest_for(self.anchors.rel(f)) if deep else None
                reports.append(deep_verify(f, anchor=rec, run_tool=True))
            anchor_issues = self.anchors.verify() if deep else []
            ok = all(r.ok for r in reports) and not anchor_issues
            anchored = None
            if ok and anchor and self.station.exists():
                try:
                    anchored = self.anchors.append(self.station).as_dict()
                except Exception as e:
                    anchor_issues.append(f"anchor append failed: {e}")
                    ok = False
            result = {
                "ok": ok,
                "checked_at": time.time(),
                "files": [r.as_dict() for r in reports],
                "anchor_issues": anchor_issues,
                "anchor_records": len(self.anchors.records()) if deep else 0,
                "anchored": anchored,
                "head": self.anchors.export_head() if deep else None,
            }
            self.last_snapshot = result
            self.stats["verify_ok" if ok else "verify_bad"] += 1
            return result

    # ------------------------------------------------------------- поток
    def run(self) -> None:
        while not self.stop.is_set():
            try:
                for m in bus_list(self.bus, address=ENGINE_ADDR):
                    if m["ok"]:
                        self._handle(m)
                    else:
                        self.stats["bad_msg"] += 1
                        log.warning("битое сообщение: %s", m["path"].name)
                    bus_consume(m["path"])
                    self.stats["msgs"] += 1
                now = time.time()
                if now - self._last_prune >= float(self.cfg.get("historian.prune_every_s")):
                    self._last_prune = now
                    res = retention.prune_all(self.store, self.cfg)
                    removed = res["historian"]["files"] + res["bus"]["files"]
                    if removed:
                        self.stats["pruned"] += removed
                        log.info("retention: removed %d file(s), %.1f KiB",
                                 removed, (res["historian"]["bytes"] + res["bus"]["bytes"]) / 1024)
                if now - self._last_anchor >= float(self.cfg.get("integrity.anchor_every_s")):
                    self._last_anchor = now
                    if self.station.exists():
                        self.anchors.append(self.station)
                        self.stats["anchors"] += 1
                self._persist_state()
            except Exception as e:
                self.stats["tick_errors"] += 1
                if time.time() - self._last_error_log > 5:
                    self._last_error_log = time.time()
                    log.warning("engine tick error: %s", e)
            self.stop.wait(self.tick_s)
