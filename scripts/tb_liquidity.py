#!/usr/bin/env python3
"""
tb_liquidity.py

ToteBot derived liquidity lens.

Read-only. No Betfair credentials. No API calls. No betting.

Purpose:
- Scan existing Betfair market_book snapshots in ~/botlab/totebot/history.
- Derive compact market/runner liquidity facts from captured exchange depth.
- Help ToteBot distinguish real moves from thin-book noise.

Inputs per race folder:
  capture_manifest.json
  closed.json/result.json          optional, used for winner context
  market_book_t15.json
  market_book_t10.json
  market_book_t5.json
  market_book_t2.json
  market_book_t30.json

Output per race folder:
  liquidity_profile.json

Default base:
  ~/botlab/totebot
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SLOTS = ("t15", "t10", "t5", "t2", "t30")
SNAPSHOT_FILES = {slot: f"market_book_{slot}.json" for slot in SLOTS}
DEFAULT_BASE = Path.home() / "botlab" / "totebot"
OUTPUT_NAME = "liquidity_profile.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def clean_runner_name(name: Any) -> str:
    """Remove common leading saddlecloth text: '1. Horse' -> 'Horse'."""
    text = str(name or "").strip()
    return re.sub(r"^\s*\d+\s*[\.)-]\s*", "", text).strip()


def as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def ladder_total(levels: Any, depth: int = 3) -> float | None:
    """Sum size from first N price ladder levels."""
    if not isinstance(levels, list):
        return None

    total = 0.0
    seen = 0
    for level in levels[:depth]:
        if not isinstance(level, dict):
            continue
        size = as_float(level.get("size"))
        if size is None:
            continue
        total += size
        seen += 1

    return round(total, 2) if seen else None


def first_price(levels: Any) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    return as_float(level.get("price")) if isinstance(level, dict) else None


def first_size(levels: Any) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    size = as_float(level.get("size")) if isinstance(level, dict) else None
    return round(size, 2) if size is not None else None


def runner_key(runner: dict[str, Any]) -> str:
    sid = runner.get("selection_id")
    if sid is not None:
        return f"sid:{sid}"
    cloth = runner.get("cloth_number")
    name = clean_runner_name(runner.get("runner_name"))
    return f"name:{cloth}:{name.lower()}"


def runner_snapshot_liquidity(runner: dict[str, Any]) -> dict[str, Any]:
    backs = runner.get("back_levels") or runner.get("available_to_back") or []
    lays = runner.get("lay_levels") or runner.get("available_to_lay") or []

    best_back = first_price(backs)
    best_lay = first_price(lays)
    spread = None
    if best_back is not None and best_lay is not None:
        spread = round(best_lay - best_back, 4)

    top3_back = ladder_total(backs, 3)
    top3_lay = ladder_total(lays, 3)
    top3_total = None
    if top3_back is not None or top3_lay is not None:
        top3_total = round((top3_back or 0.0) + (top3_lay or 0.0), 2)

    return {
        "last_price_traded": as_float(runner.get("last_price_traded")),
        "runner_total_matched": as_float(runner.get("total_matched")),
        "best_back": best_back,
        "best_back_size": first_size(backs),
        "best_lay": best_lay,
        "best_lay_size": first_size(lays),
        "spread": spread,
        "top3_back_size": top3_back,
        "top3_lay_size": top3_lay,
        "top3_book_size": top3_total,
    }


def liquidity_class(market_total: float | None, runner_top3: float | None) -> str:
    """
    Conservative first-pass liquidity labels.

    These thresholds are deliberately rough; tune from review evidence.
    """
    mt = market_total or 0.0
    rt = runner_top3 or 0.0

    if mt >= 50000 and rt >= 2000:
        return "strong"
    if mt >= 10000 and rt >= 500:
        return "okay"
    if mt >= 2500 and rt >= 150:
        return "thin-but-usable"
    return "thin"


def move_pct(first: float | None, last: float | None) -> float | None:
    if first is None or last is None or first == 0:
        return None
    return round(((last - first) / first) * 100.0, 2)


def load_snapshots(folder: Path) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for slot, filename in SNAPSHOT_FILES.items():
        data = read_json(folder / filename)
        if data:
            snapshots[slot] = data
    return snapshots


def market_total_matched(snapshot: dict[str, Any]) -> float | None:
    for key in ("total_matched", "market_total_matched"):
        value = as_float(snapshot.get(key))
        if value is not None:
            return value

    market = snapshot.get("market")
    if isinstance(market, dict):
        return as_float(market.get("total_matched"))

    return None


def snapshot_runners(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    runners = snapshot.get("runners")
    return runners if isinstance(runners, list) else []


def derive_folder(folder: Path) -> dict[str, Any] | None:
    snapshots = load_snapshots(folder)
    if not snapshots:
        return None

    manifest = read_json(folder / "capture_manifest.json") or {}
    result = read_json(folder / "result.json") or {}
    closed = read_json(folder / "closed.json") or {}

    market_id = (
        manifest.get("market_id")
        or result.get("market_id")
        or closed.get("market_id")
        or folder.name
    )

    per_slot_market: dict[str, Any] = {}
    runners_by_key: dict[str, dict[str, Any]] = {}

    for slot, snapshot in snapshots.items():
        mt = market_total_matched(snapshot)
        per_slot_market[slot] = {
            "market_total_matched": mt,
            "captured_at": snapshot.get("captured_at"),
            "delayed": snapshot.get("delayed"),
        }

        for runner in snapshot_runners(snapshot):
            if not isinstance(runner, dict):
                continue
            key = runner_key(runner)
            entry = runners_by_key.setdefault(
                key,
                {
                    "key": key,
                    "selection_id": runner.get("selection_id"),
                    "cloth_number": runner.get("cloth_number"),
                    "runner_name": runner.get("runner_name"),
                    "clean_name": clean_runner_name(runner.get("runner_name")),
                    "slots": {},
                },
            )
            entry["slots"][slot] = runner_snapshot_liquidity(runner)

    runners: list[dict[str, Any]] = []
    for entry in runners_by_key.values():
        slots = entry.get("slots", {})
        t15 = slots.get("t15", {}) if isinstance(slots, dict) else {}
        t30 = slots.get("t30", {}) if isinstance(slots, dict) else {}

        first_ltp = as_float(t15.get("last_price_traded"))
        last_ltp = as_float(t30.get("last_price_traded"))
        last_top3 = as_float(t30.get("top3_book_size"))
        last_market_total = per_slot_market.get("t30", {}).get("market_total_matched")

        entry["t15_to_t30_move_pct"] = move_pct(first_ltp, last_ltp)
        entry["liquidity_class_t30"] = liquidity_class(as_float(last_market_total), last_top3)
        runners.append(entry)

    runners.sort(
        key=lambda item: (
            item.get("liquidity_class_t30") == "thin",
            -(as_float(item.get("slots", {}).get("t30", {}).get("top3_book_size")) or 0.0),
            str(item.get("clean_name") or ""),
        )
    )

    return {
        "schema": "totebot_liquidity_profile/v1",
        "generated_at": utc_now_iso(),
        "source": "derived_from_market_book_snapshots",
        "mode": "read-only",
        "market_id": str(market_id),
        "folder": str(folder),
        "event_name": manifest.get("event_name") or result.get("event_name"),
        "market_name": manifest.get("market_name") or result.get("market_name"),
        "track": manifest.get("track") or result.get("track"),
        "complete_closed": bool(closed.get("complete")),
        "winner": result.get("winner") or result.get("winner_summary"),
        "market": per_slot_market,
        "runners": runners,
    }


def iter_market_folders(base: Path) -> list[Path]:
    history = base / "history"
    if not history.exists():
        return []

    folders: list[Path] = []
    for date_dir in sorted(history.iterdir()):
        if not date_dir.is_dir():
            continue
        for market_dir in sorted(date_dir.iterdir()):
            if market_dir.is_dir():
                folders.append(market_dir)
    return folders


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ToteBot derived liquidity profiles.")
    parser.add_argument("command", choices=("scan", "latest", "folder"), help="What to process")
    parser.add_argument("path", nargs="?", help="Folder path for 'folder' command")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE, help=f"ToteBot base dir (default: {DEFAULT_BASE})")
    parser.add_argument("--write", action="store_true", help="Write liquidity_profile.json files")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of folders for scan")
    args = parser.parse_args()

    base = args.base.expanduser()

    if args.command == "folder":
        if not args.path:
            raise SystemExit("folder command requires a path")
        folders = [Path(args.path).expanduser()]
    else:
        folders = iter_market_folders(base)
        if args.command == "latest":
            folders = folders[-1:] if folders else []
        elif args.limit > 0:
            folders = folders[-args.limit:]

    built = 0
    skipped = 0

    for folder in folders:
        profile = derive_folder(folder)
        if not profile:
            skipped += 1
            continue

        built += 1
        if args.write:
            write_json(folder / OUTPUT_NAME, profile)
        else:
            print(json.dumps(profile, indent=2, sort_keys=True))
            if args.command in ("latest", "folder"):
                break

    if args.command == "scan":
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": args.command,
                    "base": str(base),
                    "folders_seen": len(folders),
                    "profiles_built": built,
                    "skipped": skipped,
                    "wrote_files": bool(args.write),
                },
                indent=2,
                sort_keys=True,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
