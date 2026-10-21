from __future__ import annotations

from scada.alarms import AlarmEngine
from scada.config import Threshold

T = {"temp": Threshold(low=5.0, high=30.0, deadband=1.0)}


def engine(**kw) -> AlarmEngine:
    return AlarmEngine(T, realarm_s=kw.pop("realarm_s", 300.0), max_items=kw.pop("max_items", 100))


def test_raise_dedupe_clear_with_deadband():
    e = engine()
    assert e.evaluate("ctl_01", "temp", 21.0, ts=100) == []
    raised = e.evaluate("ctl_01", "temp", 35.0, ts=101)
    assert [ev["event"] for ev in raised] == ["ALARM"]
    assert raised[0]["level"] == "HIGH"
    assert e.stats()["active"] == 1

    # тот же уровень — не новый аларм (и напоминание ещё не время)
    assert e.evaluate("ctl_01", "temp", 36.0, ts=102) == []

    # вернулись в мёртвую зону: 29.5 > 30-1 -> аларм ещё держим
    assert e.evaluate("ctl_01", "temp", 29.5, ts=103) == []
    assert e.stats()["active"] == 1

    cleared = e.evaluate("ctl_01", "temp", 28.5, ts=104)
    assert [ev["event"] for ev in cleared] == ["ALARM_CLEAR"]
    assert cleared[0]["cleared_at"] == 104
    assert e.stats()["active"] == 0


def test_level_change_and_badq():
    e = engine()
    e.evaluate("ctl_01", "temp", 35.0, ts=1)
    events = e.evaluate("ctl_01", "temp", 3.0, ts=2)   # HIGH -> LOW
    assert [ev["event"] for ev in events] == ["ALARM_CLEAR", "ALARM"]
    assert events[1]["level"] == "LOW"

    events = e.evaluate("ctl_01", "temp", 21.0, quality="bad", ts=3)
    assert [ev["event"] for ev in events] == ["ALARM_CLEAR", "ALARM"]
    assert events[1]["level"] == "BADQ"

    cleared = e.evaluate("ctl_01", "temp", 21.0, quality="good", ts=4)
    assert [ev["event"] for ev in cleared] == ["ALARM_CLEAR"]


def test_unknown_point_never_alarms():
    e = engine()
    assert e.evaluate("ctl_02", "valve_pos", 10_000.0, ts=1) == []


def test_rearm_after_realarm_window():
    e = engine(realarm_s=10.0)
    e.evaluate("ctl_01", "temp", 35.0, ts=100)
    assert e.evaluate("ctl_01", "temp", 35.0, ts=105) == []
    repeat = e.evaluate("ctl_01", "temp", 36.0, ts=111)
    assert [ev["event"] for ev in repeat] == ["ALARM"]
    assert repeat[0]["repeat"] is True
    assert repeat[0]["repeated"] == 1
    assert e.stats()["active"] == 1     # напоминание не создаёт второй аларм


def test_ack_by_id_and_all():
    e = engine()
    a = e.evaluate("ctl_01", "temp", 35.0, ts=1)[0]
    other = e.evaluate("ctl_03", "temp", 40.0, ts=2)[0]
    assert {x.id for x in e.ack(alarm_id=a["id"], by="op")} == {a["id"]}
    assert e.ack(alarm_id=a["id"]) == []
    assert other["id"] != a["id"]

    acked = e.ack(all_=True, by="op2")
    assert len(acked) == 1
    assert acked[0].acked is True and acked[0].acked_by == "op2"

    recent = e.recent()
    assert recent[0]["acked"] is True


def test_ack_requires_target():
    e = engine()
    try:
        e.ack()
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("ack() без цели должен падать")


def test_max_items_is_bounded():
    e = engine(max_items=3)
    for i in range(10):
        e.evaluate("ctl_01", "temp", 35.0 + i, ts=100 + i)
        e.evaluate("ctl_01", "temp", 28.0, ts=100 + i + 0.5)
    assert len(e.recent()) <= 3
