"""retention.py — ретенция историка и спула шины.

Историк — это файлы: без ретенции он превращается в миллионы inode.
Политика: держим записи не старше `keep_hours` и не больше `max_records`
(самые старые удаляются первыми). Возраст берём по mtime: запись tagdb
write-once, её mtime = время вставки.

Спул шины: `.done` и осиротевшие `.tmp` чистятся по TTL (emlbox.bus_gc).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import emlbox


@dataclass
class PruneResult:
    files: int = 0
    bytes: int = 0
    errors: int = 0

    def add(self, other: "PruneResult") -> "PruneResult":
        self.files += other.files
        self.bytes += other.bytes
        self.errors += other.errors
        return self

    def as_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes, "errors": self.errors}


def prune_historian(hist: Path, keep_hours: float, max_records: int,
                    now: float | None = None) -> PruneResult:
    """Удаляет старые записи историка. Один проход scandir + stat."""
    hist = Path(hist)
    res = PruneResult()
    if not hist.is_dir():
        return res
    now = time.time() if now is None else now
    cutoff = now - keep_hours * 3600.0

    entries: list[tuple[float, int, Path]] = []
    try:
        with os.scandir(hist) as it:
            for e in it:
                if not e.name.endswith(".eml"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, Path(e.path)))
    except OSError:
        res.errors += 1
        return res

    drop: list[tuple[float, int, Path]] = [x for x in entries if x[0] < cutoff]
    keep = [x for x in entries if x[0] >= cutoff]
    if len(keep) > max_records:
        keep.sort(key=lambda x: x[0])
        extra = len(keep) - max_records
        drop.extend(keep[:extra])
        keep = keep[extra:]

    for _, size, path in drop:
        try:
            path.unlink()
            res.files += 1
            res.bytes += size
        except OSError:
            res.errors += 1
    return res


def prune_bus(bus: Path, done_ttl_s: float, tmp_ttl_s: float,
              now: float | None = None) -> PruneResult:
    removed = emlbox.bus_gc(Path(bus), done_ttl_s=done_ttl_s, tmp_ttl_s=tmp_ttl_s, now=now)
    return PruneResult(files=removed["done"] + removed["tmp"], bytes=removed["bytes"])


def prune_all(store: Path, cfg, now: float | None = None) -> dict:
    """Один проход ретенции по всем каталогам стора."""
    hist = prune_historian(store / "hist",
                           keep_hours=float(cfg.get("historian.keep_hours")),
                           max_records=int(cfg.get("historian.max_records")),
                           now=now)
    bus = prune_bus(store / "bus",
                    done_ttl_s=float(cfg.get("bus.done_ttl_s")),
                    tmp_ttl_s=float(cfg.get("bus.tmp_ttl_s")),
                    now=now)
    return {"historian": hist.as_dict(), "bus": bus.as_dict()}
