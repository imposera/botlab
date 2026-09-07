#!/usr/bin/env python3
"""
ToteBot Betfair history integrity audit.

Read-only checker for:
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/

It checks:
- folder/date/market_id shape
- capture_manifest.json presence and schema
- snapshot files declared in manifest exist
- snapshot market_id consistency
- snapshot slot consistency
- runner selection_id consistency across snapshots/result
- result.json / closed.json consistency
- capture completeness by slot
- duplicate market ids
- basic summary counts

It does not call Betfair and does not modify files.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_SLOTS = ["T-15", "T-10", "T-5", "T-2", "T-30"]
SLOT_FILE = {
    "T-15": "market_book_t15.json",
    "T-10": "market_book_t10.json",
    "T-5": "market_book_t5.json",
    "T-2": "market_book_t2.json",
    "T-30": "market_book_t30.json",
}
MARKET_ID_RE = re.compile(r"^1\.\d+$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class RaceAudit:
    path: Path
    market_id: str
    day: str
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    slots_captured: list[str] = field(default_factory=list)
    slots_missed: list[str] = field(default_factory=list)
    has_manifest: bool = False
    has_result: bool = False
    has_closed: bool = False
    winner: str | None = None
    winner_no: str | None = None
    market_name: str | None = None
    track: str | None = None

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        return {"__json_error__": str(exc)}
    except OSError as exc:
        return {"__io_error__": str(exc)}


def iso_to_dt(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def runner_ids(snapshot: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for runner in snapshot.get("runners", []):
        sid = runner.get("selection_id")
        if isinstance(sid, int):
            ids.add(sid)
    return ids


def audit_race_dir(race_dir: Path) -> RaceAudit:
    day = race_dir.parent.name
    market_id = race_dir.name
    audit = RaceAudit(path=race_dir, market_id=market_id, day=day)

    if not DAY_RE.match(day):
        audit.error(f"parent folder is not YYYY-MM-DD: {day}")

    if not MARKET_ID_RE.match(market_id):
        audit.error(f"folder name does not look like a Betfair market id: {market_id}")

    manifest_path = race_dir / "capture_manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None:
        audit.error("missing capture_manifest.json")
        manifest = {}
    elif "__json_error__" in manifest:
        audit.error(f"capture_manifest.json invalid JSON: {manifest['__json_error__']}")
        manifest = {}
    elif "__io_error__" in manifest:
        audit.error(f"capture_manifest.json read error: {manifest['__io_error__']}")
        manifest = {}
    else:
        audit.has_manifest = True
        if manifest.get("market_id") != market_id:
            audit.error(f"manifest market_id mismatch: {manifest.get('market_id')} != {market_id}")
        m = manifest.get("market") or {}
        audit.track = m.get("track")
        audit.market_name = m.get("market_name")

    manifest_slots = manifest.get("slots") if isinstance(manifest.get("slots"), dict) else {}

    snapshot_runner_sets: dict[str, set[int]] = {}
    snapshot_times: dict[str, datetime] = {}

    for slot in EXPECTED_SLOTS:
        rec = manifest_slots.get(slot) if isinstance(manifest_slots, dict) else None
        slot_file = race_dir / SLOT_FILE[slot]

        if isinstance(rec, dict) and rec.get("status") == "captured":
            audit.slots_captured.append(slot)
            declared = rec.get("snapshot_file")
            if declared and Path(str(declared)).name != slot_file.name:
                audit.warn(f"{slot} manifest snapshot_file name mismatch: {declared}")

            if not slot_file.exists():
                audit.error(f"{slot} captured in manifest but missing file {slot_file.name}")
                continue

            snap = read_json(slot_file)
            if snap is None:
                audit.error(f"{slot_file.name} missing")
                continue
            if "__json_error__" in snap:
                audit.error(f"{slot_file.name} invalid JSON: {snap['__json_error__']}")
                continue
            if "__io_error__" in snap:
                audit.error(f"{slot_file.name} read error: {snap['__io_error__']}")
                continue

            if snap.get("snapshot_slot") != slot:
                audit.error(f"{slot_file.name} snapshot_slot mismatch: {snap.get('snapshot_slot')} != {slot}")

            snap_market = snap.get("market") if isinstance(snap.get("market"), dict) else {}
            if snap_market.get("market_id") != market_id:
                audit.error(f"{slot_file.name} market_id mismatch: {snap_market.get('market_id')} != {market_id}")

            if not isinstance(snap.get("runners"), list) or not snap["runners"]:
                audit.error(f"{slot_file.name} has no runners")
            else:
                snapshot_runner_sets[slot] = runner_ids(snap)

            dt = iso_to_dt(snap.get("captured_at"))
            if dt:
                snapshot_times[slot] = dt

            seconds = snap_market.get("seconds_to_jump")
            if isinstance(seconds, int):
                if slot == "T-15" and not (600 < seconds <= 900):
                    audit.warn(f"T-15 seconds_to_jump outside expected band: {seconds}")
                elif slot == "T-10" and not (300 < seconds <= 600):
                    audit.warn(f"T-10 seconds_to_jump outside expected band: {seconds}")
                elif slot == "T-5" and not (120 < seconds <= 300):
                    audit.warn(f"T-5 seconds_to_jump outside expected band: {seconds}")
                elif slot == "T-2" and not (30 < seconds <= 120):
                    audit.warn(f"T-2 seconds_to_jump outside expected band: {seconds}")
                elif slot == "T-30" and not (0 < seconds <= 30):
                    audit.warn(f"T-30 seconds_to_jump outside expected band: {seconds}")

        elif isinstance(rec, dict) and rec.get("status") == "missed":
            audit.slots_missed.append(slot)
            if slot_file.exists():
                audit.warn(f"{slot} marked missed but {slot_file.name} exists")
        else:
            if slot_file.exists():
                audit.warn(f"{slot_file.name} exists but manifest has no captured record for {slot}")
                snap = read_json(slot_file)
                if isinstance(snap, dict) and "__json_error__" not in snap and "__io_error__" not in snap:
                    snapshot_runner_sets[slot] = runner_ids(snap)
            else:
                audit.slots_missed.append(slot)

    if snapshot_runner_sets:
        first_slot = list(snapshot_runner_sets)[0]
        first_ids = snapshot_runner_sets[first_slot]
        for slot, ids in snapshot_runner_sets.items():
            if ids != first_ids:
                missing = sorted(first_ids - ids)
                added = sorted(ids - first_ids)
                audit.warn(
                    f"runner selection_id set differs at {slot}; "
                    f"missing={missing[:5]} added={added[:5]}"
                )

    previous_dt = None
    previous_slot = None
    for slot in EXPECTED_SLOTS:
        dt = snapshot_times.get(slot)
        if dt and previous_dt and dt < previous_dt:
            audit.warn(f"capture time out of order: {previous_slot} after {slot}")
        if dt:
            previous_dt = dt
            previous_slot = slot

    result = read_json(race_dir / "result.json")
    if result is not None:
        audit.has_result = True
        if "__json_error__" in result:
            audit.error(f"result.json invalid JSON: {result['__json_error__']}")
            result = {}
        elif "__io_error__" in result:
            audit.error(f"result.json read error: {result['__io_error__']}")
            result = {}
        else:
            if result.get("market_id") != market_id:
                audit.error(f"result market_id mismatch: {result.get('market_id')} != {market_id}")
            if result.get("market_status") != "CLOSED":
                audit.warn(f"result market_status is not CLOSED: {result.get('market_status')}")
            winners = result.get("winners")
            winner = result.get("winner")
            if isinstance(winner, dict):
                audit.winner = winner.get("runner_name")
                audit.winner_no = winner.get("cloth_number")
            if isinstance(winners, list) and len(winners) != 1:
                audit.warn(f"winner count is {len(winners)}")
            if snapshot_runner_sets and isinstance(winner, dict):
                sid = winner.get("selection_id")
                if isinstance(sid, int):
                    union = set().union(*snapshot_runner_sets.values())
                    if sid not in union:
                        audit.warn(f"winner selection_id not present in snapshots: {sid}")

    closed = read_json(race_dir / "closed.json")
    if closed is not None:
        audit.has_closed = True
        if "__json_error__" in closed:
            audit.error(f"closed.json invalid JSON: {closed['__json_error__']}")
            closed = {}
        elif "__io_error__" in closed:
            audit.error(f"closed.json read error: {closed['__io_error__']}")
            closed = {}
        else:
            if closed.get("market_id") != market_id:
                audit.error(f"closed market_id mismatch: {closed.get('market_id')} != {market_id}")
            if closed.get("has_result") is True and not audit.has_result:
                audit.error("closed.json has_result=true but result.json missing")
            if closed.get("complete") is True:
                for slot in EXPECTED_SLOTS:
                    rec = (closed.get("snapshot_slots") or {}).get(slot)
                    if not isinstance(rec, dict) or rec.get("status") != "captured":
                        audit.warn(f"closed complete=true but {slot} not captured in closed snapshot_slots")

    if audit.has_result and not audit.has_closed:
        audit.warn("result.json exists but closed.json missing")
    if audit.has_closed and not audit.has_result:
        audit.error("closed.json exists but result.json missing")

    return audit


def discover_race_dirs(history_dir: Path, days: int | None = None) -> list[Path]:
    if not history_dir.exists():
        return []

    day_dirs = sorted([p for p in history_dir.iterdir() if p.is_dir() and DAY_RE.match(p.name)])
    if days is not None and days > 0:
        day_dirs = day_dirs[-days:]

    race_dirs: list[Path] = []
    for day_dir in day_dirs:
        for market_dir in sorted([p for p in day_dir.iterdir() if p.is_dir()]):
            if MARKET_ID_RE.match(market_dir.name):
                race_dirs.append(market_dir)

    return race_dirs


def print_detail(audit: RaceAudit) -> None:
    symbol = "OK" if audit.ok else "BAD"
    status_bits = [f"slots={len(audit.slots_captured)}/5"]
    if audit.has_result:
        status_bits.append("result")
    if audit.has_closed:
        status_bits.append("closed")
    race = " ".join(x for x in [audit.track, audit.market_name] if x) or "-"
    winner = f" winner={audit.winner_no or '?'} {audit.winner}" if audit.winner else ""
    print(f"{symbol} {audit.day}/{audit.market_id} {race} {' '.join(status_bits)}{winner}")

    for msg in audit.errors:
        print(f"  ERROR: {msg}")
    for msg in audit.warnings:
        print(f"  WARN:  {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit ToteBot Betfair history integrity.")
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=Path.home() / "botlab" / "totebot" / "history",
        help="History root. Default: ~/botlab/totebot/history",
    )
    parser.add_argument("--days", type=int, default=None, help="Only audit the most recent N day folders.")
    parser.add_argument("--market-id", help="Only audit one market id.")
    parser.add_argument("--details", action="store_true", help="Print per-race detail.")
    parser.add_argument("--only-problems", action="store_true", help="With --details, print only races with warnings/errors.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary JSON.")
    args = parser.parse_args()

    history_dir = args.history_dir.expanduser()
    race_dirs = discover_race_dirs(history_dir, args.days)

    if args.market_id:
        race_dirs = [p for p in race_dirs if p.name == args.market_id]

    audits = [audit_race_dir(p) for p in race_dirs]

    market_counts = Counter(a.market_id for a in audits)
    duplicate_market_ids = sorted([mid for mid, count in market_counts.items() if count > 1])

    total = len(audits)
    clean = sum(1 for a in audits if a.ok and not a.warnings)
    warnings_only = sum(1 for a in audits if a.warnings and not a.errors)
    errors = sum(1 for a in audits if a.errors)
    complete_closed = sum(
        1 for a in audits
        if a.has_closed and a.has_result and len(a.slots_captured) == len(EXPECTED_SLOTS) and not a.errors
    )
    with_result = sum(1 for a in audits if a.has_result)
    with_closed = sum(1 for a in audits if a.has_closed)
    slot_counts = Counter()
    for a in audits:
        for slot in a.slots_captured:
            slot_counts[slot] += 1

    if duplicate_market_ids:
        errors += len(duplicate_market_ids)

    summary = {
        "schema": "totebot_history_audit/v1",
        "history_dir": str(history_dir),
        "audited_races": total,
        "clean": clean,
        "with_warnings_only": warnings_only,
        "with_errors": errors,
        "complete_closed": complete_closed,
        "with_result": with_result,
        "with_closed": with_closed,
        "slot_capture_counts": {slot: slot_counts[slot] for slot in EXPECTED_SLOTS},
        "duplicate_market_ids": duplicate_market_ids,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "AUDIT "
            f"races={total} "
            f"clean={clean} "
            f"warnings={warnings_only} "
            f"errors={errors} "
            f"complete_closed={complete_closed} "
            f"result={with_result} "
            f"closed={with_closed}"
        )
        print("SLOTS " + " ".join(f"{slot}={slot_counts[slot]}" for slot in EXPECTED_SLOTS))
        if duplicate_market_ids:
            print("DUPLICATE_MARKET_IDS " + " ".join(duplicate_market_ids))

    if args.details:
        for audit in audits:
            has_problem = bool(audit.errors or audit.warnings)
            if args.only_problems and not has_problem:
                continue
            print_detail(audit)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
