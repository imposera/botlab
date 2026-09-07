#!/usr/bin/env python3
"""
ToteBot Open Today capture

Small manual-entry helper for recording the top 5 Open Today / early market
numbers into the current Betfair race history folder.

Runs on core7070 or any node that has the synced ToteBot history/state folder.

Examples:
    python3 tb_open_today.py 7 4.2 5 3.1 1 6.0 8 14 6 20

Short alias idea:
    alias tbtdy='python3 ~/botlab/totebot/scripts/tb_open_today.py'
    tbtdy 7 4.2 5 3.1 1 6.0 8 14 6 20

Interactive:
    python3 tb_open_today.py
    > 7 4.2
    > 5 3.1
    > 1 6.0
    > 8 14
    > 6 20

Writes:
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/manual_open_today.json

Also writes a small convenience copy:
    ~/botlab/totebot/state/manual_open_today_latest.json

Security:
- Read/write only local ToteBot files.
- No Betfair API.
- No credentials.
- No betting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_DIR = Path.home() / "botlab" / "totebot"
STATE_DIR_NAME = "state"
HISTORY_DIR_NAME = "history"

TARGET_FILE_NAME = "betfair_t15_target.json"
ACTIVE_BOOK_FILE_NAME = "active_market_book.json"
OUTPUT_FILE_NAME = "manual_open_today.json"
LATEST_FILE_NAME = "manual_open_today_latest.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)

    os.replace(temporary_path, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def market_from_state(base_dir: Path) -> dict[str, Any] | None:
    state_dir = base_dir / STATE_DIR_NAME

    target = read_json(state_dir / TARGET_FILE_NAME)
    if target and isinstance(target.get("market"), dict):
        return target["market"]

    active_book = read_json(state_dir / ACTIVE_BOOK_FILE_NAME)
    if active_book and isinstance(active_book.get("market"), dict):
        return active_book["market"]

    return None


def find_history_dir(
    base_dir: Path,
    market_id: str,
    market_start_time: str | None = None,
) -> Path | None:
    history_dir = base_dir / HISTORY_DIR_NAME

    if market_start_time:
        start = parse_time(market_start_time)
        if start:
            candidate = history_dir / start.strftime("%Y-%m-%d") / market_id
            if candidate.exists():
                return candidate

            # It might not exist yet if manual entry arrives before first snapshot.
            # In that case, it is safe to create the expected folder.
            return candidate

    matches = sorted(
        [
            path
            for path in history_dir.glob("*/*")
            if path.is_dir() and path.name == market_id
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    return matches[0] if matches else None


def parse_pairs(items: list[str]) -> list[dict[str, Any]]:
    if len(items) % 2 != 0:
        raise ValueError("Enter pairs: runner_number odds runner_number odds ...")

    rows: list[dict[str, Any]] = []

    for index in range(0, len(items), 2):
        runner_no = items[index].strip()
        odds_text = items[index + 1].strip()

        if not runner_no:
            raise ValueError("Runner number cannot be blank")

        try:
            odds = float(odds_text)
        except ValueError as exc:
            raise ValueError(f"Invalid odds for runner {runner_no}: {odds_text}") from exc

        if odds <= 0:
            raise ValueError(f"Odds must be positive for runner {runner_no}: {odds_text}")

        rows.append(
            {
                "rank": len(rows) + 1,
                "cloth_number": runner_no,
                "odds": odds,
            }
        )

    return rows


def interactive_pairs(limit: int) -> list[dict[str, Any]]:
    print(f"Enter Open Today top {limit} as: runner odds")
    print("Blank line to finish early.")
    rows: list[dict[str, Any]] = []

    while len(rows) < limit:
        try:
            line = input(f"{len(rows) + 1}> ").strip()
        except EOFError:
            break

        if not line:
            break

        parts = line.split()
        if len(parts) != 2:
            print("Use: runner odds   example: 7 4.2")
            continue

        try:
            row = parse_pairs(parts)[0]
        except ValueError as exc:
            print(f"Error: {exc}")
            continue

        row["rank"] = len(rows) + 1
        rows.append(row)

    return rows


def runner_map_from_active_book(base_dir: Path) -> dict[str, dict[str, Any]]:
    active_book = read_json(base_dir / STATE_DIR_NAME / ACTIVE_BOOK_FILE_NAME)
    mapping: dict[str, dict[str, Any]] = {}

    if not active_book or not isinstance(active_book.get("runners"), list):
        return mapping

    for runner in active_book["runners"]:
        if not isinstance(runner, dict):
            continue

        cloth = runner.get("cloth_number")
        if cloth is None:
            continue

        mapping[str(cloth)] = runner

    return mapping


def enrich_rows(
    rows: list[dict[str, Any]],
    active_runner_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []

    for row in rows:
        cloth = str(row["cloth_number"])
        active = active_runner_map.get(cloth, {})

        item = dict(row)
        if active:
            item["selection_id"] = active.get("selection_id")
            item["runner_name"] = active.get("runner_name")
            item["sort_priority"] = active.get("sort_priority")

        enriched.append(item)

    return enriched


def build_payload(
    market: dict[str, Any],
    rows: list[dict[str, Any]],
    source: str,
    note: str | None,
    operator: str | None,
) -> dict[str, Any]:
    market_id = market.get("market_id")
    prices_by_cloth = {
        str(item["cloth_number"]): item["odds"]
        for item in rows
    }

    payload: dict[str, Any] = {
        "schema": "totebot_manual_open_today/v1",
        "source": source,
        "mode": "manual",
        "entered_at": iso_z(utc_now()),
        "market_id": market_id,
        "market": {
            "market_id": market_id,
            "event_id": market.get("event_id"),
            "track": market.get("track"),
            "event_name": market.get("event_name"),
            "market_name": market.get("market_name"),
            "market_start_time": market.get("market_start_time"),
        },
        "count": len(rows),
        "top": rows,
        "prices": prices_by_cloth,
    }

    if note:
        payload["note"] = note

    if operator:
        payload["operator"] = operator

    return payload


def print_summary(payload: dict[str, Any], output_path: Path) -> None:
    market = payload.get("market", {})
    race = f"{market.get('track', '')} {market.get('market_name', '')}".strip()
    print(
        "Open Today saved "
        f"market_id={payload.get('market_id')} "
        f"race='{race}' "
        f"file={output_path}"
    )

    for item in payload.get("top", []):
        name = item.get("runner_name") or ""
        name_part = f" {name}" if name else ""
        print(
            f"{item.get('rank'):>2}. "
            f"{item.get('cloth_number'):>3}"
            f"{name_part:<24} "
            f"{item.get('odds')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture manual Open Today top runners for the current ToteBot race."
    )
    parser.add_argument(
        "pairs",
        nargs="*",
        help="Pairs: runner_number odds runner_number odds ...",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help=f"ToteBot base directory (default: {DEFAULT_BASE_DIR})",
    )
    parser.add_argument(
        "--market-id",
        help="Explicit market id. Defaults to current armed/active market.",
    )
    parser.add_argument(
        "--source",
        default="open_today",
        help="Source label, e.g. open_today, tab, sky, manual (default: open_today)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="How many entries to keep (default: 5)",
    )
    parser.add_argument(
        "--note",
        help="Optional note stored in the JSON.",
    )
    parser.add_argument(
        "--operator",
        help="Optional operator/name stored in the JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print, but do not write files.",
    )
    args = parser.parse_args()

    base_dir = args.base_dir.expanduser()

    market = market_from_state(base_dir)

    if args.market_id:
        if market is None:
            market = {"market_id": args.market_id}
        else:
            market = dict(market)
            market["market_id"] = args.market_id

    if not market or not market.get("market_id"):
        print(
            "No current market found. Run tb bf first or wait for betfair_t15_target.json.",
            file=sys.stderr,
        )
        return 2

    if args.pairs:
        try:
            rows = parse_pairs(args.pairs)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    else:
        rows = interactive_pairs(args.limit)

    if not rows:
        print("No Open Today entries supplied.", file=sys.stderr)
        return 2

    rows = rows[: max(1, args.limit)]
    active_runner_map = runner_map_from_active_book(base_dir)
    rows = enrich_rows(rows, active_runner_map)

    payload = build_payload(
        market=market,
        rows=rows,
        source=args.source,
        note=args.note,
        operator=args.operator,
    )

    market_id = str(market["market_id"])
    market_start_time = market.get("market_start_time")
    history_dir = find_history_dir(
        base_dir,
        market_id,
        str(market_start_time) if market_start_time else None,
    )

    if history_dir is None:
        print(f"Could not locate or infer history folder for market_id={market_id}", file=sys.stderr)
        return 2

    output_path = history_dir / OUTPUT_FILE_NAME
    latest_path = base_dir / STATE_DIR_NAME / LATEST_FILE_NAME

    if not args.dry_run:
        atomic_write_json(output_path, payload)
        atomic_write_json(latest_path, payload)

    print_summary(payload, output_path)

    if args.dry_run:
        print("DRY RUN: no files written")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
