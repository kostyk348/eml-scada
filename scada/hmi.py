"""hmi.py — HMI-дашборд (NiceGUI): live-значения, алармы, график истории,
трафик шины, кнопка verify, ручной setpoint.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from nicegui import ui

from .engine import Engine
from .emlbox import HMI_ADDR, bus_send

log = logging.getLogger("scada.hmi")

ALARM_COLORS = {"HIGH": "red", "LOW": "orange", "BADQ": "purple"}


class Hmi:
    def __init__(self, engine: Engine, store: Path):
        self.engine = engine
        self.store = Path(store)
        self.chart = None
        self.point_select = None
        self.verify_badge = None

    # ------------------------------------------------------------- построение
    def build(self) -> None:
        ui.page_title("EML-SCADA v0.1")
        with ui.header().classes("items-center"):
            ui.label("EML-SCADA v0.1 — всё есть .eml").classes("text-xl font-bold")
            ui.space()
            self.verify_badge = ui.badge("verify: ?", color="grey")
            ui.button("Verify", on_click=self._on_verify, icon="shield").props("flat")
        with ui.row().classes("w-full"):
            self._build_controllers()
            self._build_side_panel()
        self._build_history()
        self._build_traffic()
        ui.timer(2.0, self._refresh, active=True)

    def _build_controllers(self) -> None:
        with ui.column().classes("w-1/2 gap-2"):
            ui.label("Контроллеры (живой опрос ≤1s)").classes("text-lg font-semibold")
            self.ctl_cards: dict[str, ui.column] = {}
            for cid in ("ctl_01", "ctl_02", "ctl_03"):
                with ui.card().classes("w-full") as card:
                    ui.label(f"• {cid}  <{cid}@system.local>").classes("font-bold text-primary")
                    points_box = ui.column()
                    self.ctl_cards[cid] = points_box

    def _build_side_panel(self) -> None:
        with ui.column().classes("w-1/2 gap-2"):
            ui.label("Алармы (hash-chain лог в контейнере)").classes("text-lg font-semibold")
            self.alarm_table = ui.table(
                columns=[{"name": "ts", "label": "Время", "field": "ts"},
                         {"name": "cid", "label": "Контроллер", "field": "cid"},
                         {"name": "point", "label": "Точка", "field": "point"},
                         {"name": "value", "label": "Значение", "field": "value"},
                         {"name": "level", "label": "Уровень", "field": "level"}],
                rows=[], row_key="ts").classes("w-full")
            with ui.row().classes("items-center"):
                ui.label("Setpoint → ctl_02:").classes("font-semibold")
                self.sp_point = ui.select(["valve_pos", "flow", "temp"], value="valve_pos")
                self.sp_value = ui.input(value="70", label="значение")
                ui.button("Отправить", on_click=self._on_setpoint, icon="send")

    def _build_history(self) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center"):
                ui.label("История (tagdb, .eml-записи)").classes("text-lg font-semibold")
                self.point_select = ui.select(
                    ["ctl_01.temp", "ctl_01.pressure", "ctl_01.level",
                     "ctl_02.temp", "ctl_02.valve_pos", "ctl_02.flow",
                     "ctl_03.temp", "ctl_03.level"],
                    value="ctl_01.temp", on_change=self._on_point_change)
            self.chart = ui.echart({
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 40, "right": 20, "top": 20, "bottom": 30},
                "xAxis": {"type": "time"},
                "yAxis": {"type": "value", "scale": True},
                "series": [{"type": "line", "showSymbol": False,
                            "name": "value", "data": []}],
            }).classes("w-full h-64")

    def _build_traffic(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Шина (последние события .eml)").classes("text-lg font-semibold")
            self.traffic_table = ui.table(
                columns=[{"name": "ts", "label": "Время", "field": "ts"},
                         {"name": "ev", "label": "Событие", "field": "ev"},
                         {"name": "from", "label": "From", "field": "from"},
                         {"name": "to", "label": "To", "field": "to"}],
                rows=[], row_key="ts").classes("w-full")

    # ------------------------------------------------------------- handlers
    def _refresh(self) -> None:
        snap = self.engine.snapshot()
        # точки
        for cid, box in self.ctl_cards.items():
            box.clear()
            with box:
                pts = snap["latest"].get(cid, {})
                for pname, rec in sorted(pts.items()):
                    qcol = {"good": "green", "bad": "red", "uncertain": "orange"}.get(
                        rec.get("quality", "good"), "grey")
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"{pname} = {rec['value']}").classes("font-mono")
                        ui.badge(rec.get("quality", "good"), color=qcol)
        # алармы
        self.alarm_table.rows = list(snap["alarms"])
        for r in self.alarm_table.rows:
            r["ts"] = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
            r["level"] = ui.badge(r["level"], color=ALARM_COLORS.get(r["level"], "grey"))
        # трафик
        rows = []
        for i, t in enumerate(snap["traffic"]):
            rows.append({"ts": time.strftime("%H:%M:%S", time.localtime(t.get("ts", 0))),
                         "ev": t.get("event", ""),
                         "from": t.get("from", ""), "to": t.get("to", "")})
        self.traffic_table.rows = rows[:20]
        # история
        if self.point_select:
            self._load_history(self.point_select.value)

    def _load_history(self, sel: str) -> None:
        if not self.chart or not sel:
            return
        cid, _, pname = sel.partition(".")
        data = self.engine.get_history(pname, minutes=5)
        self.chart.options["series"][0]["data"] = [[int(ts) * 1000, v] for ts, v in data]
        self.chart.options["series"][0]["name"] = sel
        self.chart.update()

    def _on_point_change(self) -> None:
        self._load_history(self.point_select.value)

    def _on_verify(self) -> None:
        ok, lines = self.engine.run_verify()
        if ok:
            self.verify_badge.set_text("verify: OK")
            self.verify_badge.props("color=green")
            ui.notify("OK: offsets, base hash, and delta chain verified", type="positive")
        else:
            self.verify_badge.set_text("verify: BROKEN")
            self.verify_badge.props("color=red")
            ui.notify(" | ".join(lines), type="negative")

    def _on_setpoint(self) -> None:
        point = self.sp_point.value
        try:
            value = float(self.sp_value.value)
        except ValueError:
            ui.notify("не число", type="warning")
            return
        bus_send(self.store / "bus", HMI_ADDR, "ctl_02", "SETPOINT",
                 {"point": point, "value": value, "from_hmi": True, "ts": time.time()})
        ui.notify(f"SETPOINT {point}={value} -> ctl_02 отправлен")
