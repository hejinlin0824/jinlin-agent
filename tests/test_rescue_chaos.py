"""Seeded end-to-end chaos sequences for Rescue lifecycle invariants.

The sequence is deterministic per seed so a CI failure can be reproduced with
the exact seed printed by pytest. These are process-level/API interleavings,
not claims of physical power-loss simulation.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import jinlin_agent.trash as trash


@pytest.mark.parametrize("seed", [7, 19, 41, 73, 101, 211])
def test_seeded_rescue_lifecycle_sequences_converge_without_losing_data(tmp_path, seed):
    rng = random.Random(seed)
    entries = []
    for index in range(4):
        payload = f"seed={seed};item={index};payload\n".encode("ascii")
        rescue = tmp_path / f".item-{index}.py.jinlin-rescue-{seed:04x}{index:04x}"
        rescue.write_bytes(payload)
        assert trash.record_trash(tmp_path, rescue, f"item-{index}.py", "rescue")
        entries.append((rescue, tmp_path / f"item-{index}.py", payload))

    order = list(entries)
    rng.shuffle(order)
    for rescue, target, payload in order:
        # Vary the number of harmless inspection/recovery attempts before
        # publication. Recovery before a restore must not invent a transaction.
        for _ in range(rng.randrange(3)):
            inspected = trash.inspect_rescue(tmp_path)
            assert inspected["status"] in ("ok", "attention-required")
            trash.recover_rescue(tmp_path)

        restored = trash.restore_rescue(tmp_path, rescue.name)
        assert restored["status"] in ("restored", "already-published"), (
            f"seed={seed}, rescue={rescue.name}, result={restored}"
        )
        assert target.read_bytes() == payload
        assert rescue.read_bytes() == payload

        # Repeated recovery must converge and must never overwrite the target.
        for _ in range(1 + rng.randrange(3)):
            trash.recover_rescue(tmp_path)
            assert target.read_bytes() == payload
            assert rescue.read_bytes() == payload

    # Cleanup is only attempted after every target has been independently
    # verified. It may remove redundant Rescue copies, never restored targets.
    preview = trash.clean_rescue(tmp_path, apply=False)
    assert preview.get("status") in ("dry-run", "ok", "attention-required")
    cleaned = trash.clean_rescue(tmp_path, apply=True)
    assert cleaned.get("status") in ("applied", "attention-required")
    for _rescue, target, payload in entries:
        assert target.read_bytes() == payload, f"seed={seed}, target={target.name}"

    # A second cleanup/recovery pass is idempotent. The audit projector must
    # remain internally consistent after the randomized operation order.
    trash.recover_rescue(tmp_path)
    trash.clean_rescue(tmp_path, apply=True)
    report = trash.inspect_rescue(tmp_path)
    assert report["counts"]["invalid_transactions"] == 0, f"seed={seed}: {report}"
    assert report["audit"]["issues"] == [], f"seed={seed}: {report['audit']}"


def test_audit_hash_tampering_is_detected_and_append_fails_closed(tmp_path):
    audit_path = tmp_path / trash.AUDIT_REL
    ok, warning = trash._append_audit_event(
        tmp_path,
        "restore",
        "restored",
        txid="a" * 32,
        rescue=".tamper.py.jinlin-rescue-aabbccdd",
        target="tamper.py",
        sha256="b" * 64,
    )
    assert ok and warning is None

    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    event["status"] = "conflict"
    audit_path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    tampered_bytes = audit_path.read_bytes()

    inspected = trash._read_audit(tmp_path)
    assert inspected["issues"], "a valid JSON row with a changed payload must fail its hash"
    ok, warning = trash._append_audit_event(
        tmp_path, "recover", "already-published", txid="a" * 32,
    )
    assert not ok
    assert warning and "integrity" in warning.lower()
    assert audit_path.read_bytes() == tampered_bytes, "fail-closed append must preserve tampered evidence"


def test_audit_truncated_tail_is_preserved_and_blocks_future_append(tmp_path):
    audit_path = tmp_path / trash.AUDIT_REL
    ok, warning = trash._append_audit_event(
        tmp_path,
        "restore",
        "restored",
        txid="c" * 32,
        rescue=".truncate.py.jinlin-rescue-aabbccde",
        target="truncate.py",
        sha256="d" * 64,
    )
    assert ok and warning is None
    original = audit_path.read_bytes()
    assert original.endswith(b"\n")
    audit_path.write_bytes(original[:-5])
    truncated = audit_path.read_bytes()

    inspected = trash._read_audit(tmp_path)
    assert inspected["issues"]
    ok, warning = trash._append_audit_event(
        tmp_path, "recover", "already-published", txid="c" * 32,
    )
    assert not ok
    assert warning and "integrity" in warning.lower()
    assert audit_path.read_bytes() == truncated
