"""integrity.py — независимая проверка контейнера .eml + журнал якорей.

Зачем: `emlbox verify` пересчитывает base-хэш и цепочку дельт, но НЕ хэширует
tail index и trailer. Значит последнюю дельту можно переписать, поправить её
хэш в tail index и X-Tail-Hash в трейлере — и verify скажет OK (известное
ограничение mime-os, п.1 в README).

Здесь два дополнения, оба на чистом Python (независимая реализация формата):

1. Структурная проверка: границы секций, непрерывность дельт, инварианты
   трейлера, совпадение X-Delta-Seq/X-Entity-ID, отсутствие хвоста после
   трейлера, tail index ровно перед трейлером.
2. Якоря (`store/anchor.eml`): после проверки/ротации в append-only журнал
   пишется sha256 файла + base/tail-хэши. Якорь фиксирует префикс истории:
   рост (новые дельты) допустим только как продолжение заякоренной цепочки;
   перезапись последней дельты или base ловится.

Честная граница: якорь лежит на той же машине. Он защищает от правки станции
без доступа к журналу якорей, но не от того, кто перепишет оба файла.
Для этого есть export_head() — голову цепочки можно унести в коммит/почту/лог.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import emlbox

TRAILER_SIZE = 512
TRAILER_MARKER = "X-EMLBox-Trailer: v1"
ENVELOPE_SCAN_LIMIT = 65536


class IntegrityError(ValueError):
    """Файл не является контейнером EMLBox или повреждён структурно."""


# ---------------------------------------------------------------- парсинг
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _find_blank(data: bytes, start: int = 0) -> tuple[int, int] | None:
    i = data.find(b"\r\n\r\n", start)
    j = data.find(b"\n\n", start)
    if i < 0 and j < 0:
        return None
    if i < 0:
        return j, 2
    if j < 0:
        return i, 4
    return (i, 4) if i <= j else (j, 2)


def _parse_headers(head: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in head.decode("utf-8", "replace").splitlines():
        if not raw or raw[0] in " \t":
            continue
        if ":" in raw:
            k, _, v = raw.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def _parse_trailer(block: bytes) -> dict[str, str]:
    text = block.decode("utf-8", "replace")
    pos = text.find(TRAILER_MARKER)
    if pos < 0:
        raise IntegrityError("trailer: маркер X-EMLBox-Trailer не найден")
    out: dict[str, str] = {}
    for line in text[pos:].splitlines():
        if line.startswith("X-") and ": " in line:
            k, _, v = line.partition(": ")
            out[k.strip().lower()] = v.strip()
        elif out:
            break  # дошли до padding
    return out


@dataclass
class Container:
    path: Path
    data: bytes
    headers: dict[str, str]
    sections: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    trailer: dict[str, str]
    tail_index_off: int
    tail_index_len: int

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def entity(self) -> str:
        return self.headers.get("x-entity-id", "")

    @property
    def base_end(self) -> int:
        return self.entries[0]["off"] if self.entries else self.tail_index_off

    @property
    def base_hash(self) -> str:
        return self.trailer.get("x-base-hash", "")

    @property
    def tail_seq(self) -> int:
        return len(self.entries)

    @property
    def tail_hash(self) -> str:
        return self.entries[-1]["hash"] if self.entries else ""


def parse_container(path: Path) -> Container:
    path = Path(path)
    data = path.read_bytes()
    if len(data) < TRAILER_SIZE + 32:
        raise IntegrityError(f"{path.name}: файл короче трейлера ({len(data)} B)")

    head = _find_blank(data, 0)
    if head is None or head[0] > ENVELOPE_SCAN_LIMIT:
        raise IntegrityError(f"{path.name}: не найден конец envelope")
    headers = _parse_headers(data[:head[0]])

    trailer = _parse_trailer(data[-TRAILER_SIZE:])
    for key in ("x-tail-index-offset", "x-tail-index-length", "x-base-hash", "x-tail-seq"):
        if key not in trailer:
            raise IntegrityError(f"{path.name}: в трейлере нет {key}")
    try:
        tio = int(trailer["x-tail-index-offset"])
        til = int(trailer["x-tail-index-length"])
    except ValueError as e:
        raise IntegrityError(f"{path.name}: трейлер: битые оффсеты ({e})") from e

    if not 0 <= tio <= len(data) - TRAILER_SIZE or til < 0 or tio + til != len(data) - TRAILER_SIZE:
        raise IntegrityError(
            f"{path.name}: tail index [{tio}, {tio + til}) не примыкает к трейлеру (size={len(data)})")
    try:
        tail = json.loads(data[tio:tio + til].decode("utf-8"))
        entries = tail["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries is not a list")
        entries = [{"seq": int(e["seq"]), "off": int(e["off"]),
                    "len": int(e["len"]), "hash": str(e["hash"])} for e in entries]
    except (ValueError, KeyError, TypeError) as e:
        raise IntegrityError(f"{path.name}: tail index не парсится ({e})") from e

    sections: list[dict[str, Any]] = []
    idx_off = int(headers.get("x-index-offset", "-1") or -1)
    idx_len = int(headers.get("x-index-length", "-1") or -1)
    if 0 <= idx_off and idx_len >= 0 and idx_off + idx_len <= len(data):
        try:
            head_index = json.loads(data[idx_off:idx_off + idx_len].decode("utf-8"))
            sections = head_index.get("sections", [])
        except (ValueError, TypeError):
            sections = []
    else:
        raise IntegrityError(f"{path.name}: X-Index-Offset/Length вне файла")

    return Container(path=path, data=data, headers=headers, sections=sections,
                     entries=entries, trailer=trailer,
                     tail_index_off=tio, tail_index_len=til)


# ---------------------------------------------------------------- проверки
def _structural_issues(c: Container) -> list[str]:
    issues: list[str] = []
    n = c.size
    base_end = c.base_end

    if not c.entity:
        issues.append("envelope: нет X-Entity-ID")

    prev_end = 0
    for s in c.sections:
        try:
            off, length = int(s["off"]), int(s["len"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"head index: битая секция {s!r}")
            continue
        if off < prev_end:
            issues.append(f"section '{s.get('id', '?')}': перекрытие/непорядок")
        if off + length > base_end:
            issues.append(f"section '{s.get('id', '?')}': выходит за base ({off + length} > {base_end})")
        prev_end = off + length

    # дельты: непрерывны, внутри [base_end, tail_index_off)
    expect_off = base_end
    expect_seq = 1
    for e in c.entries:
        if e["seq"] != expect_seq:
            issues.append(f"delta seq: ожидали {expect_seq}, в индексе {e['seq']}")
        if e["off"] != expect_off:
            issues.append(f"delta {e['seq']}: дыра/перекрытие (off={e['off']}, ожидали {expect_off})")
        if e["off"] + e["len"] > c.tail_index_off:
            issues.append(f"delta {e['seq']}: выходит за tail index")
        if e["len"] <= 0:
            issues.append(f"delta {e['seq']}: пустой блок")
        expect_off = e["off"] + e["len"]
        expect_seq += 1

    if c.entries and expect_off != c.tail_index_off:
        issues.append(f"delta-регион заканчивается на {expect_off}, tail index с {c.tail_index_off}")

    try:
        if int(c.trailer["x-tail-seq"]) != c.tail_seq:
            issues.append(f"трейлер: X-Tail-Seq={c.trailer['x-tail-seq']} != {c.tail_seq}")
    except ValueError:
        issues.append("трейлер: X-Tail-Seq не число")

    declared_tail = c.trailer.get("x-tail-hash", "")
    if c.tail_seq:
        if declared_tail and declared_tail != c.tail_hash:
            issues.append("трейлер: X-Tail-Hash != хэш последней дельты")
    elif declared_tail:
        issues.append("трейлер: X-Tail-Hash не пуст, а дельт нет")

    # base hash
    got = _sha256_hex(c.data[:base_end])
    if got != c.base_hash:
        issues.append(f"base hash mismatch: got {got[:16]}…, trailer {c.base_hash[:16]}…")

    # цепочка дельт
    expect_hash = c.base_hash
    for e in c.entries:
        block = c.data[e["off"]:e["off"] + e["len"]]
        if len(block) != e["len"]:
            issues.append(f"delta {e['seq']}: блок обрезан")
            continue
        block_hash = _sha256_hex(block)
        if block_hash != e["hash"]:
            issues.append(f"delta {e['seq']}: hash mismatch")
        bh = _find_blank(block, 0)
        bheaders = _parse_headers(block[:bh[0]]) if bh else {}
        if bheaders.get("x-prev-hash", "") != expect_hash:
            issues.append(f"delta {e['seq']}: разрыв chain (X-Prev-Hash)")
        if bheaders.get("x-delta-seq", "") != str(e["seq"]):
            issues.append(f"delta {e['seq']}: X-Delta-Seq не совпадает")
        if c.entity and bheaders.get("x-entity-id", "") != c.entity:
            issues.append(f"delta {e['seq']}: чужой X-Entity-ID")
        expect_hash = block_hash

    return issues


@dataclass
class Report:
    path: Path
    ok: bool
    size: int = 0
    entity: str = ""
    deltas: int = 0
    sha256: str = ""
    base_hash: str = ""
    tail_hash: str = ""
    structural: list[str] = field(default_factory=list)
    tool: list[str] = field(default_factory=list)
    anchor: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def issues(self) -> list[str]:
        return self.structural + self.tool + self.anchor

    def as_dict(self) -> dict:
        return {
            "file": self.path.name,
            "ok": self.ok,
            "size": self.size,
            "entity": self.entity,
            "deltas": self.deltas,
            "sha256": self.sha256,
            "base_hash": self.base_hash,
            "tail_hash": self.tail_hash,
            "structural": self.structural,
            "tool": self.tool,
            "anchor": self.anchor,
            "notes": self.notes,
        }


def deep_verify(path: Path, anchor: "AnchorRecord | None" = None,
                run_tool: bool = True) -> Report:
    path = Path(path)
    rep = Report(path=path, ok=False)
    try:
        c = parse_container(path)
    except IntegrityError as e:
        rep.structural = [str(e)]
        return rep

    rep.size = c.size
    rep.entity = c.entity
    rep.deltas = c.tail_seq
    rep.base_hash = c.base_hash
    rep.tail_hash = c.tail_hash
    rep.sha256 = _sha256_hex(c.data)
    rep.structural = _structural_issues(c)

    if run_tool:
        tool_ok, tool_lines = emlbox.verify(path)
        if not tool_ok:
            rep.tool = [l for l in tool_lines if l.lower() != "broken"] or ["emlbox verify: BROKEN"]
        else:
            rep.notes.append("emlbox verify: OK")

    if anchor is not None:
        rep.anchor = anchor.check(c, rep.sha256)

    rep.ok = not rep.issues
    return rep


# ---------------------------------------------------------------- якоря
@dataclass
class AnchorRecord:
    seq: int
    prev: str
    ts_ms: int
    file: str
    size: int
    sha256: str
    base_hash: str
    tail_seq: int
    tail_hash: str
    self: str = ""

    @property
    def ts(self) -> float:
        """Время якоря в секундах (для API/выгрузки)."""
        return self.ts_ms / 1000.0

    def payload(self) -> dict:
        # ts — целые миллисекунды: JSON-float не переживает round-trip через
        # контейнер без потери последнего бита, а self-hash должен сходиться.
        return {"seq": self.seq, "prev": self.prev, "ts": self.ts_ms,
                "file": self.file, "size": self.size, "sha256": self.sha256,
                "base_hash": self.base_hash, "tail_seq": self.tail_seq,
                "tail_hash": self.tail_hash}

    def digest(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode()
        return _sha256_hex(blob)

    def as_dict(self) -> dict:
        return {**self.payload(), "self": self.self or self.digest()}

    def check(self, c: Container, digest: str | None = None) -> list[str]:
        """Сверка текущего контейнера с заякоренным префиксом истории."""
        issues: list[str] = []
        if c.base_hash != self.base_hash:
            issues.append("anchor: base hash не совпадает (переписана base-часть)")
        if c.tail_seq < self.tail_seq:
            issues.append(f"anchor: дельт стало меньше ({c.tail_seq} < {self.tail_seq}) — усечение")
        elif c.tail_seq == self.tail_seq:
            if digest is not None and digest != self.sha256:
                issues.append("anchor: sha256 файла не совпадает (правка tail index/трейлера)")
        elif self.tail_seq > 0:
            # рост допустим только как продолжение заякоренной цепочки
            anchored = c.entries[self.tail_seq - 1]
            if anchored["hash"] != self.tail_hash:
                issues.append("anchor: хэш заякоренной дельты изменился — история переписана")
        return issues


class AnchorLog:
    """Append-only журнал якорей в контейнере: таблица chain, ключ = seq."""

    def __init__(self, path: Path, store: Path | None = None):
        self.path = Path(path)
        self.store = Path(store) if store is not None else self.path.parent
        self._lock = threading.Lock()

    # --- служебное
    def ensure(self) -> None:
        if not self.path.exists():
            if not emlbox.create(self.path, "anchors@system.local", "EML-SCADA integrity anchors"):
                raise RuntimeError(f"не удалось создать журнал якорей {self.path}")

    def rel(self, target: Path) -> str:
        target = Path(target)
        try:
            return target.resolve().relative_to(self.store.resolve()).as_posix()
        except ValueError:
            return target.name

    def records(self) -> list[AnchorRecord]:
        if not self.path.exists():
            return []
        raw = emlbox.kv_dump(self.path, "chain")
        out: list[AnchorRecord] = []
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                rec = AnchorRecord(
                    seq=int(value["seq"]), prev=str(value.get("prev", "")),
                    ts_ms=int(value.get("ts", 0)), file=str(value.get("file", "")),
                    size=int(value.get("size", 0)), sha256=str(value.get("sha256", "")),
                    base_hash=str(value.get("base_hash", "")),
                    tail_seq=int(value.get("tail_seq", 0)),
                    tail_hash=str(value.get("tail_hash", "")),
                    self=str(value.get("self", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if rec.seq != int(key):
                continue
            out.append(rec)
        return sorted(out, key=lambda r: r.seq)

    def head(self) -> AnchorRecord | None:
        recs = self.records()
        return recs[-1] if recs else None

    def latest_for(self, file: str) -> AnchorRecord | None:
        for rec in reversed(self.records()):
            if rec.file == file:
                return rec
        return None

    # --- запись
    def append(self, target: Path, ts: float | None = None) -> AnchorRecord:
        """Ставит якорь. `ts` — секунды (по умолчанию now), в журнал идёт в мс."""
        with self._lock:
            self.ensure()
            c = parse_container(target)
            head = self.head()
            rec = AnchorRecord(
                seq=(head.seq + 1) if head else 1,
                prev=(head.self or head.digest()) if head else "",
                ts_ms=int(round((time.time() if ts is None else ts) * 1000)),
                file=self.rel(target),
                size=c.size,
                sha256=_sha256_hex(c.data),
                base_hash=c.base_hash,
                tail_seq=c.tail_seq,
                tail_hash=c.tail_hash,
            )
            rec.self = rec.digest()
            seq = emlbox.kv_set(self.path, "chain", str(rec.seq), rec.as_dict())
            if seq is None:
                raise RuntimeError(f"не удалось записать якорь в {self.path}")
            return rec

    # --- проверка
    def verify(self) -> list[str]:
        issues: list[str] = []
        if not self.path.exists():
            return ["anchor: журнал якорей отсутствует"]
        recs = self.records()
        if not recs:
            return []  # пустой журнал — не порча: якорей ещё не ставили
        prev = ""
        for rec in recs:
            if rec.self != rec.digest():
                issues.append(f"anchor {rec.seq}: self-hash не совпадает (запись правили)")
            if rec.prev != prev:
                issues.append(f"anchor {rec.seq}: разрыв prev")
            prev = rec.self
        tool_ok, lines = emlbox.verify(self.path)
        if not tool_ok:
            issues.append("anchor: контейнер журнала не проходит emlbox verify")
        return issues

    def export_head(self) -> dict:
        """Голова цепочки для офсайт-копии (коммит/почта/бумага)."""
        head = self.head()
        if head is None:
            return {"chain": "empty"}
        return {"chain_seq": head.seq, "self": head.self, "file": head.file,
                "sha256": head.sha256, "tail_seq": head.tail_seq,
                "tail_hash": head.tail_hash, "ts": head.ts}
