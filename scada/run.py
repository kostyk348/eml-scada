"""run.py — точка входа EML-SCADA 1.0.

    python -m scada.run                          # HMI на 127.0.0.1:8080
    python -m scada.run --store store --port 9000
    python -m scada.run headless --seconds 22 --json
    python -m scada.run verify [--deep] [--json]
    python -m scada.run demo                     # tamper-evident демо

Старый синтаксис (`--headless --seconds N`) сохранён.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError
from .controllers import Controller
from .engine import Engine
from .emlbox import HMI_ADDR, EmlboxMissing, bus_consume, bus_list

log = logging.getLogger("scada")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


class MailDrain(threading.Thread):
    """Съедает почту HMI, когда HMI нет (headless/CI): спул не должен копиться."""

    def __init__(self, bus: Path, stop: threading.Event, every: float = 0.5):
        super().__init__(daemon=True, name="mail-drain")
        self.bus = Path(bus)
        self.stop = stop
        self.every = every
        self.consumed = 0

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                for m in bus_list(self.bus, address=HMI_ADDR):
                    if bus_consume(m["path"]):
                        self.consumed += 1
            except Exception:
                pass
            self.stop.wait(self.every)


def start_system(store: Path, cfg: Config, faults: bool = True) -> tuple[Engine, list[Controller], threading.Event]:
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    if not faults:
        cfg.raw["sim"]["faults_enabled"] = False
    stop = threading.Event()
    engine = Engine(store, cfg, stop=stop)
    engine.ensure_layout()
    cfg.write_if_missing(store)
    ctls = [Controller(cid, store / "bus", cfg, stop=stop) for cid in cfg.controllers]
    for c in ctls:
        c.start()
    engine.start()
    return engine, ctls, stop


def shutdown(engine: Engine, ctls: list[Controller], stop: threading.Event) -> None:
    stop.set()
    for t in [*ctls, engine]:
        t.join(timeout=5.0)


# ---------------------------------------------------------------- headless
def run_headless(store: Path, cfg: Config, seconds: float, as_json: bool = False) -> int:
    engine, ctls, stop = start_system(store, cfg)
    drain = MailDrain(store / "bus", stop)
    drain.start()
    log.info("headless: %.1f s", seconds)
    try:
        time.sleep(seconds)
        engine.setpoint("ctl_02", "flow", 60.0, by="headless")
        time.sleep(2.5)  # контроллер подтверждает на своём тике, движок читает ACK
        snap = engine.snapshot()
        verify = engine.verify(deep=True, anchor=False)
    finally:
        shutdown(engine, ctls, stop)
        drain.join(timeout=2)

    hist_records = len(list((store / "hist").glob("*.eml")))
    ack_count = snap["stats"].get("setpoint_ack", 0)
    points = sum(len(v) for v in snap["latest"].values())
    report = {
        "version": __version__,
        "store": str(store),
        "points": points,
        "historian_records": hist_records,
        "alarms_active": snap["alarm_stats"]["active"],
        "alarms_raised": snap["stats"].get("alarm_raise", 0),
        "alarms_cleared": snap["stats"].get("alarm_clear", 0),
        "setpoint_ack": ack_count,
        "messages": snap["stats"].get("msgs", 0),
        "bad_messages": snap["stats"].get("bad_msg", 0),
        "verify_ok": verify["ok"],
        "verify_files": [{"file": f["file"], "ok": f["ok"], "deltas": f["deltas"],
                          "issues": f["structural"] + f["tool"] + f["anchor"]}
                         for f in verify["files"]],
        "anchor_issues": verify["anchor_issues"],
    }
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("\n=== EML-SCADA headless summary ===")
        print(f"latest values:        {points} point(s)")
        print(f"historian records:    {hist_records} (.eml)")
        print(f"alarms:               active={report['alarms_active']} "
              f"raised={report['alarms_raised']} cleared={report['alarms_cleared']}")
        print(f"setpoint_ack on bus:  {ack_count}")
        print(f"bus messages:         {report['messages']} (bad {report['bad_messages']})")
        for f in report["verify_files"]:
            mark = "OK" if f["ok"] else "BROKEN"
            print(f"verify {f['file']}: {mark} ({f['deltas']} delta(s))")
            for issue in f["issues"]:
                print(f"  - {issue}")
        if report["anchor_issues"]:
            print("anchor:", "; ".join(report["anchor_issues"]))
    return 0 if verify["ok"] else 1


# ---------------------------------------------------------------- verify
def run_verify(store: Path, cfg: Config, deep: bool = True, as_json: bool = False) -> int:
    store = Path(store)
    if not store.exists():
        print(f"нет стора: {store}", file=sys.stderr)
        return 2
    engine = Engine(store, cfg)
    result = engine.verify(deep=deep, anchor=False)
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"store: {store}")
        for f in result["files"]:
            mark = "OK" if f["ok"] else "BROKEN"
            print(f"  {f['file']:<32} {mark:<7} {f['size']:>9} B  "
                  f"deltas={f['deltas']:<6} sha256={f['sha256'][:16]}…")
            for issue in f["structural"] + f["tool"] + f["anchor"]:
                print(f"    ! {issue}")
        print(f"  anchors: {result['anchor_records']} record(s)"
              + ("" if not result["anchor_issues"] else " — ПРОБЛЕМЫ"))
        for issue in result["anchor_issues"]:
            print(f"    ! {issue}")
        print("RESULT:", "OK" if result["ok"] else "BROKEN")
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------- HMI
def run_hmi(store: Path, cfg: Config, host: str | None, port: int | None) -> int:
    from nicegui import ui

    from .api import register_api
    from .hmi import Hmi

    engine, ctls, stop = start_system(store, cfg)
    register_api(engine)
    host = host or str(cfg.get("hmi.host"))
    port = int(port or cfg.get("hmi.port"))
    log.info("HMI: http://%s:%d  (store=%s)", host, port, store)

    @ui.page("/")
    def index() -> None:
        Hmi(engine, cfg).build()

    try:
        ui.run(host=host, port=port, show=False, reload=False,
               title="EML-SCADA 1.0", favicon="📧")
    finally:
        shutdown(engine, ctls, stop)
    return 0


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scada", description=f"EML-SCADA {__version__}")
    ap.add_argument("--version", action="version", version=f"EML-SCADA {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--store", default="store", help="рабочий каталог")
        p.add_argument("--config", default=None, help="путь к config.json")
        p.add_argument("-v", "--verbose", action="store_true")

    p_run = sub.add_parser("run", help="HMI (по умолчанию)")
    common(p_run)
    p_run.add_argument("--host", default=None)
    p_run.add_argument("--port", type=int, default=None)
    p_run.add_argument("--headless", action="store_true", help="без HMI (старый синтаксис)")
    p_run.add_argument("--seconds", type=float, default=8.0)
    p_run.add_argument("--json", action="store_true")
    p_run.add_argument("--no-faults", action="store_true", help="выключить имитацию дефектов")

    p_head = sub.add_parser("headless", help="прогон без GUI")
    common(p_head)
    p_head.add_argument("--seconds", type=float, default=8.0)
    p_head.add_argument("--json", action="store_true")
    p_head.add_argument("--no-faults", action="store_true")

    p_ver = sub.add_parser("verify", help="проверка целостности")
    common(p_ver)
    p_ver.add_argument("--deep", dest="deep", action="store_true", default=True)
    p_ver.add_argument("--no-deep", dest="deep", action="store_false")
    p_ver.add_argument("--json", action="store_true")

    p_demo = sub.add_parser("demo", help="tamper-evident демо")
    common(p_demo)
    return ap


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    try:
        cfg = Config.load(Path(args.store), Path(args.config) if args.config else None)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "verify":
            return run_verify(Path(args.store), cfg, deep=args.deep, as_json=args.json)
        if args.cmd == "demo":
            from .demo import run as run_demo
            return run_demo(Path(args.store), cfg)
        if args.cmd == "headless" or getattr(args, "headless", False):
            return run_headless(Path(args.store), cfg, args.seconds, as_json=args.json)
        return run_hmi(Path(args.store), cfg, getattr(args, "host", None),
                       getattr(args, "port", None))
    except (EmlboxMissing, RuntimeError) as e:
        print(f"fatal: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
