#!/usr/bin/env python3
"""
ToteBot race review lens.

Observer-only derived analysis for one completed Betfair race.

Version 1.2.0 validates completion and usable captures, orders races by start
time, and keeps insufficient observations separate from flat price paths.

Reads:
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/

Default:
    tb_review.py --latest

Optional derived write:
    tb_review.py --latest --write

Writes only when --write is supplied:
    ~/botlab/totebot/review/YYYY-MM-DD/<market_id>.json

Raw history is never modified.
This is observer coaching, not a betting command.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from tb_race_lifecycle import scratching_break
from tb_runner_shape import shape_class

DEFAULT_BASE_DIR = Path.home() / "botlab" / "totebot"
HISTORY_DIR_NAME = "history"
REVIEW_DIR_NAME = "review"
VERSION = "1.2.0"

SNAPSHOT_FILES = (
    ("T-15", "market_book_t15.json"),
    ("T-10", "market_book_t10.json"),
    ("T-5", "market_book_t5.json"),
    ("T-2", "market_book_t2.json"),
    ("T-30", "market_book_t30.json"),
)

RESULT_NAMES = (
    "result.json",
    "market_result.json",
    "race_result.json",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        value = float(value)
        if math.isfinite(value):
            return value
    return None


def clean_text(value: Any, fallback: str = "") -> str:
    value = str(value or "").strip()
    return value if value else fallback


def runner_key(runner: dict[str, Any]) -> str:
    selection_id = runner.get("selection_id") or runner.get("selectionId")
    if selection_id is not None:
        return f"sid:{selection_id}"
    cloth = runner.get("cloth_number") or runner.get("clothNumber") or ""
    name = runner.get("runner_name") or runner.get("runnerName") or ""
    return f"cloth:{cloth}:{name}"


def runner_name(runner: dict[str, Any]) -> str:
    return clean_text(runner.get("runner_name") or runner.get("runnerName") or runner.get("name"))


def runner_no(runner: dict[str, Any]) -> str:
    return clean_text(runner.get("cloth_number") or runner.get("clothNumber") or runner.get("number"))


def price_from_runner(runner: dict[str, Any]) -> float | None:
    for key in ("last_price_traded", "lastPriceTraded", "price"):
        value = number(runner.get(key))
        if value is not None and value > 0:
            return value
    for levels_key in ("back_levels", "lay_levels", "availableToBack", "availableToLay"):
        levels = runner.get(levels_key)
        if isinstance(levels, list) and levels and isinstance(levels[0], dict):
            value = number(levels[0].get("price"))
            if value is not None and value > 0:
                return value
    return None


def result_for_race(race_dir: Path) -> dict[str, Any] | None:
    for name in RESULT_NAMES:
        result = read_json(race_dir / name)
        if result:
            return result
    for path in sorted(race_dir.glob("*result*.json")):
        result = read_json(path)
        if result:
            return result
    return None


def winner_runners(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not result:
        return []
    winners = result.get("winners")
    if not isinstance(winners, list) or not winners:
        winner = result.get("winner")
        winners = [winner] if isinstance(winner, dict) else []
    return [runner for runner in winners if isinstance(runner, dict)]


def winner_keys(result: dict[str, Any] | None) -> set[str]:
    return {runner_key(runner) for runner in winner_runners(result)}


def race_completed(race_dir: Path) -> bool:
    result = result_for_race(race_dir)
    if not result:
        return False
    status = clean_text(result.get("market_status")).upper()
    if status and status != "CLOSED":
        return False
    if clean_text(result.get("result_status")).lower() in {"pending", "open", "unresolved", "error"}:
        return False
    closed = read_json(race_dir / "closed.json")
    if (race_dir / "closed.json").exists():
        return bool(closed and closed.get("complete") is True and closed.get("has_result") is True)
    # Older folders may lack a closure marker; require an explicit closed result.
    return status == "CLOSED"


def race_time_key(race_dir: Path) -> tuple[datetime, str]:
    sources = [result_for_race(race_dir), read_json(race_dir / "closed.json")]
    sources.extend(read_json(race_dir / file_name) for _, file_name in SNAPSHOT_FILES)
    for source in sources:
        if not source:
            continue
        market = source.get("market") if isinstance(source.get("market"), dict) else {}
        value = market.get("market_start_time") or source.get("market_start_time")
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc), str(race_dir)
        except ValueError:
            continue
    try:
        day = datetime.strptime(race_dir.parent.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        day = datetime.min.replace(tzinfo=timezone.utc)
    return day, str(race_dir)


def discover_completed_races(history_dir: Path) -> list[Path]:
    races: list[Path] = []
    if not history_dir.is_dir():
        return races
    for day_dir in history_dir.iterdir():
        if not day_dir.is_dir():
            continue
        for race_dir in day_dir.iterdir():
            if race_dir.is_dir() and race_completed(race_dir):
                races.append(race_dir)
    races.sort(key=race_time_key, reverse=True)
    return races


def find_market_race(history_dir: Path, market_id: str) -> Path | None:
    matches = [
        path for path in history_dir.glob("*/*")
        if path.is_dir() and path.name == market_id
    ]
    matches.sort(key=race_time_key, reverse=True)
    return matches[0] if matches else None


def load_snapshots(race_dir: Path):
    snapshots: dict[str, dict[str, Any]] = {}
    prices: dict[str, dict[str, float]] = {}
    runners: dict[str, dict[str, Any]] = {}
    for stage, file_name in SNAPSHOT_FILES:
        snapshot = read_json(race_dir / file_name)
        if not snapshot:
            continue
        snapshots[stage] = snapshot
        snapshot_runners = snapshot.get("runners")
        for runner in snapshot_runners if isinstance(snapshot_runners, list) else []:
            if not isinstance(runner, dict):
                continue
            key = runner_key(runner)
            runners.setdefault(key, runner)
            price = price_from_runner(runner)
            if price is not None:
                prices.setdefault(key, {})[stage] = price
    return snapshots, prices, runners


def first_last(stage_prices: dict[str, float]) -> tuple[float | None, float | None]:
    first = None
    last = None
    for stage, _ in SNAPSHOT_FILES:
        value = stage_prices.get(stage)
        if value is None:
            continue
        if first is None:
            first = value
        last = value
    return first, last


def pct_move(first: float | None, last: float | None) -> float | None:
    if first is None or last is None or first <= 0:
        return None
    return ((last - first) / first) * 100.0


def movement_class(move_pct: float | None) -> str:
    # Negative odds move = firming; positive = drifting.
    if move_pct is None:
        return "unknown"
    if move_pct <= -50.0:
        return "extreme_firm"
    if move_pct >= 50.0:
        return "extreme_drift"
    if move_pct <= -5.0:
        return "firmed"
    if move_pct >= 5.0:
        return "drifted"
    return "flat"


def movement_directions(stage_prices: dict[str, float]) -> list[int]:
    values = [stage_prices[stage] for stage, _ in SNAPSHOT_FILES if stage in stage_prices]
    directions: list[int] = []
    for previous, current in zip(values, values[1:]):
        if current < previous:
            directions.append(-1)
        elif current > previous:
            directions.append(1)
        else:
            directions.append(0)
    return directions



def market_rank_from_first(rows: list[dict[str, Any]]) -> dict[str, int]:
    ranked = sorted(
        (row for row in rows if row.get("first_price") is not None),
        key=lambda row: (float(row["first_price"]), row.get("no") or "", row.get("name") or ""),
    )
    return {row["key"]: index for index, row in enumerate(ranked, start=1)}


def market_role(rank: int | None, field_size: int) -> str:
    if rank is None:
        return "unknown"
    if rank == 1:
        return "favourite"
    if rank <= 3:
        return "top_3"
    top_half = (field_size // 2) + (field_size % 2)
    return "top_half" if rank <= top_half else "outsider"


def market_from_snapshots(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for stage, _ in SNAPSHOT_FILES:
        snapshot = snapshots.get(stage)
        if snapshot and isinstance(snapshot.get("market"), dict):
            return snapshot["market"]
    return {}


def capture_status(race_dir: Path, snapshots: dict[str, dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    if snapshots is None:
        snapshots, _, _ = load_snapshots(race_dir)
    present = []
    for stage, _ in SNAPSHOT_FILES:
        runners = snapshots.get(stage, {}).get("runners")
        if isinstance(runners, list) and any(
            isinstance(runner, dict) and price_from_runner(runner) is not None for runner in runners
        ):
            present.append(stage)
    if len(present) == len(SNAPSHOT_FILES):
        return "complete", present
    if present:
        return "partial", present
    return "missing", []


def build_review(race_dir: Path) -> dict[str, Any]:
    snapshots, snapshot_prices, runner_map = load_snapshots(race_dir)
    result = result_for_race(race_dir)
    winners = winner_keys(result)
    market = market_from_snapshots(snapshots)
    if not market and result and isinstance(result.get("market"), dict):
        market = result["market"]
    capture, stages_present = capture_status(race_dir, snapshots)
    price_break = scratching_break([snapshots.get(stage) for stage, _ in SNAPSHOT_FILES])

    if result and isinstance(result.get("runners"), list):
        for runner in result["runners"]:
            if isinstance(runner, dict):
                runner_map.setdefault(runner_key(runner), runner)
    for winner in winner_runners(result):
        stored = runner_map.setdefault(runner_key(winner), dict(winner))
        for field, value in winner.items():
            if not stored.get(field) and value is not None:
                stored[field] = value

    all_keys = sorted(set(runner_map) | set(snapshot_prices) | winners)
    rows: list[dict[str, Any]] = []

    for key in all_keys:
        runner = runner_map.get(key, {})
        stage_prices = snapshot_prices.get(key, {})
        first, last = first_last(stage_prices)
        move = pct_move(first, last) if len(stage_prices) >= 2 and not price_break else None
        rows.append({
            "key": key,
            "selection_id": clean_text(runner.get("selection_id") or runner.get("selectionId")),
            "no": runner_no(runner),
            "name": runner_name(runner) or key,
            "winner": key in winners,
            "prices": stage_prices,
            "first_price": first,
            "last_price": last,
            "move_pct": round(move, 2) if move is not None else None,
            "movement_class": "adjustment_break" if price_break else movement_class(move),
            "shape_class": "scratching_break" if price_break else shape_class(stage_prices),
        })

    field_size = len(rows)
    ranks = market_rank_from_first(rows)
    top_half_count = (field_size // 2) + (field_size % 2)

    for row in rows:
        rank = ranks.get(row["key"])
        row["market_rank"] = rank
        row["market_role"] = market_role(rank, field_size)
        row["cohail_top_half"] = bool(rank is not None and rank <= top_half_count)

    rows.sort(key=lambda row: (row["market_rank"] is None, row["market_rank"] or 9999, row["no"], row["name"]))
    winner_rows = [row for row in rows if row["winner"]]
    top_half_rows = [row for row in rows if row["cohail_top_half"]]
    top_half_drifters = [row for row in top_half_rows if row["movement_class"] in {"drifted", "extreme_drift"}]
    top_half_firmers_50 = [row for row in top_half_rows if row["move_pct"] is not None and row["move_pct"] <= -50.0]
    extreme_firmers = [row for row in rows if row["movement_class"] == "extreme_firm"]
    extreme_drifters = [row for row in rows if row["movement_class"] == "extreme_drift"]

    move_rows = [row for row in rows if row["move_pct"] is not None]
    largest_firmer = min((row for row in move_rows if row["move_pct"] < 0), key=lambda row: row["move_pct"], default=None)
    largest_drifter = max((row for row in move_rows if row["move_pct"] > 0), key=lambda row: row["move_pct"], default=None)

    if top_half_drifters:
        cohail_observation = "top_half_drifter_present"
    elif top_half_firmers_50:
        cohail_observation = "no_top_half_drifter_but_50pct_firmer_present"
    else:
        cohail_observation = "no_primary_cohail_movement_candidate"

    return {
        "schema": "totebot.race_review.v1",
        "version": VERSION,
        "generated_at": utc_now().isoformat(),
        "source": "tb_review.py",
        "observer_only": True,
        "race": {
            "date": race_dir.parent.name,
            "market_id": clean_text(market.get("market_id"), race_dir.name),
            "track": clean_text(market.get("track"), "UNKNOWN"),
            "country": clean_text(market.get("country_code") or market.get("country"), "UNKNOWN"),
            "state": clean_text(market.get("state") or market.get("region"), "UNKNOWN"),
            "event_name": clean_text(market.get("event_name")),
            "market_name": clean_text(market.get("market_name")),
            "market_start_time": market.get("market_start_time"),
            "field_size": field_size,
            "market_total_matched": number(market.get("total_matched")),
            "capture": capture,
            "price_series_break": price_break,
            "stages_present": stages_present,
            "result_found": result is not None,
        },
        "winner": winner_rows[0] if len(winner_rows) == 1 else None,
        "winners": winner_rows,
        "extremes": {
            "largest_firmer": largest_firmer,
            "largest_drifter": largest_drifter,
            "extreme_firmers": extreme_firmers,
            "extreme_drifters": extreme_drifters,
        },
        "cohail_lens": {
            "top_half_count": top_half_count,
            "top_half_drifter_present": bool(top_half_drifters),
            "top_half_drifters": top_half_drifters,
            "top_half_firmer_50_present": bool(top_half_firmers_50),
            "top_half_firmers_50": top_half_firmers_50,
            "winner_in_top_half": any(row["cohail_top_half"] for row in winner_rows),
            "winner_drifted": any(row["movement_class"] in {"drifted", "extreme_drift"} for row in winner_rows),
            "winner_firmed_50": any(row["move_pct"] is not None and row["move_pct"] <= -50.0 for row in winner_rows),
            "observation": cohail_observation,
        },
        "runners": rows,
    }


def fmt_price(value: Any) -> str:
    value = number(value)
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def fmt_pct(value: Any) -> str:
    value = number(value)
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def yes_no(value: bool) -> str:
    return "YES" if value else "NO"


def display_runner(row: dict[str, Any] | None) -> str:
    if not row:
        return "—"
    return f"{row.get('no') or ''} {row.get('name') or ''}".strip() + f" ({fmt_pct(row.get('move_pct'))}, {row.get('movement_class')})"


def print_review(review: dict[str, Any]) -> None:
    race = review["race"]
    winner = review.get("winner")
    cohail = review["cohail_lens"]
    extremes = review["extremes"]
    title = " ".join(part for part in (race.get("track"), race.get("market_name")) if part)

    print()
    print(f"🏇 ToteBot Review v{VERSION} — {title or race['market_id']}")
    print()
    print(f"Market: {race['market_id']}  {race['country']}/{race['state']}")
    print(f"Capture: {race['capture']} ({', '.join(race['stages_present']) or 'none'})")
    if race.get("market_total_matched") is not None:
        print(f"Matched at earliest snapshot: {race['market_total_matched']:,.0f}")
    print(f"Field: {race['field_size']}")

    if winner:
        print(
            f"Winner: {winner.get('no') or ''} {winner.get('name') or ''}  "
            f"{fmt_price(winner.get('first_price'))} → {fmt_price(winner.get('last_price'))}  "
            f"{fmt_pct(winner.get('move_pct'))}  {winner.get('movement_class')} / {winner.get('shape_class')}"
        )
    elif review.get("winners"):
        print("Winners: " + ", ".join(display_runner(row) for row in review["winners"]))
    else:
        print("Winner: result present but winner could not be matched")

    print()
    print("Shape notes:")
    interesting = sorted(review["runners"], key=lambda row: (row["winner"] is False, -abs(row["move_pct"] or 0.0)))
    for row in interesting[:8]:
        marker = "★" if row["winner"] else " "
        print(
            f" {marker} {(row.get('no') or ''):>2} {(row.get('name') or '')[:24]:24} "
            f"{fmt_price(row.get('first_price')):>6} → {fmt_price(row.get('last_price')):<6} "
            f"{fmt_pct(row.get('move_pct')):>8}  {row.get('movement_class'):14} {row.get('shape_class')}"
        )

    print()
    print("Cohail lens:")
    print(f"  Top-half runners: {cohail['top_half_count']}/{race['field_size']}")
    print(f"  Top-half drifter present: {yes_no(cohail['top_half_drifter_present'])}")
    print(f"  Top-half 50% firmer present: {yes_no(cohail['top_half_firmer_50_present'])}")
    print(f"  Winner in top half: {yes_no(cohail['winner_in_top_half'])}")
    print(f"  Winner drifted: {yes_no(cohail['winner_drifted'])}")
    print(f"  Winner firmed >=50%: {yes_no(cohail['winner_firmed_50'])}")
    print(f"  Observation: {cohail['observation']}")

    print()
    print("Largest firmer: " + display_runner(extremes.get("largest_firmer")))
    print("Largest drifter: " + display_runner(extremes.get("largest_drifter")))
    print()
    print("Observer only — classifications describe what happened; they are not betting instructions.")


def review_output_path(base_dir: Path, race_dir: Path) -> Path:
    return base_dir / REVIEW_DIR_NAME / race_dir.parent.name / f"{race_dir.name}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Review one completed ToteBot race.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--latest", action="store_true", help="Review the most recently completed race (default).")
    target.add_argument("--market-id", help="Review a specific Betfair market ID.")
    parser.add_argument("--write", action="store_true", help="Write a derived review JSON under ~/botlab/totebot/review/.")
    parser.add_argument("--json", action="store_true", help="Print the review as JSON instead of human output.")
    args = parser.parse_args()

    base_dir = args.base_dir.expanduser()
    history_dir = base_dir / HISTORY_DIR_NAME
    if not history_dir.is_dir():
        print(f"History directory not found: {history_dir}")
        return 1

    if args.market_id:
        race_dir = find_market_race(history_dir, args.market_id)
        if race_dir is None:
            print(f"Market not found: {args.market_id}")
            return 1
        if not race_completed(race_dir):
            print(f"Market is not confirmed complete: {args.market_id}", file=sys.stderr)
            return 1
    else:
        completed = discover_completed_races(history_dir)
        if not completed:
            print(f"No completed races found under: {history_dir}")
            return 1
        race_dir = completed[0]

    review = build_review(race_dir)
    if args.json:
        print(json.dumps(review, indent=2, ensure_ascii=False))
    else:
        print_review(review)

    if args.write:
        output_path = review_output_path(base_dir, race_dir)
        atomic_write_json(output_path, review)
        print(f"Derived review JSON: {output_path}", file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
