from __future__ import annotations

import threading

from scada import emlbox


def test_publish_is_atomic_under_concurrency(tmp_path):
    """Читатель не должен видеть половину сообщения (tmp + os.replace)."""
    bus = tmp_path / "bus"
    stop = threading.Event()
    seen: set[str] = set()
    corrupt: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            try:
                for m in emlbox.bus_list(bus):
                    with lock:
                        if not m["ok"]:
                            corrupt.append(m["path"].name)
                        seen.add(m["path"].name)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    total = 80

    def writer(worker: int):
        for i in range(total // 4):
            emlbox.bus_send(bus, f"w{worker}", emlbox.ENGINE_ADDR, "TELEMETRY",
                            {"cid": f"w{worker}", "points": {"temp": 21.0 + i},
                             "quality": "good", "ts": 1000 + i, "pad": "x" * 200})

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    writers = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    t.join(timeout=5)

    # финальный проход: за время гонки читатель мог не дойти до части файлов
    # (каждый скан читает весь каталог), поэтому полноту проверяем после остановки
    for m in emlbox.bus_list(bus):
        with lock:
            if not m["ok"]:
                corrupt.append(m["path"].name)
            seen.add(m["path"].name)

    assert not errors, errors[:3]
    assert not corrupt, corrupt[:3]
    assert len(seen) == total, f"reader saw {len(seen)} of {total} messages"
    assert len(list(bus.glob("*.tmp"))) == 0


def test_address_filter_and_consume(tmp_path):
    bus = tmp_path / "bus"
    to_engine = emlbox.bus_send(bus, "ctl_01", emlbox.ENGINE_ADDR, "TELEMETRY",
                                {"cid": "ctl_01", "points": {"temp": 21.0}})
    to_hmi = emlbox.bus_send(bus, emlbox.ENGINE_ADDR, emlbox.HMI_ADDR, "ALARM",
                             {"id": "a1", "level": "HIGH"})

    engine_mail = emlbox.bus_list(bus, address=emlbox.ENGINE_ADDR)
    assert [m["path"] for m in engine_mail] == [to_engine]
    hmi_mail = emlbox.bus_list(bus, address=f"{emlbox.HMI_ADDR}@system.local")
    assert [m["path"] for m in hmi_mail] == [to_hmi]

    assert emlbox.bus_consume(to_engine) is True
    assert emlbox.bus_consume(to_engine) is False       # уже съедено
    assert (bus / (to_engine.name + ".done")).exists()
    assert emlbox.bus_list(bus, address=emlbox.ENGINE_ADDR) == []


def test_corrupt_message_is_reported_not_raised(tmp_path):
    bus = tmp_path / "bus"
    bus.mkdir()
    (bus / "00000000000000000001_1_00001.TELEMETRY.msg.eml").write_text(
        "From: <x>\r\nTo: <scada_engine>\r\nX-Event: TELEMETRY\r\n\r\n{broken", encoding="utf-8")
    msgs = emlbox.bus_list(bus, address=emlbox.ENGINE_ADDR)
    assert len(msgs) == 1
    assert msgs[0]["ok"] is False and msgs[0]["body"] == {}


def test_gc_removes_expired_done_and_tmp(tmp_path):
    bus = tmp_path / "bus"
    p = emlbox.bus_send(bus, "a", "b", "X", {"n": 1})
    emlbox.bus_consume(p)
    done = bus / (p.name + ".done")
    orphan_tmp = bus / "00000000000000000002_1_00002.X.msg.eml.tmp"
    orphan_tmp.write_text("partial", encoding="utf-8")

    removed = emlbox.bus_gc(bus, done_ttl_s=0, tmp_ttl_s=0)
    assert removed["done"] == 1 and removed["tmp"] == 1 and removed["bytes"] > 0
    assert not done.exists() and not orphan_tmp.exists()


def test_eml_parse_headers():
    headers, body = emlbox.parse_eml(
        b"From: <a@x>\r\nTo: <b>\r\nX-Event: PING\r\n\r\n{\"ok\":true}")
    assert headers["x-event"] == "PING"
    assert headers["to"] == "<b>"
    assert body == b'{"ok":true}'
