"""api.py — JSON-API поверх того же движка (FastAPI из NiceGUI).

Нужен для интеграций: SCADA-шлюзы, скрипты, внешние HMI. Всё read-only, кроме
`/api/ack` и `/api/setpoint`, которые валидируются движком.

    GET  /healthz            — живость + счётчики
    GET  /api/state          — снимок (точки, алармы, трафик, статистика)
    GET  /api/alarms         — список алармов + статистика
    GET  /api/series         — живой график из кольцевого буфера
    GET  /api/history        — глубокая история из tagdb (.eml-записи)
    GET  /api/verify         — проверка целостности (+ якорь)
    POST /api/ack            — {"id": "..."} | {"all": true}
    POST /api/setpoint       — {"cid": "ctl_02", "point": "flow", "value": 60}
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from nicegui import app

from .engine import Engine


def register_api(engine: Engine) -> None:
    """Регистрирует роуты один раз (повторный вызов — no-op)."""
    if getattr(app, "_eml_scada_api", False):
        return
    app._eml_scada_api = True  # type: ignore[attr-defined]

    @app.get("/healthz")
    def healthz() -> dict:
        snap = engine.snapshot()
        return {"ok": True, "uptime_s": snap["uptime_s"],
                "alarms_active": snap["alarm_stats"]["active"],
                "station": snap["station"], "store": snap["store"]}

    @app.get("/api/state")
    def state() -> dict:
        return engine.snapshot()

    @app.get("/api/alarms")
    def alarms(limit: int = 200) -> dict:
        return {"stats": engine.alarms.stats(),
                "items": engine.alarms.recent(max(1, min(limit, 1000)))}

    @app.get("/api/series")
    def series(device: str, point: str, minutes: float = 5.0) -> dict:
        rows = engine.get_series(device, point, max(0.1, min(minutes, 60.0)))
        return {"device": device, "point": point, "rows": rows}

    @app.get("/api/history")
    def history(point: str, device: str | None = None, minutes: float = 60.0) -> dict:
        rows = engine.get_history(point, device, max(0.1, min(minutes, 60.0 * 24)))
        return {"device": device, "point": point, "rows": rows}

    @app.get("/api/verify")
    def verify(deep: bool = True) -> dict:
        return engine.verify(deep=deep)

    @app.post("/api/ack")
    def ack(payload: dict[str, Any]) -> dict:
        alarm_id = payload.get("id")
        if not alarm_id and not payload.get("all"):
            raise HTTPException(status_code=400, detail="нужен id или all=true")
        acked = engine.ack(alarm_id=str(alarm_id) if alarm_id else None,
                           all_=bool(payload.get("all")),
                           by=str(payload.get("by") or "api"))
        return {"acked": acked, "count": len(acked)}

    @app.post("/api/setpoint")
    def setpoint(payload: dict[str, Any]) -> dict:
        cid = str(payload.get("cid") or "")
        point = str(payload.get("point") or "")
        try:
            value = float(payload.get("value"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="value должен быть числом") from None
        try:
            req = engine.setpoint(cid, point, value, by=str(payload.get("by") or "api"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"sent": req}
