"""Ретенция историка (возраст + лимит записей) и спула шины (.done/.tmp TTL)."""
from __future__ import annotations

import os
import time

from scada import emlbox
from scada.config import Config
from scada.retention import prune_all, prune_bus, prune_historian


def make_file(path, *, age_s: float = 0.0, size: int = 0,
              now: float | None = None):
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = now - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_prune_historian_by_age(tmp_path):
    now = time.time()
    hist = tmp_path / "hist"
    old = make_file(hist / "old.eml", age_s=3 * 3600, size=100, now=now)
    fresh = make_file(hist / "fresh.eml", age_s=60, size=50, now=now)

    res = prune_historian(hist, keep_hours=1.0, max_records=1000, now=now)
    assert (res.files, res.bytes, res.errors) == (1, 100, 0)
    assert not old.exists() and fresh.exists()


def test_prune_historian_by_max_records_keeps_newest(tmp_path):
    now = time.time()
    hist = tmp_path / "hist"
    files = [make_file(hist / f"{i:02d}.eml", age_s=10 * (10 - i), size=10, now=now)
             for i in range(10)]

    res = prune_historian(hist, keep_hours=24.0, max_records=4, now=now)
    assert res.files == 6
    left = sorted(p.name for p in hist.glob("*.eml"))
    assert left == ["06.eml", "07.eml", "08.eml", "09.eml"]
    assert all(p.exists() for p in files[6:])


def test_prune_historian_ignores_other_files_and_missing_dir(tmp_path):
    hist = tmp_path / "hist"
    make_file(hist / "index.kv", age_s=10 * 3600, size=5)
    make_file(hist / "note.txt", age_s=10 * 3600, size=5)

    res = prune_historian(hist, keep_hours=1.0, max_records=10)
    assert (res.files, res.errors) == (0, 0)
    assert (hist / "index.kv").exists() and (hist / "note.txt").exists()

    missing = prune_historian(tmp_path / "nope", keep_hours=1.0, max_records=10)
    assert (missing.files, missing.bytes, missing.errors) == (0, 0, 0)


def test_prune_historian_max_records_zero_removes_all(tmp_path):
    hist = tmp_path / "hist"
    for i in range(3):
        make_file(hist / f"{i}.eml", size=1)
    res = prune_historian(hist, keep_hours=24.0, max_records=0)
    assert res.files == 3
    assert list(hist.glob("*.eml")) == []


def test_prune_bus_expires_done_and_orphan_tmp(tmp_path):
    now = time.time()
    bus = tmp_path / "bus"

    done_path = emlbox.bus_send(bus, "a", emlbox.HMI_ADDR, "ALARM", {"id": "x"})
    assert emlbox.bus_consume(done_path)
    old_done = bus / (done_path.name + ".done")
    os.utime(old_done, (now - 3600, now - 3600))

    fresh_done_src = emlbox.bus_send(bus, "b", emlbox.HMI_ADDR, "ALARM", {"id": "y"})
    emlbox.bus_consume(fresh_done_src)

    stale_tmp = make_file(bus / "0009_1_1.PING.msg.eml.tmp", age_s=600, now=now)
    fresh_tmp = make_file(bus / "0010_1_2.PING.msg.eml.tmp", age_s=1, now=now)

    res = prune_bus(bus, done_ttl_s=600.0, tmp_ttl_s=60.0, now=now)
    assert res.files == 2 and res.errors == 0
    assert not old_done.exists() and not stale_tmp.exists()
    assert fresh_done_src.with_name(fresh_done_src.name + ".done").exists()
    assert fresh_tmp.exists()


def test_prune_all_uses_config_defaults(tmp_path):
    now = time.time()
    store = tmp_path / "store"
    hist = store / "hist"
    bus = store / "bus"

    make_file(hist / "ancient.eml", age_s=48 * 3600, size=7, now=now)
    make_file(hist / "recent.eml", age_s=60, size=3, now=now)

    sent = emlbox.bus_send(bus, "x", emlbox.HMI_ADDR, "ALARM", {"id": "z"})
    emlbox.bus_consume(sent)
    old_done = bus / (sent.name + ".done")
    os.utime(old_done, (now - 24 * 3600, now - 24 * 3600))

    cfg = Config.load(store)        # без config.json -> встроенные дефолты
    assert cfg.get("historian.keep_hours") == 24.0

    res = prune_all(store, cfg, now=now)
    assert res["historian"]["files"] == 1
    assert res["historian"]["bytes"] == 7
    assert res["bus"]["files"] == 1
    assert not old_done.exists()
    assert (hist / "recent.eml").exists()


def test_prune_all_on_empty_store_is_noop(tmp_path):
    cfg = Config.load(tmp_path)
    res = prune_all(tmp_path / "empty", cfg)
    assert res["historian"]["files"] == 0 and res["bus"]["files"] == 0
    assert res["historian"]["errors"] == 0
