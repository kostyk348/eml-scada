"""demo.py — демонстрация tamper-evident свойств (шаг 4 README).

Сценарий:
  1) честная станция проходит deep verify (структура + emlbox + якорь);
  2) правка тела дельты (обычный случай) ловится emlbox verify;
  3) правка ПОСЛЕДНЕЙ дельты с пересчётом её хэша в tail index и X-Tail-Hash в
     трейлере — emlbox verify говорит OK (известное ограничение: tail index и
     трейлер не хэшируются), а deep verify с якорем ловит подмену.

Возвращает 0, если поведение совпало с ожидаемым (все проверки сработали).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from . import emlbox
from .config import Config
from .integrity import AnchorLog, deep_verify, parse_container

TRAILER_SIZE = 512


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _patch_tail_index(data: bytearray, entry_index: int, new_hash: str,
                      tail_off: int, tail_len: int) -> None:
    tail = json.loads(bytes(data[tail_off:tail_off + tail_len]).decode("utf-8"))
    tail["entries"][entry_index]["hash"] = new_hash
    blob = json.dumps(tail, separators=(",", ":")).encode("utf-8")
    if len(blob) != tail_len:
        raise RuntimeError(f"tail index изменил длину ({len(blob)} != {tail_len})")
    data[tail_off:tail_off + tail_len] = blob


def _patch_trailer_hash(data: bytearray, new_hash: str) -> None:
    tail = bytes(data[-TRAILER_SIZE:]).decode("utf-8")
    updated = re.sub(r"X-Tail-Hash: [0-9a-f]+", f"X-Tail-Hash: {new_hash}", tail, count=1)
    blob = updated.encode("utf-8")
    if len(blob) != TRAILER_SIZE:
        raise RuntimeError("трейлер изменил длину")
    data[-TRAILER_SIZE:] = blob


def tamper_delta_body(src: Path, dst: Path, which: str = "last") -> dict:
    """Меняет цифру в теле дельты, не трогая хэши (обычная порча журнала)."""
    shutil.copy2(src, dst)
    c = parse_container(dst)
    entry = c.entries[-1] if which == "last" else c.entries[0]
    data = bytearray(dst.read_bytes())
    block = bytes(data[entry["off"]:entry["off"] + entry["len"]])
    m = re.search(rb'"value"\s*:\s*(-?\d)', block)
    if not m:
        raise RuntimeError("в дельте нет числа в value — нечего портить")
    at = entry["off"] + m.start(1)
    data[at] = ord("7") if data[at] != ord("7") else ord("3")
    dst.write_bytes(bytes(data))
    return {"file": dst.name, "delta": entry["seq"], "byte": at,
            "old": chr(block[m.start(1)]), "new": chr(data[at])}


def tamper_last_delta_consistently(src: Path, dst: Path,
                                   delta_index: int | None = None) -> dict:
    """Правит дельту (по умолчанию последнюю) и пересчитывает её хэш везде, где он записан.

    `delta_index` — 0-based индекс дельты; произвольный индекс нужен, чтобы
    проверить якорь на заякоренном префиксе (перезапись «старой» истории).
    """
    shutil.copy2(src, dst)
    c = parse_container(dst)
    data = bytearray(dst.read_bytes())
    index = len(c.entries) - 1 if delta_index is None else int(delta_index)
    if not 0 <= index < len(c.entries):
        raise ValueError(f"нет дельты с индексом {index} (всего {len(c.entries)})")
    entry = c.entries[index]
    off, length = entry["off"], entry["len"]
    block = bytes(data[off:off + length])
    m = re.search(rb'"value"\s*:\s*(-?\d)', block)
    if not m:
        raise RuntimeError("в дельте нет числа в value")
    data[off + m.start(1)] = ord("9") if data[off + m.start(1)] != ord("9") else ord("1")
    new_block = bytes(data[off:off + length])
    new_hash = _sha(new_block)
    _patch_tail_index(data, index, new_hash, c.tail_index_off, c.tail_index_len)
    if index == len(c.entries) - 1:
        _patch_trailer_hash(data, new_hash)
    dst.write_bytes(bytes(data))
    return {"file": dst.name, "delta": entry["seq"], "new_hash": new_hash}


def run(store: Path, cfg: Config | None = None) -> int:
    store = Path(store)
    cfg = cfg or Config.load(store)
    station = store / f"{cfg.get('station.name')}.eml"
    if not station.exists():
        print(f"[!] станции нет: {station}")
        print("    сначала накопи дельты: python -m scada.run headless --store", store, "--seconds 8")
        return 2

    anchors = AnchorLog(store / "anchor.eml", store=store)
    base = deep_verify(station, anchor=anchors.latest_for(anchors.rel(station)))
    if base.deltas < 2:
        print(f"[!] в станции {base.deltas} дельта(ы) — мало для демо, погоняй движок ещё")
        return 2

    print(f"[0] станция: {station.name}, {base.size} B, дельт {base.deltas}")
    ok = True

    # --- 1. честная станция
    anchor = anchors.append(station)
    print(f"[1] создан якорь seq={anchor.seq} sha256={anchor.sha256[:16]}…")
    rep = deep_verify(station, anchor=anchor)
    print(f"[1] честная станция: deep verify = {'OK' if rep.ok else 'BROKEN'}")
    if not rep.ok:
        print("    ", rep.issues)
        ok = False

    # --- 2. правка тела обычной дельты
    t1 = store / "station_01.tampered.eml"
    info = tamper_delta_body(station, t1, which="last")
    tool_ok, tool_lines = emlbox.verify(t1)
    deep = deep_verify(t1, anchor=anchor)
    print(f"[2] порча тела дельты seq={info['delta']} (байт {info['byte']}): "
          f"emlbox={'OK' if tool_ok else 'BROKEN'}, deep={'OK' if deep.ok else 'BROKEN'}")
    if tool_ok or deep.ok:
        print("    ВНИМАНИЕ: обычная порча не поймана")
        ok = False

    # --- 3. согласованная подмена последней дельты (обход emlbox verify)
    t2 = store / "station_01.tailcut.eml"
    info2 = tamper_last_delta_consistently(station, t2)
    tool_ok2, tool_lines2 = emlbox.verify(t2)
    deep2 = deep_verify(t2, anchor=anchor)
    print(f"[3] подмена последней дельты seq={info2['delta']} + пересчёт хэшей: "
          f"emlbox={'OK' if tool_ok2 else 'BROKEN'}, deep={'OK' if deep2.ok else 'BROKEN'}")
    if tool_ok2 and deep2.ok:
        print("    ВНИМАНИЕ: подмена не поймана ни одним уровнем")
        ok = False
    if not tool_ok2:
        print("    (проверь: ожидалось, что emlbox verify это НЕ ловит — так и задумано в mime-os)")

    print()
    print("[4] голова журнала якорей (для офсайт-копии):")
    print("   ", json.dumps(anchors.export_head(), ensure_ascii=False))
    for f in (t1, t2):
        try:
            f.unlink()
        except OSError:
            pass
    print()
    print(">>> TAMPER-EVIDENT: OK" if ok else ">>> ПРОВЕРКА НЕ ПРОШЛА")
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    store = Path(argv[0]) if argv else Path("store")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        return run(store)
    except Exception as e:
        print(f"demo failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
