"""run.py — точка входа EML-SCADA v0.1.

Запуск HMI:        python -m scada.run --store store
Запуск без HMI:    python -m scada.run --store store --headless --seconds 8
Запуск демо:       python verify_demo.py store
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from .controllers import Controller
from .engine import Engine

log = logging.getLogger("scada")

# глобалы системы: стартуют в main(), страница HMI читает их
_engine: Engine | None = None
_store: Path | None = None


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)


def start_system(store: Path) -> tuple[Engine, list[Controller]]:
    store.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    engine = Engine(store, stop=stop)
    engine.ensure_layout()
    ctls = [Controller(cid, store / "bus", stop=stop) for cid in ("ctl_01", "ctl_02", "ctl_03")]
    for c in ctls:
        c.start()
    engine.start()
    return engine, ctls


def run_headless(store: Path, seconds: float) -> int:
    engine, _ = start_system(store)
    log.info("headless: гоняю %.1fс, потом verify", seconds)
    time.sleep(seconds)
    # демо связи контроллеров: движок -> ctl_02 setpoint
    from .emlbox import ENGINE_ADDR, bus_send
    bus_send(store / "bus", ENGINE_ADDR, "ctl_02", "SETPOINT",
             {"point": "flow", "value": 60, "ts": time.time()})
    time.sleep(2.5)
    snap = engine.snapshot()
    n_latest = sum(len(v) for v in snap["latest"].values())
    n_records = len(list((store / "hist").glob("*.eml")))
    ok, lines = engine.run_verify()
    ack_count = sum(1 for t in snap["traffic"] if t.get("event") == "SETPOINT_ACK")
    print("\n=== EML-SCADA headless summary ===")
    print(f"latest values: {n_latest} point(s)")
    print(f"historian records (.eml): {n_records}")
    print(f"alarms: {len(snap['alarms'])}")
    print(f"setpoint_ack on bus: {ack_count}")
    print(f"station verify: {lines}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="EML-SCADA v0.1")
    ap.add_argument("--store", default="store", help="рабочий каталог (bus/, hist/, station_01.eml)")
    ap.add_argument("--headless", action="store_true", help="без HMI: прогнать N секунд и выйти")
    ap.add_argument("--seconds", type=float, default=8.0, help="сколько секунд гонять в headless")
    ap.add_argument("--port", type=int, default=8080, help="порт HMI")
    args = ap.parse_args()

    setup_logging()
    store = Path(args.store)

    if args.headless:
        return run_headless(store, args.seconds)

    global _engine, _store
    _engine, _ = start_system(store)
    _store = store
    log.info("HMI стартует на http://127.0.0.1:%d", args.port)
    from nicegui import ui

    @ui.page("/")
    def index() -> None:
        from .hmi import Hmi
        hmi = Hmi(_engine, _store)
        hmi.build()

    ui.run(host="127.0.0.1", port=args.port, show=False, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())