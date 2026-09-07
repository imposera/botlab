#!/usr/bin/env python3
"""
ToteBot Watchboard
==================

Read-only terminal watchboard for synced ToteBot state and history.

Runs on core7070, Zenbook, or any node that has the synced files.

Reads:
    ~/botlab/totebot/state/betfair_watch_status.json
    ~/botlab/totebot/state/betfair_t15_target.json
    ~/botlab/totebot/state/active_market_book.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/

Examples:
    python3 totebot_watch.py
    python3 totebot_watch.py --once
    python3 totebot_watch.py --interval 5
    python3 totebot_watch.py --market-id 1.259722038 --once

Controls:
    Ctrl-C to exit live watch mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path.home() / "botlab" / "totebot" / "state"
DEFAULT_HISTORY_DIR = Path.home() / "botlab" / "totebot" / "history"

SLOT_ORDER = ["T-15", "T-10", "T-5", "T-2", "T-30"]
SLOT_FILES = {
    "T-15": "market_book_t15.json",
    "T-10": "market_book_t10.json",
    "T-5": "market_book_t5.json",
    "T-2": "market_book_t2.json",
    "T-30": "market_book_t30.json",
}

WIDTH = 96


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file safely. Missing/bad files simply appear unavailable."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def parse_time(value: str | None) -> datetime | None:
    """Parse ISO-8601 timestamps used by Betfair and Botlab state."""
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def age_text(timestamp: str | None) -> str:
    """Return a concise age like 12s, 4m, 2h."""
    parsed = parse_time(timestamp)

    if parsed is None:
        return "unknown"

    seconds = int((now_utc() - parsed).total_seconds())

    if seconds < 0:
        return "future"

    if seconds < 60:
        return f"{seconds}s"

    minutes, remainder = divmod(seconds, 60)

    if minutes < 60:
        return f"{minutes}m {remainder}s"

    hours, minutes = divmod(minutes, 60)

    if hours < 24:
        return f"{hours}h {minutes}m"

    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def duration_text(seconds: int | float | None) -> str:
    """Format seconds-to-jump as T-12:34 or +0:12."""
    if seconds is None:
        return "unknown"

    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "unknown"

    prefix = "T-" if value >= 0 else "+"
    value = abs(value)
    minutes, secs = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{prefix}{hours}:{minutes:02d}:{secs:02d}"

    return f"{prefix}{minutes}:{secs:02d}"


def number_text(value: Any, decimals: int = 2) -> str:
    """Display numbers compactly while safely handling missing values."""
    if value is None:
        return "-"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"

    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}m"

    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}k"

    return f"{number:.{decimals}f}"


def price_text(value: Any) -> str:
    if value is None:
        return "-"

    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def pct_change(old: Any, new: Any) -> str:
    """
    Show price change from first retained snapshot to latest.

    In racing odds:
    - negative percentage = shortened
    - positive percentage = drifted
    """
    try:
        old_value = float(old)
        new_value = float(new)
    except (TypeError, ValueError):
        return "-"

    if old_value <= 0:
        return "-"

    change = ((new_value - old_value) / old_value) * 100

    if abs(change) < 0.05:
        return "flat"

    if change < 0:
        return f"{change:.1f}% short"

    return f"+{change:.1f}% drift"


def first_price(levels: Any) -> tuple[Any, Any]:
    """Return first price/size from back or lay levels."""
    if not isinstance(levels, list) or not levels:
        return None, None

    first = levels[0]

    if not isinstance(first, dict):
        return None, None

    return first.get("price"), first.get("size")


def clear_screen(enabled: bool) -> None:
    if enabled and sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def line(char: str = "─") -> str:
    return char * WIDTH


def safe_get(mapping: Any, *keys: str) -> Any:
    current = mapping

    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)

    return current


def find_history_dir(
    history_dir: Path,
    market_id: str,
    market_start_time: str | None = None,
) -> Path | None:
    """
    Prefer expected YYYY-MM-DD/market_id path, then fall back to a search.

    Market start date is UTC because that matches the collector's history path.
    """
    parsed = parse_time(market_start_time)

    if parsed is not None:
        expected = history_dir / parsed.strftime("%Y-%m-%d") / market_id
        if expected.is_dir():
            return expected

    candidates = sorted(
        history_dir.glob(f"*/*/{market_id}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    return candidates[0] if candidates else None


def latest_history_market_dir(history_dir: Path) -> Path | None:
    """Find the most recently modified market history directory."""
    candidates: list[Path] = []

    for path in history_dir.glob("*/*"):
        if path.is_dir() and (path / "capture_manifest.json").exists():
            candidates.append(path)

    if not candidates:
        return None

    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_history_snapshots(history_market_dir: Path | None) -> dict[str, dict[str, Any]]:
    if history_market_dir is None:
        return {}

    snapshots: dict[str, dict[str, Any]] = {}

    for slot, file_name in SLOT_FILES.items():
        snapshot = read_json(history_market_dir / file_name)

        if snapshot:
            snapshots[slot] = snapshot

    return snapshots


def snapshot_runner_map(snapshot: dict[str, Any] | None) -> dict[Any, dict[str, Any]]:
    if not snapshot:
        return {}

    runners = snapshot.get("runners", [])

    if not isinstance(runners, list):
        return {}

    result: dict[Any, dict[str, Any]] = {}

    for runner in runners:
        if isinstance(runner, dict) and runner.get("selection_id") is not None:
            result[runner["selection_id"]] = runner

    return result


def earliest_snapshot(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for slot in SLOT_ORDER:
        if slot in snapshots:
            return snapshots[slot]
    return None


def latest_snapshot(
    active_book: dict[str, Any] | None,
    snapshots: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """
    Prefer active current book only when it matches a known market.
    Otherwise use the latest archived stage.
    """
    if active_book:
        return active_book.get("snapshot_slot", "LIVE"), active_book

    for slot in reversed(SLOT_ORDER):
        if slot in snapshots:
            return slot, snapshots[slot]

    return "-", None


def manifest_slot_status(
    manifest: dict[str, Any] | None,
    slot: str,
    snapshots: dict[str, dict[str, Any]],
) -> str:
    if slot in snapshots:
        return "✓"

    status = safe_get(manifest, "slots", slot, "status")

    if status == "missed":
        return "·"

    if status == "captured":
        return "✓"

    return "—"


def market_matches(
    candidate_book: dict[str, Any] | None,
    market_id: str | None,
) -> bool:
    if not candidate_book or not market_id:
        return False

    return safe_get(candidate_book, "market", "market_id") == market_id


def market_label(market: dict[str, Any] | None) -> str:
    if not isinstance(market, dict):
        return "No market selected"

    track = market.get("track") or "Unknown track"
    race = market.get("market_name") or "Unknown race"
    return f"{track} · {race}"


def render_header() -> None:
    print("🏇 TOTEBOT WATCHBOARD")
    print(line())


def render_discovery(discovery: dict[str, Any] | None) -> None:
    print("DISCOVERY")

    if not discovery:
        print("  Status       unavailable")
        print()
        return

    next_market = discovery.get("next_market") or {}
    status = discovery.get("status", "unknown")
    reason = discovery.get("reason", "-")

    print(f"  Status       {status}")
    print(f"  Reason       {reason}")

    if next_market:
        print(f"  Next market  {market_label(next_market)}")
        print(
            "  Starts       "
            f"{duration_text(next_market.get('seconds_to_jump'))} "
            f"({next_market.get('market_start_time', '-')})"
        )
        print(f"  Market ID    {next_market.get('market_id', '-')}")

    print(f"  Updated      {age_text(discovery.get('updated_at'))} ago")
    print()


def render_target(target: dict[str, Any] | None) -> tuple[str | None, dict[str, Any] | None]:
    print("ACTIVE TARGET")

    if not target:
        print("  No target file available.")
        print()
        return None, None

    status = target.get("status", "unknown")
    market = target.get("market") or {}
    arm_id = target.get("arm_id")

    print(f"  Status       {status}")
    print(f"  Race         {market_label(market)}")
    print(f"  Arm ID       {arm_id or '-'}")
    print(f"  Market ID    {market.get('market_id', '-')}")
    print(
        "  Clock        "
        f"{duration_text(market.get('seconds_to_jump'))} "
        f"({market.get('market_start_time', '-')})"
    )
    print(f"  Updated      {age_text(target.get('updated_at'))} ago")
    print()

    return market.get("market_id"), market


def render_capture_status(
    manifest: dict[str, Any] | None,
    snapshots: dict[str, dict[str, Any]],
    history_market_dir: Path | None,
) -> None:
    print("CAPTURE HISTORY")

    slot_text = "  ".join(
        f"{slot} {manifest_slot_status(manifest, slot, snapshots)}"
        for slot in SLOT_ORDER
    )

    print(f"  Stages       {slot_text}")

    if history_market_dir:
        print(f"  Archive      {history_market_dir}")
    else:
        print("  Archive      no matching history folder yet")

    if manifest:
        print(f"  Manifest     updated {age_text(manifest.get('updated_at'))} ago")

    print()


def render_market_summary(
    source_slot: str,
    source_book: dict[str, Any] | None,
) -> None:
    print("LATEST MARKET BOOK")

    if not source_book:
        print("  Awaiting first captured market book.")
        print()
        return

    market = source_book.get("market") or {}

    print(f"  Source       {source_slot}")
    print(f"  Race         {market_label(market)}")
    print(
        "  Clock        "
        f"{duration_text(market.get('seconds_to_jump'))} "
        f"· captured {age_text(source_book.get('captured_at'))} ago"
    )
    print(
        "  Market       "
        f"{market.get('market_status', '-')} "
        f"· in-play={market.get('inplay', '-')}"
    )
    print(
        "  Matched      "
        f"{number_text(market.get('total_matched'))} "
        f"· delayed={market.get('is_market_data_delayed', '-')}"
    )
    print()


def sorted_runners(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank runners by LTP, then by cloth number/sort priority where possible."""
    runners = snapshot.get("runners", [])

    if not isinstance(runners, list):
        return []

    clean = [runner for runner in runners if isinstance(runner, dict)]

    def sort_key(runner: dict[str, Any]) -> tuple[int, float, int]:
        ltp = runner.get("last_price_traded")

        try:
            ltp_value = float(ltp)
            missing = 0
        except (TypeError, ValueError):
            ltp_value = 99999.0
            missing = 1

        priority = runner.get("sort_priority")

        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            priority_value = 9999

        return missing, ltp_value, priority_value

    return sorted(clean, key=sort_key)


def render_runner_table(
    source_book: dict[str, Any] | None,
    baseline_book: dict[str, Any] | None,
) -> None:
    print("RUNNERS — LATEST MARKET SHAPE")

    if not source_book:
        print("  No runner-level market book available.")
        print()
        return

    baseline = snapshot_runner_map(baseline_book)
    runners = sorted_runners(source_book)

    if not runners:
        print("  No runners in current market book.")
        print()
        return

    print(
        "  "
        f"{'Rk':>2} "
        f"{'No':>3} "
        f"{'Runner':<24} "
        f"{'LTP':>7} "
        f"{'Move':<13} "
        f"{'Back':>7} "
        f"{'Lay':>7} "
        f"{'Matched':>9}"
    )
    print("  " + "─" * (WIDTH - 2))

    for rank, runner in enumerate(runners, start=1):
        selection_id = runner.get("selection_id")
        old_runner = baseline.get(selection_id, {})
        old_ltp = old_runner.get("last_price_traded")

        back_price, _back_size = first_price(runner.get("back_levels"))
        lay_price, _lay_size = first_price(runner.get("lay_levels"))

        cloth = runner.get("cloth_number") or "-"
        name = str(runner.get("runner_name") or "Unknown")[:24]

        print(
            "  "
            f"{rank:>2} "
            f"{str(cloth):>3} "
            f"{name:<24} "
            f"{price_text(runner.get('last_price_traded')):>7} "
            f"{pct_change(old_ltp, runner.get('last_price_traded')):<13} "
            f"{price_text(back_price):>7} "
            f"{price_text(lay_price):>7} "
            f"{number_text(runner.get('total_matched')):>9}"
        )

    print()


def render_footer(
    active_book: dict[str, Any] | None,
    history_market_dir: Path | None,
) -> None:
    print(line())

    active_age = age_text(active_book.get("captured_at")) if active_book else "n/a"
    history_text = str(history_market_dir) if history_market_dir else "not found"

    print(
        f"Current book age: {active_age} "
        f"· History: {history_text}"
    )
    print("Read-only view · Ctrl-C exits live mode")


def render(
    state_dir: Path,
    history_dir: Path,
    requested_market_id: str | None = None,
    clear: bool = True,
) -> None:
    clear_screen(clear)

    discovery = read_json(state_dir / "betfair_watch_status.json")
    target = read_json(state_dir / "betfair_t15_target.json")
    active_book = read_json(state_dir / "active_market_book.json")

    target_market_id = safe_get(target, "market", "market_id")
    target_start_time = safe_get(target, "market", "market_start_time")

    market_id = requested_market_id or target_market_id

    if market_id:
        history_market_dir = find_history_dir(
            history_dir,
            market_id,
            target_start_time,
        )
    else:
        history_market_dir = latest_history_market_dir(history_dir)

    manifest = (
        read_json(history_market_dir / "capture_manifest.json")
        if history_market_dir
        else None
    )

    snapshots = load_history_snapshots(history_market_dir)

    # Only show active live book if it belongs to selected market.
    if market_id and not market_matches(active_book, market_id):
        current_book = None
    else:
        current_book = active_book

    source_slot, source_book = latest_snapshot(current_book, snapshots)
    baseline_book = earliest_snapshot(snapshots)

    render_header()
    render_discovery(discovery)

    if requested_market_id:
        print("HISTORY TARGET")
        print(f"  Forced market {requested_market_id}")
        print()

    render_target(target)
    render_capture_status(manifest, snapshots, history_market_dir)
    render_market_summary(source_slot, source_book)
    render_runner_table(source_book, baseline_book)
    render_footer(current_book, history_market_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ToteBot market/history watchboard."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Synced current-state directory (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=DEFAULT_HISTORY_DIR,
        help=f"Synced race history directory (default: {DEFAULT_HISTORY_DIR})",
    )
    parser.add_argument(
        "--market-id",
        help="Display a specific historical market ID.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render once and exit.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Refresh interval in seconds for live mode (default: 5).",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear terminal between refreshes.",
    )
    args = parser.parse_args()

    state_dir = args.state_dir.expanduser()
    history_dir = args.history_dir.expanduser()

    if args.interval <= 0:
        print("--interval must be greater than zero.", file=sys.stderr)
        return 2

    try:
        while True:
            render(
                state_dir=state_dir,
                history_dir=history_dir,
                requested_market_id=args.market_id,
                clear=not args.no_clear,
            )

            if args.once:
                return 0

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nWatchboard closed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
