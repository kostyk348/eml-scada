"""verify_demo.py — демонстрация tamper-evident (S2):
1. verify станции -> OK
2. копия станции; в копии флипается байт В ДЕЛЬТА-БЛОКЕ (правка журнала алармов)
3. verify копии -> BROKEN (hash-chain ловит вмешательство)

Флип в дельта-блоке (не в tail-индексе): verify хэширует base + каждый дельта-блок,
но НЕ tail index / trailer (см. src/verify.rs) — это известное ограничение mime-os.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from scada.emlbox import emlbox_bin, verify


def delta_regions(path: Path) -> list[tuple[int, int]]:
    """Парсим `emlbox mount`: [(off, len), ...] по дельтам."""
    out = subprocess.run([emlbox_bin(), "mount", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    regions = []
    for line in out.splitlines():
        m = re.search(r"seq=\d+\s+off=(\d+)\s+len=(\d+)", line)
        if m:
            regions.append((int(m.group(1)), int(m.group(2))))
    return regions


if __name__ == "__main__":
    store = Path(sys.argv[1] if len(sys.argv) > 1 else "store")
    station = store / "station_01.eml"
    if not station.exists():
        print(f"станция не найдена: {station}; сначала: python -m scada.run --headless --seconds 20")
        sys.exit(1)

    ok, lines = verify(station)
    print(f"[1] честная станция: verify = {lines}")
    assert ok, "честная станция обязана проходить verify"

    tampered = store / "station_01.tampered.eml"
    shutil.copy2(station, tampered)

    regions = delta_regions(tampered)
    if not regions:
        print("в станции нет дельт — сначала накопись (--seconds >= 20)")
        sys.exit(2)
    off, length = regions[0]
    flip_at = off + max(1, length // 2)  # середина тела дельты, не заголовок X-Prev-Hash
    print(f"[2] правка журнала: флип байта в дельта-блоке off={off} len={length} -> byte@{flip_at}")

    data = bytearray(tampered.read_bytes())
    data[flip_at] ^= 0xFF
    tampered.write_bytes(bytes(data))

    ok2, lines2 = verify(tampered)
    print(f"[3] подделанная копия: verify = {lines2}")
    if not ok2:
        print(">>> ВМЕШАТЕЛЬСТВО ОБНАРУЖЕНО: правка лога ловится hash-chain")
        sys.exit(0)
    print(">>> ВНИМАНИЕ: подделка НЕ обнаружена — проверь")
    sys.exit(3)
