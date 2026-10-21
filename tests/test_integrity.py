"""Целостность: deep verify, якоря, два типа подмены."""
from __future__ import annotations

import shutil

import pytest

from scada import emlbox
from scada.demo import tamper_delta_body, tamper_last_delta_consistently
from scada.integrity import AnchorLog, IntegrityError, deep_verify, parse_container, sha256_file

pytestmark = pytest.mark.integration


@pytest.fixture()
def container(tmp_path, emlbox_bin):
    path = tmp_path / "station.eml"
    assert emlbox.create(path, "station@system.local", "test station")
    for i in range(6):
        assert emlbox.kv_set(path, "state", f"p{i % 3}", {"value": i, "quality": "good"}) is not None
    return path


def test_honest_container_passes_every_level(container):
    c = parse_container(container)
    assert c.tail_seq == 6
    assert c.entity == "station@system.local"
    assert c.base_hash and c.tail_hash
    rep = deep_verify(container)
    assert rep.ok, rep.issues
    tool_ok, _ = emlbox.verify(container)
    assert tool_ok


def test_plain_body_tamper_is_caught(container, tmp_path):
    bad = tmp_path / "bad.eml"
    tamper_delta_body(container, bad, which="last")
    tool_ok, _ = emlbox.verify(bad)
    assert not tool_ok
    assert not deep_verify(bad).ok


def test_tail_rewrite_evades_tool_but_not_anchor(container, tmp_path):
    anchors = AnchorLog(tmp_path / "anchor.eml", store=tmp_path)
    anchor = anchors.append(container)

    bad = tmp_path / "tailcut.eml"
    tamper_last_delta_consistently(container, bad)

    tool_ok, _ = emlbox.verify(bad)
    assert tool_ok, "ожидание: emlbox verify не хэширует tail index/трейлер"

    assert deep_verify(bad).ok, "без якоря структурных отличий нет"
    rep = deep_verify(bad, anchor=anchor)
    assert not rep.ok
    assert any("anchor" in i for i in rep.anchor), rep.issues


def test_truncation_is_rejected(container, tmp_path):
    bad = tmp_path / "truncated.eml"
    shutil.copy2(container, bad)
    data = bad.read_bytes()
    bad.write_bytes(data[:-100])
    with pytest.raises(IntegrityError):
        parse_container(bad)
    assert not deep_verify(bad).ok


def test_anchor_growth_is_allowed_but_rewrite_is_not(container, tmp_path):
    anchors = AnchorLog(tmp_path / "anchor.eml", store=tmp_path)
    anchor = anchors.append(container)
    before = parse_container(container)
    assert anchor.check(before, sha256_file(container)) == []

    for i in range(2):
        emlbox.kv_set(container, "state", "p0", {"value": 100 + i})
    grown = parse_container(container)
    assert grown.tail_seq == anchor.tail_seq + 2
    assert anchor.check(grown, sha256_file(container)) == []

    # подменяем именно ЗАЯКОРЕННУЮ дельту (seq == anchor.tail_seq) с пересчётом
    # её хэша в tail index: emlbox verify и структурные инварианты молчат,
    # но якорь видит, что зафиксированная история изменилась
    tamper_last_delta_consistently(container, tmp_path / "tmp.eml",
                                   delta_index=anchor.tail_seq - 1)
    rewritten = parse_container(tmp_path / "tmp.eml")
    issues = anchor.check(rewritten, sha256_file(tmp_path / "tmp.eml"))
    assert any("заякоренной дельты" in i for i in issues), issues


def test_anchor_chain_detects_edited_record(container, tmp_path):
    anchors = AnchorLog(tmp_path / "anchor.eml", store=tmp_path)
    a1 = anchors.append(container)
    a2 = anchors.append(container)
    assert a2.prev == a1.self
    assert anchors.verify() == []
    assert anchors.head().seq == 2

    # правим запись в append-only журнале через новый delta того же ключа
    emlbox.kv_set(anchors.path, "chain", "1", {**a1.as_dict(), "sha256": "0" * 64})
    issues = anchors.verify()
    assert any("self-hash" in i for i in issues), issues


def test_anchor_self_hash_survives_container_roundtrip(container, tmp_path):
    """`ts` в журнале — целые мс: float JSON не переживает round-trip без потери бита,
    и self-hash ложно «расходился» после каждой перезаписи контейнера."""
    anchors = AnchorLog(tmp_path / "anchor.eml", store=tmp_path)
    for _ in range(5):
        anchors.append(container)
    assert anchors.verify() == []
    records = anchors.records()
    assert [r.seq for r in records] == [1, 2, 3, 4, 5]
    assert all(isinstance(r.payload()["ts"], int) for r in records)


def test_anchor_rel_and_export(container, tmp_path):
    anchors = AnchorLog(tmp_path / "anchor.eml", store=tmp_path)
    rec = anchors.append(container)
    assert rec.file == "station.eml"
    head = anchors.export_head()
    assert head["chain_seq"] == 1 and len(head["self"]) == 64
    assert anchors.latest_for("station.eml").sha256 == sha256_file(container)
