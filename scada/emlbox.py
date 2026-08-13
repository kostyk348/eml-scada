"""emlbox.py — обёртка над emlbox CLI + собственная MIME-шина (Python).

CLI вызывается subprocess-списком (без проблем с кавычками PowerShell).
Шина дублирует формат src/ipc.rs, но позволяет задать From (CLI зашивает view).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_EMLBOX = Path(r"C:\Users\Lab\Documents\mime-os\target\release\emlbox.exe")
MSG_SUFFIX = ".msg.eml"
DONE_SUFFIX = ".done"

ENGINE_ADDR = "scada_engine"
HMI_ADDR = "scada_hmi"


def emlbox_bin() -> str:
    return os.environ.get("EMLBOX", str(DEFAULT_EMLBOX))


def run_cli(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """Запуск emlbox. Возвращает (code, stdout, stderr)."""
    proc = subprocess.run(
        [emlbox_bin(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------- контейнер
def create(path: Path, entity: str, subject: str) -> bool:
    code, out, err = run_cli(["create", str(path), entity, subject])
    if code != 0:
        print(f"[emlbox] create failed: {err or out}", file=sys.stderr)
    return code == 0


def kv_set(path: Path, table: str, key: str, value: Any) -> Optional[int]:
    code, out, err = run_cli(["kv", "set", str(path), table, key, json.dumps(value)])
    if code != 0:
        print(f"[emlbox] kv set {table}.{key}: {err or out}", file=sys.stderr)
        return None
    m = re.search(r"seq=(\d+)", out)
    return int(m.group(1)) if m else None


def kv_get(path: Path, table: str, key: str) -> Any:
    code, out, err = run_cli(["kv", "get", str(path), table, key])
    if code != 0 or out == "<missing>":
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def kv_dump(path: Path, table: str) -> dict:
    code, out, err = run_cli(["kv", "dump", str(path), table])
    if code != 0:
        print(f"[emlbox] kv dump {table}: {err or out}", file=sys.stderr)
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- tagdb
def tagdb_insert(db: Path, body: dict, rec_id: str, tags: list[str],
                 device: str, ts: int) -> bool:
    args = ["tagdb", "insert", str(db), json.dumps(body), "--id", rec_id]
    for t in tags:
        args += ["--tag", t]
    args += ["--device", device, "--ts", str(ts)]
    code, out, err = run_cli(args)
    if code != 0:
        print(f"[emlbox] tagdb insert {rec_id}: {err or out}", file=sys.stderr)
    return code == 0


def tagdb_query(db: Path, query: str) -> list[Path]:
    """Возвращает пути записей-хитов."""
    code, out, err = run_cli(["tagdb", "query", str(db), query])
    if code != 0:
        print(f"[emlbox] tagdb query: {err or out}", file=sys.stderr)
        return []
    hits = []
    for line in out.splitlines():
        m = re.match(r"\s*(.+\.eml)\s*\(.*\)", line)
        if m:
            hits.append(Path(m.group(1)))
    return hits


def read_eml(path: Path) -> tuple[dict[str, str], bytes]:
    """Читает .eml-запись/сообщение: (заголовки, тело)."""
    data = path.read_bytes()
    sep = b"\r\n\r\n" if b"\r\n\r\n" in data else b"\n\n"
    head_b, _, body_b = data.partition(sep)
    headers: dict[str, str] = {}
    for line in head_b.decode("utf-8", "replace").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers, body_b


# ---------------------------------------------------------------- IPC-шина
def bus_send(bus: Path, from_: str, to: str, event: str, body: dict) -> Path:
    """Пишет .eml-сообщение в spool (формат src/ipc.rs)."""
    bus.mkdir(parents=True, exist_ok=True)
    nanos = time.time_ns()
    safe_ev = re.sub(r"[\s/:]", "_", event)
    name = f"{nanos:020}_{os.getpid()}.{safe_ev}{MSG_SUFFIX}"
    path = bus / name
    buf = (f"From: <{from_}@system.local>\r\nTo: <{to}>\r\nX-Event: {event}\r\n"
           f"X-EMLBox-Msg: v1\r\nContent-Type: application/json\r\n\r\n")
    path.write_bytes(buf.encode("utf-8") + json.dumps(body).encode("utf-8"))
    return path


def bus_list(bus: Path) -> list[dict]:
    """Список непрочитанных сообщений: [{path, from, to, event, body}]."""
    out = []
    if not bus.is_dir():
        return out
    for p in sorted(bus.glob(f"*{MSG_SUFFIX}")):
        name = p.name
        if name.endswith(DONE_SUFFIX):
            continue
        headers, body_b = read_eml(p)
        try:
            body = json.loads(body_b.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        out.append({
            "path": p,
            "from": headers.get("from", "").strip("<>"),
            "to": headers.get("to", "").strip("<>"),
            "event": headers.get("x-event", ""),
            "body": body,
        })
    return out


def bus_consume(path: Path) -> None:
    """Потребление сообщения: rename -> .done."""
    try:
        path.rename(Path(str(path) + DONE_SUFFIX))
    except OSError:
        pass


# ---------------------------------------------------------------- verify
def verify(path: Path) -> tuple[bool, list[str]]:
    code, out, err = run_cli(["verify", str(path)])
    if code == 0:
        return True, [out]
    lines = (out + "\n" + err).splitlines()
    broken = [l for l in lines if l.strip()]
    return False, broken
