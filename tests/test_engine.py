"""Движок целиком: телеметрия -> историк/состояние, алармы, ACK, setpoint,
ротация станции, восстановление состояния и проверка целостности.

Тесты гоняют Engine без потока (детерминированно: сообщение кладётся в почту и
отдаётся `_handle`), кроме одного теста на сам цикл `run()`.
"""
from __future__ import annotations

import shutil
import threading
import time

import pytest

from scada import emlbox
from scada.controllers import Controller
from scada.demo import tamper_last_delta_consistently
from scada.engine import Engine
from scada.integrity import parse_container

pytestmark = pytest.mark.integration


def wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def cfg(make_cfg):
    """Быстрый конфиг: состояние пишется только по force, якоря — вручную."""
    return make_cfg(
        engine={"tick_s": 0.2},
        sim={"tick_s": 0.3, "fault_every_ticks": 2, "fault_ticks": 2,
             "badq_every_ticks": 0},
        station={"state_persist_s": 3600.0},
        integrity={"anchor_every_s": 3600.0},
    )


@pytest.fixture()
def engine(store, cfg):
    eng = Engine(store, cfg)
    eng.ensure_layout()
    return eng


def drain(engine: Engine, event: str | None = None,
          address: str = emlbox.HMI_ADDR) -> list[dict]:
    return [m for m in emlbox.bus_list(engine.bus, address=address)
            if event is None or m["event"] == event]


def feed(engine: Engine, cid: str, points: dict, *, quality: str = "good",
         ts: float | None = None) -> float:
    """Кладёт TELEMETRY в почту движка и обрабатывает ровно её."""
    ts = time.time() if ts is None else ts
    path = emlbox.bus_send(engine.bus, cid, emlbox.ENGINE_ADDR, "TELEMETRY",
                           {"cid": cid, "points": points,
                            "quality": quality, "ts": ts})
    msgs = [m for m in emlbox.bus_list(engine.bus, address=emlbox.ENGINE_ADDR)
            if m["path"] == path]
    assert msgs, "TELEMETRY не попала в почту движка"
    engine._handle(msgs[0])
    emlbox.bus_consume(path)
    return ts


# ---------------------------------------------------------------- телеметрия
def test_telemetry_updates_state_history_and_ring(engine, store):
    ts = feed(engine, "ctl_01", {"temp": 21.5, "pressure": 2.4, "level": 55.0})

    snap = engine.snapshot()
    temp = snap["latest"]["ctl_01"]["temp"]
    assert temp["value"] == 21.5
    assert temp["unit"] == "°C" and temp["digits"] == 1
    assert temp["stale"] is False
    assert snap["stats"]["telemetry"] == 1
    assert snap["stats"]["hist_writes"] == 3

    assert len(list((store / "hist").glob("*.eml"))) == 3
    assert engine.get_series("ctl_01", "temp", minutes=10) == [(ts, 21.5)]

    history = engine.get_history("temp", cid="ctl_01", minutes=60)
    assert [v for _, v in history] == [21.5]


def test_bad_telemetry_is_counted_not_fatal(engine):
    feed(engine, "ctl_01", {"temp": "not-a-number"}, ts=time.time())
    snap = engine.snapshot()
    assert snap["stats"]["bad_msg"] == 1
    assert "temp" not in snap["latest"].get("ctl_01", {})

    msgs = drain(engine, "TELEMETRY", address=emlbox.ENGINE_ADDR)
    assert msgs == []


def test_ticks_stale_after_timeout(engine):
    feed(engine, "ctl_01", {"temp": 20.0}, ts=time.time() - 10.0)
    assert engine.snapshot()["latest"]["ctl_01"]["temp"]["stale"] is True


# ---------------------------------------------------------------- алармы
def test_alarm_raise_clear_and_bus_events(engine):
    feed(engine, "ctl_01", {"temp": 45.0})          # high = 30
    events = drain(engine, "ALARM")
    assert len(events) == 1
    alarm = events[0]["body"]
    assert alarm["level"] == "HIGH" and alarm["point"] == "temp"
    assert engine.snapshot()["alarm_stats"]["active"] == 1
    assert engine.snapshot()["stats"]["alarm_raise"] == 1

    feed(engine, "ctl_01", {"temp": 21.0})          # вернулись в норму
    clears = drain(engine, "ALARM_CLEAR")
    assert len(clears) == 1
    assert clears[0]["body"]["id"] == alarm["id"]
    assert clears[0]["body"]["state"] == "CLEARED"
    assert engine.snapshot()["alarm_stats"]["active"] == 0


def test_ack_by_id_and_via_bus(engine):
    feed(engine, "ctl_01", {"temp": 45.0})
    alarm = engine.snapshot()["alarms"][0]

    acked = engine.ack(alarm_id=alarm["id"], by="op")
    assert len(acked) == 1 and acked[0]["acked"] is True
    assert engine.ack(alarm_id=alarm["id"]) == []       # повторный ACK — не событие

    bus_acks = drain(engine, "ALARM_ACK")
    assert len(bus_acks) == 1
    assert bus_acks[0]["body"]["acked_by"] == "op"

    # ACK приходит не только из HMI, но и письмом на почту движка
    feed(engine, "ctl_03", {"temp": 45.0})
    assert engine.alarms.stats()["unacked"] == 1
    path = emlbox.bus_send(engine.bus, emlbox.HMI_ADDR, emlbox.ENGINE_ADDR,
                           "ALARM_ACK", {"all": True, "by": "busop"})
    msg = [m for m in emlbox.bus_list(engine.bus, address=emlbox.ENGINE_ADDR)
           if m["path"] == path][0]
    engine._handle(msg)
    assert engine.alarms.stats()["unacked"] == 0


# ---------------------------------------------------------------- setpoint
def test_setpoint_roundtrip_with_controller(store, cfg):
    engine = Engine(store, cfg)
    engine.ensure_layout()
    stop = threading.Event()
    ctl = Controller("ctl_02", store / "bus", cfg, stop=stop)
    ctl.start()
    try:
        assert wait_until(lambda: ctl.sent >= 1), "контроллер не отправил телеметрию"

        engine.setpoint("ctl_02", "flow", 60.0, by="engine_test")
        assert wait_until(lambda: bool(drain(engine, "SETPOINT_ACK",
                                             address=emlbox.ENGINE_ADDR))), \
            "контроллер не подтвердил setpoint"
        ack = drain(engine, "SETPOINT_ACK", address=emlbox.ENGINE_ADDR)[0]
        assert ack["body"]["value"] == 60.0 and ack["body"]["ok"] is True
        assert ctl.targets["flow"] == 60.0

        engine._handle(ack)
        assert engine.snapshot()["stats"]["setpoint_ack"] == 1

        # вне диапазона точки — отказ ещё до контроллера
        with pytest.raises(ValueError):
            engine.setpoint("ctl_02", "flow", 1000.0)
        with pytest.raises(ValueError):
            engine.setpoint("ctl_02", "no_such_point", 1.0)

        # контроллер тоже отвергает мусор и отвечает SETPOINT_REJECT
        emlbox.bus_send(store / "bus", emlbox.HMI_ADDR, "ctl_02", "SETPOINT",
                        {"point": "flow", "value": 1e9})
        assert wait_until(lambda: ctl.rejects == 1), "контроллер не отверг setpoint"
        rej = drain(engine, "SETPOINT_REJECT")
        assert rej and "out of" in rej[0]["body"]["reason"]
    finally:
        stop.set()
        ctl.join(timeout=5.0)


# ---------------------------------------------------------------- состояние
def test_state_writes_are_batched_not_per_point(engine):
    """15 точек за 5 сообщений не должны дать 15 дельт в контейнере станции."""
    before = parse_container(engine.station).tail_seq
    for i in range(5):
        feed(engine, "ctl_01", {"temp": 20.0 + i, "pressure": 2.5, "level": 40.0})
    after = parse_container(engine.station).tail_seq
    assert after - before <= 1, f"5 сообщений (15 точек) дали {after - before} дельт"

    engine._persist_state(force=True)
    assert parse_container(engine.station).tail_seq == after + 1


def test_state_restored_from_container(store, cfg):
    first = Engine(store, cfg)
    first.ensure_layout()
    feed(first, "ctl_01", {"temp": 33.5})
    first._persist_state(force=True)

    second = Engine(store, cfg)
    second.ensure_layout()
    snap = second.snapshot()
    assert snap["latest"]["ctl_01"]["temp"]["value"] == 33.5
    assert snap["stats"]["state_restored"] == 1


def test_rotation_moves_station_to_archive(engine, store):
    limit = int(engine.cfg.get("station.rotate_after_deltas"))
    engine._deltas = limit
    engine._persist_state(force=True)

    archived = list((store / "archive").glob("station_01_*.eml"))
    assert len(archived) == 1
    assert engine.station.exists()
    assert parse_container(engine.station).tail_seq < limit
    assert engine.snapshot()["stats"]["station_rotated"] == 1
    # архивный файл заякорен уже под своим финальным именем
    assert engine.anchors.latest_for(f"archive/{archived[0].name}") is not None


# ---------------------------------------------------------------- целостность
def test_verify_passes_then_catches_tampered_station(engine, tmp_path):
    feed(engine, "ctl_01", {"temp": 21.0})
    first = engine.verify(deep=True, anchor=True)
    assert first["ok"], first
    assert first["anchored"]["seq"] == 1
    assert first["anchor_issues"] == []

    bad = tmp_path / "bad.eml"
    tamper_last_delta_consistently(engine.station, bad)
    shutil.copy2(bad, engine.station)      # подмена на месте

    rep = engine.verify(deep=True, anchor=False)
    assert rep["ok"] is False
    issues = [i for f in rep["files"] for i in f["structural"] + f["tool"] + f["anchor"]]
    assert any("anchor" in i for i in issues), issues


# ---------------------------------------------------------------- цикл run()
def test_engine_thread_consumes_bus(store, cfg):
    cfg.raw["station"]["state_persist_s"] = 0.3
    engine = Engine(store, cfg)
    engine.ensure_layout()
    engine.start()
    try:
        emlbox.bus_send(engine.bus, "ctl_01", emlbox.ENGINE_ADDR, "TELEMETRY",
                        {"cid": "ctl_01", "points": {"temp": 22.0},
                         "quality": "good", "ts": time.time()})
        assert wait_until(lambda: engine.snapshot()["stats"].get("msgs", 0) >= 1), \
            "движок не разобрал сообщение из почты"
        snap = engine.snapshot()
        assert snap["latest"]["ctl_01"]["temp"]["value"] == 22.0
        assert snap["stats"]["telemetry"] >= 1
        assert not drain(engine, address=emlbox.ENGINE_ADDR), "почта движка не опустела"
    finally:
        engine.stop.set()
        engine.join(timeout=5.0)
