"""WP-C2: recompute benchmark records from the raw event streams.

The committed event streams under a benchmark home dir are the original
audit evidence — this script never modifies them. It re-derives each run's
verification verdict (WP-C1 semantics) and token accounting (WP-C2: only
provider-reported usage counts as actual) from the raw events through the
SAME code path the live runner uses (`benchmark_matrix.root_record_fields`
and `_session_metrics`), then:

* writes recomputed per-run records + records.json + summary.json + a
  machine-generated old-vs-new table under ``--out``
* with ``--apply``, also refreshes the derived per-run JSON / records /
  summary files next to the originals (``--records``).

Run mapping is frozen in a manifest (``--out/manifest.json``): supervised
runs map to their ledger root id, single runs map to their session dirs
(single creates no ledger task). The manifest is written once from
timestamp matching and then stays fixed so reruns are deterministic.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# metric fields this script owns (everything else in a record is preserved)
RECOMPUTED_FIELDS = (
    "tokens_in_actual", "tokens_out_actual", "tokens_total_actual",
    "tokens_estimated_context", "tokens_usage_missing_calls", "usage_complete",
    "model_calls", "model_attempts", "tool_calls",
    "verification_status", "verification_passed", "verification_evidence",
    "changed_files", "human_interventions", "replans",
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)


def _session_start_utc(home: Path, sessions_dir: Path, session_id: str) -> datetime | None:
    """UTC start time of a session, from its first event's ts (the session
    directory NAME uses local wall time, which is why matching goes through
    the event log instead)."""
    from jinlin_agent.events import read_events

    sdir = sessions_dir / session_id
    if not (sdir / "events.jsonl").exists():
        return None
    for event in read_events(sdir):
        ts = event.get("ts") or ""
        if ts:
            return _parse_ts(ts)
    return None


def build_manifest(home: Path, records_dir: Path, tasks_file: Path) -> dict[str, Any]:
    """Derive the run->evidence mapping once, from timestamps."""
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger

    cfg = load_config(home=home)
    ledger = TaskLedger(cfg).open()
    try:
        roots = [r for r in ledger.list_tasks() if not r["parent_task_id"]]
        sessions_dir = Path(cfg.sessions_dir)
        session_ids = sorted(p.name for p in sessions_dir.iterdir() if p.is_dir())
        session_starts = {s: _session_start_utc(home, sessions_dir, s) for s in session_ids}
    finally:
        ledger.close()

    manifest: dict[str, Any] = {}
    for record_file in sorted(records_dir.glob("*_*.json")):
        if record_file.name in ("records.json", "summary.json"):
            continue
        record = json.loads(record_file.read_text(encoding="utf-8"))
        key = f"{record['task_id']}_{record['strategy']}"
        started = _parse_ts(record["started_at"])
        if record.get("ledger_task"):
            candidates = [
                r["task_id"] for r in roots
                if abs((_parse_ts(r["created_at"]) - started).total_seconds()) <= 120
            ]
            if len(candidates) != 1:
                raise SystemExit(
                    f"manifest: {key}: expected exactly one ledger root near {record['started_at']}, got {candidates}"
                )
            manifest[key] = {"kind": "root", "root": candidates[0]}
        else:
            matches = [
                s for s, start in session_starts.items()
                if start is not None and abs((start - started).total_seconds()) <= 120
            ]
            if len(matches) != 1:
                raise SystemExit(
                    f"manifest: {key}: cannot match a single session near {record['started_at']}: {matches}"
                )
            manifest[key] = {"kind": "sessions", "sessions": [matches[0]]}
    return manifest


def _sessions_for_key(home: Path, key: str, manifest: dict[str, Any]) -> list[str]:
    entry = manifest[key]
    if entry["kind"] == "sessions":
        return entry["sessions"]
    return []


def recompute_key(
    key: str,
    original: dict[str, Any],
    home: Path,
    workspace: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.state import recover as fold_events
    from jinlin_agent.verify import detect_project

    import benchmark_matrix as bm

    cfg = load_config(home=home)
    has_test_command = bool(detect_project(workspace, cfg).test_commands)
    entry = manifest[key]
    recomputed: dict[str, Any] = dict(original)
    if entry["kind"] == "sessions":
        from jinlin_agent.events import read_events

        metrics_list = []
        human = 0
        for sid in entry["sessions"]:
            sdir = Path(cfg.sessions_dir) / sid
            events = list(read_events(sdir))
            st = fold_events(events)
            metrics_list.append(bm._session_metrics(st, profile="code", has_test_command=has_test_command))
            human += bm._human_interventions([dict(e) for e in events])
        agg = {
            "tokens_estimated_context": 0, "tokens_usage_missing_calls": 0,
            "model_calls": 0, "model_attempts": 0, "tool_calls": 0,
        }
        actual_in: int | None = None
        actual_out: int | None = None
        changed: set[str] = set()
        for m in metrics_list:
            for f in ("tokens_estimated_context", "tokens_usage_missing_calls", "model_calls", "model_attempts", "tool_calls"):
                agg[f] += m[f] or 0
            if m["tokens_in_actual"] is not None:
                actual_in = (actual_in or 0) + m["tokens_in_actual"]
            if m["tokens_out_actual"] is not None:
                actual_out = (actual_out or 0) + m["tokens_out_actual"]
            changed.update(m["changed_files"])
        usage_observed = actual_in is not None or actual_out is not None
        changed_files = sorted(changed)
        root = bm._aggregate_verification(metrics_list) if metrics_list else {
            "verification_status": "not_run", "verification_passed": False,
            "verification_evidence": {"status": "not_run", "required_levels": [], "executed_levels": [],
                                      "failed_levels": [], "skipped_levels": [],
                                      "notes": ["no session event log matched"], "components": 0},
        }
        recomputed.update(agg)
        recomputed["tokens_in_actual"] = actual_in if usage_observed else None
        recomputed["tokens_out_actual"] = actual_out if usage_observed else None
        recomputed["tokens_total_actual"] = (actual_in + actual_out) if usage_observed else None
        recomputed["usage_complete"] = bool(metrics_list) and all(m["usage_complete"] for m in metrics_list)
        recomputed["usage_missing_sessions"] = len(entry["sessions"]) - len(metrics_list)
        recomputed["usage_expected_sessions"] = len(entry["sessions"])
        recomputed["changed_files"] = changed_files
        recomputed.update(root)
        recomputed["human_interventions"] = human
        recomputed["replans"] = 0  # single creates no ledger tasks: no replans possible
    else:
        from jinlin_agent.events import read_events

        cfg2 = load_config(home=home)
        ledger = TaskLedger(cfg2).open()
        try:
            root_id = entry["root"]
            root_row = ledger.require_task(root_id)
            log_dir = Path(cfg2.tasks_dir) / root_id
            events = list(read_events(log_dir)) if (log_dir / "events.jsonl").exists() else []
            fields = bm.root_record_fields(ledger, cfg2, root_id, workspace, events)
            recomputed.update(fields)
            recomputed["status"] = root_row["status"]
            result = root_row.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result or "{}")
                except json.JSONDecodeError:
                    result = {}
            recomputed["error_summary"] = (root_row.get("error") or result.get("summary") or "")
        finally:
            ledger.close()
    return {k: recomputed.get(k) for k in (original.keys() | set(RECOMPUTED_FIELDS))}


def _diff_table(original: dict[str, Any], recomputed: dict[str, Any]) -> str:
    lines = ["| field | original | recomputed |", "|---|---|---|"]
    for field in (*RECOMPUTED_FIELDS, "status", "wall_seconds"):
        o, n = original.get(field), recomputed.get(field)
        if o != n:
            lines.append(f"| {field} | {json.dumps(o, ensure_ascii=False)} | {json.dumps(n, ensure_ascii=False)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="WP-C2: recompute benchmark records from event streams")
    parser.add_argument("--home", required=True, help="benchmark home dir (contains ledger.db, sessions/, tasks/)")
    parser.add_argument("--records", required=True, help="dir holding the original per-run JSONs")
    parser.add_argument("--tasks", required=True, help="tasks JSON used for the runs (workspace paths)")
    parser.add_argument("--out", default="docs/architecture/v2/benchmark/recomputed")
    parser.add_argument("--apply", action="store_true", help="also refresh the per-run JSONs/records/summary in --records")
    parser.add_argument("--reuse-manifest", action="store_true", help="reuse the existing manifest instead of re-deriving")
    args = parser.parse_args()

    home = Path(args.home)
    records_dir = Path(args.records)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = {t["id"]: t for t in json.loads(Path(args.tasks).read_text(encoding="utf-8"))}
    manifest_path = out_dir / "manifest.json"
    if args.reuse_manifest and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = build_manifest(home, records_dir, Path(args.tasks))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = ["# WP-C2 recomputation (machine-generated)\n"]
    recomputed_records = []
    for record_file in sorted(records_dir.glob("*_*.json")):
        if record_file.name in ("records.json", "summary.json"):
            continue
        original = json.loads(record_file.read_text(encoding="utf-8"))
        key = f"{original['task_id']}_{original['strategy']}"
        task = tasks[original["task_id"]]
        workspace = Path(str(task.get("workspace") or "")).resolve()
        recomputed = recompute_key(key, original, home, workspace, manifest)
        (out_dir / f"{record_file.stem}.json").write_text(
            json.dumps(recomputed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_lines.append(f"## {key}\n\n" + _diff_table(original, recomputed) + "\n")
        recomputed_records.append(recomputed)
        if args.apply:
            record_file.write_text(json.dumps(recomputed, ensure_ascii=False, indent=2), encoding="utf-8")

    import benchmark_matrix as bm

    (out_dir / "records.json").write_text(
        json.dumps(recomputed_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(bm.aggregate(recomputed_records), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.apply:
        (records_dir / "records.json").write_text(
            json.dumps(recomputed_records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (records_dir / "summary.json").write_text(
            json.dumps(bm.aggregate(recomputed_records), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (out_dir / "CHANGES.md").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"recomputed {len(recomputed_records)} records -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
