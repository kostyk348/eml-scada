"""alarms.py — состояние алармов: raise / clear / rearm / ack.

Чистая логика без I/O, чтобы её можно было проверить тестами и переиспользовать
в движке. Правила:

* HIGH/LOW — по порогам точки; BADQ — по качеству данных (приоритетнее порогов).
* Сброс — только по возврату в норму с мёртвой зоной (антидребезг на границе).
* Пока аларм активен, повторный ALARM уходит не чаще `realarm_s` (напоминание).
* ACK не снимает аларм, а фиксирует, что оператор его увидел.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .config import Threshold

LEVELS = ("HIGH", "LOW", "BADQ")
EV_RAISE = "ALARM"
EV_CLEAR = "ALARM_CLEAR"
EV_ACK = "ALARM_ACK"


@dataclass
class Alarm:
    id: str
    cid: str
    point: str
    level: str
    value: float
    raised_at: float
    state: str = "ACTIVE"          # ACTIVE | CLEARED
    peak: float = 0.0
    acked: bool = False
    acked_at: float | None = None
    acked_by: str = ""
    cleared_at: float | None = None
    repeated: int = 0              # сколько раз напоминали
    _last_emit: float = field(default=0.0, repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "cid": self.cid,
            "point": self.point,
            "level": self.level,
            "value": round(self.value, 3),
            "peak": round(self.peak, 3),
            "ts": self.raised_at,
            "state": self.state,
            "acked": self.acked,
            "acked_at": self.acked_at,
            "acked_by": self.acked_by,
            "cleared_at": self.cleared_at,
            "repeated": self.repeated,
        }


class AlarmEngine:
    def __init__(self, thresholds: dict[str, Threshold],
                 realarm_s: float = 300.0, max_items: int = 500):
        self.thresholds = thresholds
        self.realarm_s = float(realarm_s)
        self.max_items = int(max_items)
        self._active: dict[tuple[str, str], Alarm] = {}
        self._items: deque[Alarm] = deque(maxlen=self.max_items)

    # ------------------------------------------------------------- helpers
    def _raise(self, cid: str, point: str, level: str, value: float, ts: float) -> Alarm:
        aid = f"{cid}.{point}.{int(ts * 1000)}"
        alarm = Alarm(id=aid, cid=cid, point=point, level=level,
                      value=value, raised_at=ts, peak=value, _last_emit=ts)
        self._active[(cid, point)] = alarm
        self._items.appendleft(alarm)
        return alarm

    @staticmethod
    def _event(kind: str, alarm: Alarm, **extra) -> dict:
        payload = {"event": kind, **alarm.as_dict(), **extra}
        return payload

    # ------------------------------------------------------------- оценка
    def evaluate(self, cid: str, point: str, value: float,
                 quality: str = "good", ts: float | None = None) -> list[dict]:
        """Возвращает список событий (ALARM / ALARM_CLEAR) по одной точке."""
        ts = time.time() if ts is None else float(ts)
        th = self.thresholds.get(point)
        level = "BADQ" if quality == "bad" else (th.level_for(value) if th else None)
        key = (cid, point)
        active = self._active.get(key)
        events: list[dict] = []

        if active is not None:
            active.peak = max(active.peak, value) if active.level == "HIGH" else (
                min(active.peak, value) if active.level == "LOW" else active.peak)

            if level == active.level:
                # тот же аларм: напоминание не чаще realarm_s
                if self.realarm_s > 0 and ts - active._last_emit >= self.realarm_s:
                    active.repeated += 1
                    active._last_emit = ts
                    events.append(self._event(EV_RAISE, active, repeat=True))
                return events

            # уровень сменился или вернулось в норму -> закрываем старый
            if level is None:
                if th is not None and not th.cleared(value):
                    return events  # внутри мёртвой зоны: держим аларм как есть
            active.state = "CLEARED"
            active.cleared_at = ts
            events.append(self._event(EV_CLEAR, active))
            self._active.pop(key, None)
            if level is not None:
                fresh = self._raise(cid, point, level, value, ts)
                events.append(self._event(EV_RAISE, fresh))
            return events

        if level is not None:
            fresh = self._raise(cid, point, level, value, ts)
            events.append(self._event(EV_RAISE, fresh))
        return events

    # ------------------------------------------------------------- ACK
    def ack(self, alarm_id: str | None = None, cid: str | None = None,
            point: str | None = None, all_: bool = False, by: str = "hmi",
            ts: float | None = None) -> list[Alarm]:
        ts = time.time() if ts is None else float(ts)
        targets: Iterable[Alarm]
        if all_:
            targets = list(self._items)
        elif alarm_id:
            targets = [a for a in self._items if a.id == alarm_id]
        elif cid and point:
            targets = [a for a in self._items if a.cid == cid and a.point == point]
        elif cid:
            targets = [a for a in self._items if a.cid == cid]
        else:
            raise ValueError("ack: нужен alarm_id, cid(+point) или all_=True")
        acked = []
        for a in targets:
            if a.acked:
                continue
            a.acked = True
            a.acked_at = ts
            a.acked_by = by
            acked.append(a)
        return acked

    # ------------------------------------------------------------- чтение
    def active(self) -> list[Alarm]:
        return list(self._active.values())

    def recent(self, limit: int | None = None) -> list[dict]:
        items = list(self._items)
        return [a.as_dict() for a in (items if limit is None else items[:limit])]

    def stats(self) -> dict:
        active = self.active()
        return {
            "active": len(active),
            "unacked": sum(1 for a in active if not a.acked),
            "total": len(self._items),
            "high": sum(1 for a in active if a.level == "HIGH"),
            "low": sum(1 for a in active if a.level == "LOW"),
            "badq": sum(1 for a in active if a.level == "BADQ"),
        }
