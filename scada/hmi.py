"""hmi.py — операторский дашборд (NiceGUI).

Страница читает только `Engine.snapshot()` (память) и кольцевой буфер графиков:
никаких подпроцессов emlbox на каждый refresh. Глубокая история из tagdb —
через API/`/api/history`, чтобы UI не блокировался диском.

Действия оператора: ACK аларма, ACK all, setpoint, verify (в отдельном потоке).
"""
from __future__ import annotations

import time

from nicegui import run, ui

from .config import Config
from .emlbox import HMI_ADDR, bus_consume, bus_list
from .engine import Engine

ALARM_COLORS = {"HIGH": "red", "LOW": "orange", "BADQ": "purple"}
STATE_COLORS = {"ACTIVE": "negative", "CLEARED": "grey"}
QUALITY_COLORS = {"good": "text-green-600", "bad": "text-red-600",
                  "uncertain": "text-orange-500", "grey": "text-gray-500"}


class Hmi:
    def __init__(self, engine: Engine, cfg: Config):
        self.engine = engine
        self.cfg = cfg
        self.point_labels: dict[tuple[str, str], ui.label] = {}
        self.value_labels: dict[tuple[str, str], ui.label] = {}
        self.stats_labels: dict[str, ui.label] = {}
        self.alarm_box: ui.column | None = None
        self.alarm_sig: tuple | None = None
        self.device_select = None
        self.point_select = None
        self.window_select = None
        self.chart = None
        self.traffic_table = None
        self.verify_badge = None
        self.clock = None
        self._mail_seen = 0

    # ------------------------------------------------------------- построение
    def build(self) -> None:
        ui.page_title("EML-SCADA 1.0")
        with ui.header().classes("items-center gap-4"):
            ui.label("EML-SCADA 1.0").classes("text-xl font-bold")
            ui.label("всё есть .eml").classes("text-sm opacity-70")
            self.clock = ui.label("").classes("text-sm opacity-70")
            ui.space()
            self.verify_badge = ui.label("verify: ?").classes("text-sm font-mono")
            ui.button("Verify", on_click=self._on_verify, icon="shield").props("flat dense")
        with ui.row().classes("w-full items-start no-wrap"):
            with ui.column().classes("w-3/5 gap-3"):
                self._build_controllers()
                self._build_chart()
                self._build_traffic()
            with ui.column().classes("w-2/5 gap-3"):
                self._build_alarms()
                self._build_operator()
                self._build_stats()
        ui.timer(float(self.cfg.get("hmi.refresh_s")), self._refresh, active=True)
        self._refresh()
        # статус целостности — на старте, чтобы бейдж не висел «?»
        ui.timer(1.0, self._verify_on_start, once=True)

    def _build_controllers(self) -> None:
        self.ctl_grid = ui.row().classes("w-full gap-3")
        for cid, points in self.cfg.controllers.items():
            with self.ctl_grid:
                with ui.card().classes("flex-1 min-w-[240px]"):
                    with ui.row().classes("items-center w-full"):
                        ui.label(cid).classes("font-bold text-primary")
                        ui.label(f"<{cid}@system.local>").classes("text-xs opacity-60")
                    for pname, spec in points.items():
                        with ui.row().classes("items-center w-full gap-2"):
                            ui.label(f"{pname}").classes("font-mono text-sm w-24")
                            val = ui.label("—").classes("font-mono text-sm flex-1")
                            q = ui.label("n/a").classes("text-xs font-mono opacity-60")
                            self.value_labels[(cid, pname)] = val
                            self.point_labels[(cid, pname)] = q

    def _build_alarms(self) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label("Алармы").classes("text-lg font-semibold")
                ui.space()
                self.alarm_count = ui.badge("0", color="grey")
                ui.button("ACK all", on_click=self._on_ack_all,
                          icon="done_all").props("flat dense")
            self.alarm_box = ui.column().classes("w-full gap-1")

    def _build_operator(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Оператор").classes("text-lg font-semibold")
            with ui.row().classes("items-center gap-2 w-full"):
                self.sp_device = ui.select(list(self.cfg.controllers), value="ctl_02",
                                          label="контроллер").classes("w-32")
                self.sp_point = ui.select(
                    list(self.cfg.controllers["ctl_02"]), value="flow",
                    label="точка").classes("w-36")
                self.sp_device.on_value_change(
                    lambda: self.sp_point.set_options(list(self.cfg.controllers[self.sp_device.value])))
                self.sp_value = ui.input(value="60", label="значение").classes("w-28")
                ui.button("Отправить", on_click=self._on_setpoint, icon="send")
            ui.label("SETPOINT идёт почтой на шину; контроллер отвечает ACK/REJECT")\
                .classes("text-xs opacity-60")

    def _build_chart(self) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.label("Тренд (кольцевой буфер, 1 ч)").classes("text-lg font-semibold")
                ui.space()
                self.device_select = ui.select(list(self.cfg.controllers), value="ctl_01",
                                              on_change=lambda: self._rebind_points())
                self.point_select = ui.select(list(self.cfg.controllers["ctl_01"]),
                                              value="temp", on_change=lambda: self._load_chart())
                self.window_select = ui.select({1: "1 мин", 5: "5 мин", 15: "15 мин", 60: "60 мин"},
                                              value=5, on_change=lambda: self._load_chart())
            self.chart = ui.echart({
                "tooltip": {"trigger": "axis"},
                "grid": {"left": 48, "right": 16, "top": 16, "bottom": 28},
                "xAxis": {"type": "time"},
                "yAxis": {"type": "value", "scale": True},
                "series": [{"type": "line", "showSymbol": False, "name": "value",
                            "data": [], "lineStyle": {"width": 1.5}}],
            }).classes("w-full h-64")

    def _build_traffic(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Шина: события .eml").classes("text-lg font-semibold")
            self.traffic_table = ui.table(
                columns=[{"name": "ts", "label": "Время", "field": "ts", "align": "left"},
                         {"name": "ev", "label": "Событие", "field": "ev", "align": "left"},
                         {"name": "frm", "label": "From", "field": "frm", "align": "left"},
                         {"name": "to", "label": "To", "field": "to", "align": "left"},
                         {"name": "what", "label": "Детали", "field": "what", "align": "left"}],
                rows=[], row_key="id").classes("w-full text-xs").props("dense flat")

    def _build_stats(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("Движок").classes("text-lg font-semibold")
            for key, label in (("msgs", "сообщений"), ("telemetry", "телеметрий"),
                               ("hist", "записей историка"), ("alarms", "алармов raise/clear"),
                               ("acked", "ACK"), ("pruned", "удалено ретенцией"),
                               ("anchors", "якорей"), ("bad", "битых")):
                with ui.row().classes("items-center w-full justify-between"):
                    ui.label(label).classes("text-sm opacity-70")
                    self.stats_labels[key] = ui.label("0").classes("font-mono text-sm")
            ui.separator()
            with ui.row().classes("items-center w-full justify-between"):
                ui.label("станция").classes("text-sm opacity-70")
                ui.label(self.engine.station.name).classes("font-mono text-xs")

    # ------------------------------------------------------------- refresh
    def _mailbox(self) -> None:
        """Читает свою почту (ALARM/ALARM_ACK) — иначе спул копится."""
        for m in bus_list(self.engine.bus, address=HMI_ADDR):
            try:
                if m["event"] in ("ALARM", "ALARM_CLEAR"):
                    self._mail_seen += 1
                    ev = m["body"]
                    ui.notify(f"{m['event']} {ev.get('cid')}.{ev.get('point')}="
                              f"{ev.get('value')} ({ev.get('level')})",
                              type="negative" if m["event"] == "ALARM" else "positive")
            finally:
                bus_consume(m["path"])

    def _refresh(self) -> None:
        snap = self.engine.snapshot()
        self.clock.set_text(time.strftime("%H:%M:%S"))
        stale_after = self.cfg.stale_after_s

        for (cid, pname), val_label in self.value_labels.items():
            rec = snap["latest"].get(cid, {}).get(pname)
            if rec is None:
                val_label.set_text("—")
                self.point_labels[(cid, pname)].set_text("n/a")
                continue
            digits = int(rec.get("digits", 2))
            val_label.set_text(f"{rec['value']:.{digits}f} {rec.get('unit', '')}")
            stale = rec.get("stale") or (time.time() - rec["ts"]) > stale_after
            val_label.classes(replace="font-mono text-sm flex-1 " +
                              ("opacity-40" if stale else "font-semibold"))
            quality_label = self.point_labels[(cid, pname)]
            quality_label.set_text("stale" if stale else rec["quality"])
            color = "text-gray-500" if stale else QUALITY_COLORS.get(rec.get("quality"), "text-gray-500")
            quality_label.classes(replace=f"text-xs font-mono {color}")

        self._render_alarms(snap["alarms"])
        self._render_traffic(snap["traffic"])
        self._render_stats(snap["stats"], snap["alarm_stats"])
        self._load_chart()
        try:
            self._mailbox()
        except Exception:
            pass

    def _render_alarms(self, alarms: list[dict]) -> None:
        sig = tuple((a["id"], a["state"], a["acked"], a["value"]) for a in alarms[:30])
        self.alarm_count.set_text(f"{sum(1 for a in alarms if a['state'] == 'ACTIVE')} active")
        if sig == self.alarm_sig or self.alarm_box is None:
            return
        self.alarm_sig = sig
        self.alarm_box.clear()
        with self.alarm_box:
            if not alarms:
                ui.label("нет алармов").classes("text-sm opacity-60")
                return
            for a in alarms[:12]:
                with ui.row().classes("items-center w-full gap-2 no-wrap"):
                    ui.badge(a["level"], color=ALARM_COLORS.get(a["level"], "grey"))
                    ui.label(f"{a['cid']}.{a['point']} = {a['value']}").classes("font-mono text-xs flex-1")
                    ui.label(time.strftime("%H:%M:%S", time.localtime(a["ts"]))).classes("text-xs opacity-60")
                    if a["state"] != "ACTIVE":
                        ui.badge("cleared", color="grey").classes("text-xs")
                    elif a["acked"]:
                        ui.badge(f"ack: {a['acked_by']}", color="green").classes("text-xs")
                    else:
                        ui.button(icon="check", on_click=lambda a_id=a["id"]: self._on_ack(a_id))\
                            .props("flat dense size=sm")

    def _render_traffic(self, traffic: list[dict]) -> None:
        rows = []
        for i, t in enumerate(traffic[:20]):
            what = ""
            if t.get("point"):
                what = f"{t.get('cid', '')}.{t.get('point')} {t.get('level', '')}"
            elif t.get("count"):
                what = f"ack: {t['count']}"
            elif t.get("file"):
                what = t["file"]
            rows.append({"id": i, "ts": time.strftime("%H:%M:%S", time.localtime(t.get("ts", 0))),
                         "ev": t.get("event", ""), "frm": t.get("from", ""),
                         "to": t.get("to", ""), "what": what})
        self.traffic_table.rows = rows

    def _render_stats(self, stats: dict, alarm_stats: dict) -> None:
        self.stats_labels["msgs"].set_text(str(stats.get("msgs", 0)))
        self.stats_labels["telemetry"].set_text(str(stats.get("telemetry", 0)))
        self.stats_labels["hist"].set_text(str(stats.get("hist_writes", 0)))
        self.stats_labels["alarms"].set_text(
            f"{stats.get('alarm_raise', 0)}/{stats.get('alarm_clear', 0)}")
        self.stats_labels["acked"].set_text(str(stats.get("alarm_ack", 0)))
        self.stats_labels["pruned"].set_text(str(stats.get("pruned", 0)))
        self.stats_labels["anchors"].set_text(str(stats.get("anchors", 0)))
        self.stats_labels["bad"].set_text(str(stats.get("bad_msg", 0)))

    # ------------------------------------------------------------- график
    def _rebind_points(self) -> None:
        device = self.device_select.value
        self.point_select.set_options(list(self.cfg.controllers[device]),
                                      value=next(iter(self.cfg.controllers[device])))
        self._load_chart()

    def _load_chart(self) -> None:
        if not self.chart or not self.device_select.value:
            return
        device = self.device_select.value
        point = self.point_select.value
        minutes = float(self.window_select.value or 5)
        data = self.engine.get_series(device, point, minutes)
        series = self.chart.options["series"][0]
        series["data"] = [[ts * 1000, v] for ts, v in data]
        series["name"] = f"{device}.{point}"
        self.chart.update()

    # ------------------------------------------------------------- действия
    async def _verify_on_start(self) -> None:
        """Первый заход на страницу: показать результат проверки без клика."""
        if self.engine.last_snapshot is not None:
            self._show_verify(self.engine.last_snapshot)
            return
        await self._on_verify()

    def _show_verify(self, result: dict) -> None:
        if result["ok"]:
            self.verify_badge.set_text(f"verify: OK ({len(result['files'])})")
            self.verify_badge.classes(replace="text-sm font-mono text-green-600")
        else:
            self.verify_badge.set_text("verify: BROKEN")
            self.verify_badge.classes(replace="text-sm font-mono text-red-600")

    async def _on_verify(self) -> None:
        self.verify_badge.set_text("verify: …")
        self.verify_badge.classes(replace="text-sm font-mono text-gray-500")
        result = await run.io_bound(self.engine.verify)
        self._show_verify(result)
        if result["ok"]:
            files = len(result["files"])
            ui.notify(f"OK: {files} container(s), chain + anchors целы", type="positive")
        else:
            problems = [i for f in result["files"] for i in f["structural"] + f["tool"] + f["anchor"]]
            problems += result["anchor_issues"]
            ui.notify(" | ".join(problems[:4]) or "проверка не прошла", type="negative")

    def _on_ack(self, alarm_id: str) -> None:
        acked = self.engine.ack(alarm_id=alarm_id, by="hmi")
        ui.notify(f"ACK: {len(acked)}" if acked else "уже подтверждён",
                  type="positive" if acked else "info")
        self.alarm_sig = ()
        self._refresh()

    def _on_ack_all(self) -> None:
        acked = self.engine.ack(all_=True, by="hmi")
        ui.notify(f"ACK all: {len(acked)}", type="positive")
        self.alarm_sig = ()
        self._refresh()

    def _on_setpoint(self) -> None:
        cid = self.sp_device.value
        point = self.sp_point.value
        try:
            value = float(self.sp_value.value)
        except (TypeError, ValueError):
            ui.notify("значение должно быть числом", type="warning")
            return
        try:
            self.engine.setpoint(cid, point, value, by="hmi")
        except ValueError as e:
            ui.notify(str(e), type="negative")
            return
        ui.notify(f"SETPOINT {cid}.{point}={value} отправлен на шину")
