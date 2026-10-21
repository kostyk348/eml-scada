"""emlbox.py — обёртка над CLI emlbox (mime-os) + MIME-шина в Python.

CLI вызывается списком argv (без проблем с кавычками PowerShell). Шина —
spool-директория .eml-сообщений (формат src/ipc.rs), но с атомарной публикацией:
сообщение сначала пишется в `.tmp`, потом `os.replace` — читатель никогда не
увидит половину сообщения.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Путь по умолчанию — рядом собранный mime-os; переопределяется EMLBOX.
DEFAULT_EMLBOX = Path(r"C:\Users\Lab\Documents\mime-os\target\release\emlbox.exe")
MSG_SUFFIX = ".msg.eml"
DONE_SUFFIX = ".done"
TMP_SUFFIX = ".tmp"

ENGINE_ADDR = "scada_engine"
HMI_ADDR = "scada_hmi"

_seq = itertools.count(1)


class EmlboxMissing(RuntimeError):
    """Бинарь emlbox не найден — без него контейнеры не работают."""


def emlbox_bin() -> str:
    env = os.environ.get("EMLBOX")
    if env:
        return env
    if DEFAULT_EMLBOX.exists():
        return str(DEFAULT_EMLBOX)
    found = shutil.which("emlbox")
    if found:
        return found
    raise EmlboxMissing(
        "emlbox не найден: собери mime-os (cargo build --release) "
        f"или задай EMLBOX (искали {DEFAULT_EMLBOX} и PATH)"
    )


def check_binary() -> str:
    """Проверка на старте: лучше упасть сразу, чем на первом сообщении."""
    path = emlbox_bin()
    code, out, err = run_cli(["verify", os.devnull], timeout=10.0, check=False)
    if "unknown command" in (out + err).lower():
        raise EmlboxMissing(f"{path}: CLI не отвечает как emlbox")
    return path


def run_cli(args: list[str], timeout: float = 30.0, check: bool = False) -> tuple[int, str, str]:
    """Запуск emlbox. Возвращает (code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [emlbox_bin(), *args],
            stdin=subprocess.DEVNULL,   # не наследуем stdin: у сервиса/CI он бывает невалиден
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"emlbox {args[0] if args else ''}: timeout {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)
    if check and proc.returncode != 0:
        raise RuntimeError(f"emlbox {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
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
        # пустая таблица — это не ошибка для вызывающего
        return {}
    try:
        value = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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
        m = re.match(r"\s*(.+\.eml)\s+\(.*\)\s*$", line)
        if m:
            p = Path(m.group(1))
            hits.append(p if p.is_absolute() else db / p.name)
    return hits


def read_eml(path: Path) -> tuple[dict[str, str], bytes]:
    """Читает .eml-запись/сообщение: (заголовки, тело)."""
    data = path.read_bytes()
    return parse_eml(data)


def parse_eml(data: bytes) -> tuple[dict[str, str], bytes]:
    sep = b"\r\n\r\n" if b"\r\n\r\n" in data else b"\n\n"
    head_b, _, body_b = data.partition(sep)
    headers: dict[str, str] = {}
    for line in head_b.decode("utf-8", "replace").splitlines():
        if line[:1] in (" ", "\t") and headers:
            continue  # folding не нужен для наших сообщений
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers, body_b


# ---------------------------------------------------------------- IPC-шина
def _safe_event(event: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", event)[:40] or "EVENT"


def bus_send(bus: Path, from_: str, to: str, event: str, body: dict,
             ts_ns: int | None = None) -> Path:
    """Атомарно публикует .eml-сообщение в spool (tmp + replace)."""
    bus = Path(bus)
    bus.mkdir(parents=True, exist_ok=True)
    nanos = time.time_ns() if ts_ns is None else ts_ns
    name = f"{nanos:020}_{os.getpid()}_{next(_seq):05d}.{_safe_event(event)}{MSG_SUFFIX}"
    path = bus / name
    tmp = bus / (name + TMP_SUFFIX)
    buf = (f"From: <{from_}@system.local>\r\nTo: <{to}>\r\nX-Event: {event}\r\n"
           f"X-EMLBox-Msg: v1\r\nContent-Type: application/json\r\n\r\n")
    payload = buf.encode("utf-8") + json.dumps(body).encode("utf-8")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    return path


def bus_list(bus: Path, address: str | None = None) -> list[dict]:
    """Список непрочитанных сообщений: [{path, from, to, event, body, ok}].

    address — если задан, фильтруем по To: (свой почтовый ящик). Иначе все.
    """
    out = []
    bus = Path(bus)
    if not bus.is_dir():
        return out
    for p in sorted(bus.glob(f"*{MSG_SUFFIX}")):
        if p.name.endswith(DONE_SUFFIX):
            continue
        try:
            with open(p, "rb") as f:
                head_chunk = f.read(8192)
            headers, _ = parse_eml(head_chunk)
            to = headers.get("to", "").strip().strip("<>")
            if address is not None and address.split("@")[0] != to.split("@")[0]:
                continue  # чужая почта: тело даже не читаем
            headers, body_b = read_eml(p)
        except OSError:
            continue  # файл только что съеден другим потребителем (гонка) — пропуск
        try:
            body = json.loads(body_b.decode("utf-8"))
            ok = isinstance(body, dict)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body, ok = {}, False
        out.append({
            "path": p,
            "from": headers.get("from", "").strip().strip("<>"),
            "to": to,
            "event": headers.get("x-event", ""),
            "body": body,
            "ok": ok,
            "size": p.stat().st_size if p.exists() else 0,
        })
    return out


def bus_consume(path: Path) -> bool:
    """Потребление сообщения: rename -> .done. False, если уже съедено."""
    target = Path(str(path) + DONE_SUFFIX)
    try:
        os.replace(path, target)
        return True
    except OSError:
        return False


def bus_gc(bus: Path, done_ttl_s: float = 600.0, tmp_ttl_s: float = 60.0,
           now: float | None = None) -> dict:
    """Чистка спула: старые .done и осиротевшие .tmp. Возвращает счётчики."""
    bus = Path(bus)
    now = time.time() if now is None else now
    removed = {"done": 0, "tmp": 0, "bytes": 0}
    if not bus.is_dir():
        return removed
    for p in bus.iterdir():
        try:
            if p.name.endswith(MSG_SUFFIX + DONE_SUFFIX):
                ttl = done_ttl_s
                bucket = "done"
            elif p.name.endswith(TMP_SUFFIX):
                ttl = tmp_ttl_s
                bucket = "tmp"
            else:
                continue
            age = now - p.stat().st_mtime
            if age >= ttl:
                size = p.stat().st_size
                p.unlink()
                removed[bucket] += 1
                removed["bytes"] += size
        except OSError:
            continue  # гонка с другим потребителем — не наша проблема
    return removed


# ---------------------------------------------------------------- verify
def verify(path: Path) -> tuple[bool, list[str]]:
    code, out, err = run_cli(["verify", str(path)])
    if code == 0:
        return True, [out]
    lines = (out + "\n" + err).splitlines()
    return False, [l.strip() for l in lines if l.strip()]
