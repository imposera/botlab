#!/usr/bin/env python3
"""
ToteBot Blackbook / Winner Register

Builds a small read-only-derived blackbook from completed ToteBot history folders.

Source of truth:
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/closed.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/result.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/market_book_t15.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/market_book_t10.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/market_book_t5.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/market_book_t2.json
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/market_book_t30.json

Writes derived register:
    ~/botlab/totebot/state/tb_blackbook.json

Version 1.5.0 keeps the register deliberately conservative:
- automatically records every observed winner
- keeps market profile / price path for winners
- preserves any existing manual tags/notes/status in tb_blackbook.json
- does not call Betfair
- does not change raw history folders
- filtered scans refresh one market while preserving other registered wins
- all scans retain wins whose source history is unavailable
- confirmed empty results revoke wins while retaining manual metadata
- writes are serialized and unreadable existing registers are not overwritten

Examples:
    python3 tb_blackbook.py scan
    python3 tb_blackbook.py list
    python3 tb_blackbook.py list --min-wins 2
    python3 tb_blackbook.py show "Police Gazette"
"""

from __future__ import annotations

from tb_runner_shape import shape_symbols

import argparse
import fcntl
import json
import math
import os
import re
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from tb_race_lifecycle import scratching_break


DEFAULT_BASE_DIR = Path.home() / "botlab" / "totebot"
STATE_FILE_NAME = "tb_blackbook.json"
VERSION = "1.6.0"

COUNTRIES = {
    'AU': 'Australia', 'GB': 'United Kingdom', 'US': 'United States',
    'FR': 'France', 'ZA': 'South Africa', 'IE': 'Ireland', 'NZ': 'New Zealand',
    'HK': 'Hong Kong', 'SG': 'Singapore', 'JP': 'Japan', 'CA': 'Canada',
    'DE': 'Germany', 'IT': 'Italy', 'AE': 'United Arab Emirates',
}
COUNTRY_ALIASES = {
    'AUS': 'AU', 'UK': 'GB', 'GBR': 'GB', 'USA': 'US', 'FRA': 'FR',
    'RSA': 'ZA', 'ZAF': 'ZA', 'IRL': 'IE', 'NZL': 'NZ',
    **{name.upper(): code for code, name in COUNTRIES.items()},
}


def market_country(market_dir, result, closed):
    """Use explicit source geography only, falling back through captures."""
    def country(source):
        market = (source or {}).get('market') or {}
        for key in ('country_code', 'country'):
            value = str(market.get(key) or '').strip().upper()
            code = COUNTRY_ALIASES.get(value, value)
            if code in COUNTRIES:
                return code
        return None
    for source in (result, closed):
        code = country(source)
        if code:
            return code
    for stage in reversed(STAGE_ORDER):
        code = country(read_json(market_dir / STAGE_FILES[stage]))
        if code:
            return code
    return None

STAGE_FILES = {
    "t15": "market_book_t15.json",
    "t10": "market_book_t10.json",
    "t5": "market_book_t5.json",
    "t2": "market_book_t2.json",
    "t30": "market_book_t30.json",
}
STAGE_ORDER = ["t15", "t10", "t5", "t2", "t30"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


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
        temporary = Path(handle.name)
    os.replace(temporary, path)


def normalise_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def distance_metres(label: Any) -> float | None:
    """Parse an explicit leading race distance; never infer approximate trips."""
    text = str(label or "").strip().lower()
    text = re.sub(r"^r\d+\s+", "", text)
    metric = re.match(r"^(\d{3,5})\s*m(?:etres|eters)?\b", text)
    if metric:
        return float(metric[1])
    imperial = re.match(r"^(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)f)?\s*(?:(\d+)y)?(?=\s|$)", text)
    if imperial and any(imperial.groups()):
        miles, furlongs, yards = (float(v or 0) for v in imperial.groups())
        value = miles * 1609.344 + furlongs * 201.168 + yards * 0.9144
        return round(value, 4) if value > 0 else None
    return None


def runner_key(runner: dict[str, Any]) -> str:
    selection_id = runner.get("selection_id") or runner.get("selectionId")
    if selection_id not in (None, ""):
        return f"sid:{selection_id}"
    return f"name:{normalise_name(runner.get('runner_name') or runner.get('runnerName'))}"


def price_from_runner(runner: dict[str, Any]) -> float | None:
    for key in ("last_price_traded", "lastPriceTraded"):
        value = runner.get(key)
        if value is not None:
            try:
                number = float(value)
                if math.isfinite(number) and number > 0:
                    return number
            except (TypeError, ValueError):
                pass

    for levels_key in ("back_levels", "lay_levels", "availableToBack", "availableToLay"):
        levels = runner.get(levels_key)
        if isinstance(levels, list) and levels:
            first = levels[0]
            if isinstance(first, dict) and first.get("price") is not None:
                try:
                    number = float(first["price"])
                    if math.isfinite(number) and number > 0:
                        return number
                except (TypeError, ValueError):
                    pass
    return None


def pct_move(first: float | None, last: float | None) -> float | None:
    if not first or not last:
        return None
    return ((last - first) / first) * 100.0


def shape_for_prices(prices: dict[str, float]) -> str:
    return shape_symbols(prices)


def first_last(prices: dict[str, float]) -> tuple[float | None, float | None]:
    first = None
    last = None
    for stage in STAGE_ORDER:
        value = prices.get(stage)
        if value is None:
            continue
        if first is None:
            first = value
        last = value
    return first, last


def matched_from_runner(runner: dict[str, Any]) -> float | None:
    """Read reported volume, falling back to summed traded price levels."""
    total = None
    for key in ("total_matched", "totalMatched"):
        try:
            value = float(runner.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            total = value
            break
    if total:
        return total
    exchange = runner.get("ex")
    exchange = exchange if isinstance(exchange, dict) else {}
    for levels in (runner.get("traded_levels"), exchange.get("tradedVolume")):
        if not isinstance(levels, list) or not levels:
            continue
        sizes = []
        for level in levels:
            try:
                size = float(level.get("size")) if isinstance(level, dict) else -1
            except (TypeError, ValueError):
                break
            if not math.isfinite(size) or size < 0:
                break
            sizes.append(size)
        if len(sizes) == len(levels):
            return sum(sizes)
    return total



def snapshot_runner_data(market_dir: Path, winner: dict[str, Any]):
    key = runner_key(winner)
    name_norm = normalise_name(winner.get("runner_name"))
    cloth = str(winner.get("cloth_number") or "")
    prices: dict[str, float] = {}
    matched = {}

    for stage, file_name in STAGE_FILES.items():
        snapshot = read_json(market_dir / file_name)
        if not snapshot:
            continue
        candidates = []
        for runner in snapshot.get("runners", []) or []:
            if not isinstance(runner, dict):
                continue
            candidate_key = runner_key(runner)
            if key.startswith("sid:") and candidate_key.startswith("sid:") and candidate_key != key:
                continue
            if candidate_key == key and key != "name:":
                rank = 0
            elif name_norm and normalise_name(runner.get("runner_name")) == name_norm:
                rank = 1
            elif cloth and str(runner.get("cloth_number") or "") == cloth:
                rank = 2
            else:
                continue
            candidates.append((rank, runner))
        if candidates:
            best_rank = min(rank for rank, runner in candidates)
            matches = [runner for rank, runner in candidates if rank == best_rank]
            if len(matches) != 1:
                continue
            runner = matches[0]
            price = price_from_runner(runner)
            if price is not None:
                prices[stage] = price
            market = snapshot.get("market") or {}
            runner_amount = matched_from_runner(runner)
            market_amount = matched_from_runner(market)
            if market_amount is None:
                market_amount = matched_from_runner(snapshot)
            share = (100 * runner_amount / market_amount
                     if runner_amount is not None and market_amount is not None
                     and market_amount > 0 and runner_amount <= market_amount else None)
            matched[stage] = {
                "runner_matched": runner_amount, "market_matched": market_amount,
                "share_pct": share, "captured_at": snapshot.get("captured_at"),
                "currency": market.get("currency") or snapshot.get("currency"),
                "delayed": bool(market.get("is_market_data_delayed") or snapshot.get("is_market_data_delayed")),
            }
    return prices, matched


def snapshot_price_path(market_dir: Path, winner: dict[str, Any]) -> dict[str, float]:
    return snapshot_runner_data(market_dir, winner)[0]


def race_sort_key(race: dict[str, Any]) -> tuple[str, str]:
    value = race.get("market_start_time") or race.get("date") or ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        value = dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        value = str(race.get("date") or "")
    return str(value), str(race.get("market_id") or "")


def market_label(result: dict[str, Any], closed: dict[str, Any] | None) -> dict[str, Any]:
    market = result.get("market") if isinstance(result.get("market"), dict) else {}
    if not market and closed and isinstance(closed.get("market"), dict):
        market = closed["market"]
    return market


def completed_market_dirs(history_dir: Path, market_id: str | None = None) -> list[Path]:
    if market_id:
        return sorted(path for path in history_dir.glob(f"*/*") if path.is_dir() and path.name == market_id)
    return sorted(path for path in history_dir.glob("*/*") if path.is_dir())


def load_existing_blackbook(path: Path) -> dict[str, dict[str, Any]]:
    existing = read_json(path)
    if path.exists() and (existing is None or not isinstance(existing.get("entries"), list)):
        raise ValueError(f"Refusing to overwrite unreadable blackbook: {path}")
    existing = existing or {}
    entries = existing.get("entries")
    if not isinstance(entries, list):
        return {}
    return {str(item.get("key")): item for item in entries if isinstance(item, dict) and item.get("key")}


def build_blackbook(base_dir: Path, market_id: str | None = None) -> dict[str, Any]:
    history_dir = base_dir / "history"
    state_path = base_dir / "state" / STATE_FILE_NAME
    existing_entries = load_existing_blackbook(state_path)

    aggregate: dict[str, dict[str, Any]] = {}
    scanned = 0
    skipped = 0
    refreshed_markets: set[str] = set()

    for market_dir in completed_market_dirs(history_dir, market_id):
        closed = read_json(market_dir / "closed.json")
        result = read_json(market_dir / "result.json")

        if not closed or not result:
            skipped += 1
            continue
        if not closed.get("complete") or not closed.get("has_result"):
            skipped += 1
            continue

        winners = result.get("winners")
        if not isinstance(winners, list):
            winner = result.get("winner")
            winners = [winner] if isinstance(winner, dict) else []

        # An explicit empty list is a confirmed result with no winners.
        # Missing or malformed winners are not authoritative corrections.
        if not isinstance(result.get("winners"), list) and not isinstance(result.get("winner"), dict):
            skipped += 1
            continue
        if any(not isinstance(w, dict) or runner_key(w) == "name:" for w in winners):
            skipped += 1
            continue

        scanned += 1
        market = market_label(result, closed)
        market_id_value = str(result.get("market_id") or closed.get("market_id") or market_dir.name)
        refreshed_markets.add(market_id_value)
        market_start = market.get("market_start_time") or result.get("market_start_time")
        date = market_dir.parent.name
        country_code = market_country(market_dir, result, closed)
        price_break = scratching_break([read_json(market_dir / file_name) for file_name in STAGE_FILES.values()])

        for winner in winners:
            if not isinstance(winner, dict):
                continue

            key = runner_key(winner)
            if key in ("sid:None", "sid:", "name:"):
                continue

            prices, matched = snapshot_runner_data(market_dir, winner)
            first, last = first_last(prices)
            move = pct_move(first, last) if not price_break else None
            shape = shape_for_prices(prices) if not price_break else "⚠ scratching break"

            race_record = {
                "date": date,
                "market_id": market_id_value,
                "track": market.get("track"),
                "country_code": country_code,
                "country": COUNTRIES.get(country_code),
                "event_name": market.get("event_name"),
                "market_name": market.get("market_name"),
                "distance_metres": distance_metres(market.get("market_name")),
                "market_start_time": market_start,
                "cloth_number": winner.get("cloth_number"),
                "selection_id": winner.get("selection_id"),
                "runner_name": winner.get("runner_name"),
                "prices": prices,
                "matched": matched,
                "first_price": first,
                "last_price": last,
                "move_pct": move,
                "shape": shape,
                "result_status": result.get("result_status"),
                "price_series_break": price_break,
            }

            entry = aggregate.setdefault(
                key,
                {
                    "key": key,
                    "runner_name": winner.get("runner_name"),
                    "normalised_name": normalise_name(winner.get("runner_name")),
                    "selection_ids": [],
                    "wins_seen": 0,
                    "races_seen": 0,
                    "last_seen": None,
                    "last_win": None,
                    "tracks_seen": [],
                    "tags": [],
                    "notes": "",
                    "status": "auto-winner",
                    "wins": [],
                },
            )

            selection_id = winner.get("selection_id")
            if selection_id is not None and selection_id not in entry["selection_ids"]:
                entry["selection_ids"].append(selection_id)

            track = market.get("track")
            if track and track not in entry["tracks_seen"]:
                entry["tracks_seen"].append(track)

            entry["wins_seen"] += 1
            entry["races_seen"] += 1
            entry["last_seen"] = max(str(entry.get("last_seen") or ""), str(date))
            entry["last_win"] = race_record
            entry["wins"].append(race_record)

    # Replace only successfully read markets, even during full scans. Missing
    # history must not erase previously registered wins or manual research.
    for key, old in existing_entries.items():
        retained = [deepcopy(win) for win in old.get("wins", [])
                    if str(win.get("market_id")) not in refreshed_markets]
        manual = bool(old.get("tags") or old.get("notes") or
                      old.get("status") not in (None, "auto-winner"))
        if key in aggregate:
            aggregate[key]["wins"].extend(retained)
        elif retained or manual:
            aggregate[key] = deepcopy(old)
            aggregate[key]["wins"] = retained

    # Recompute summaries after merging, using race times rather than folder order.
    for key, entry in aggregate.items():
        wins = sorted(entry["wins"], key=race_sort_key)
        entry["wins"] = wins
        entry["wins_seen"] = entry["races_seen"] = len(wins)
        entry["last_win"] = wins[-1] if wins else None
        entry["last_seen"] = max((str(win.get("date") or "") for win in wins), default=None)
        entry["selection_ids"] = list(dict.fromkeys(
            win["selection_id"] for win in wins if win.get("selection_id") is not None))
        entry["tracks_seen"] = list(dict.fromkeys(
            win["track"] for win in wins if win.get("track")))
        old = existing_entries.get(key, {})
        for field in ("tags", "notes", "status"):
            if field in old:
                entry[field] = old[field]

    entries = list(aggregate.values())
    entries.sort(key=lambda item: (-int(item.get("wins_seen") or 0), str(item.get("runner_name") or "")))

    return {
        "schema": "totebot_blackbook/v1",
        "version": VERSION,
        "mode": "derived-from-closed-history",
        "generated_at": iso_z(utc_now()),
        "base_dir": str(base_dir),
        "scanned_closed_markets": scanned,
        "skipped": skipped,
        "entry_count": len(entries),
        "entries": entries,
    }


def fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number >= 100:
        return f"{number:.0f}"
    if number >= 10:
        return f"{number:.1f}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.1f}%"


def load_blackbook(base_dir: Path) -> dict[str, Any]:
    return read_json(base_dir / "state" / STATE_FILE_NAME) or {
        "entries": [],
        "entry_count": 0,
        "schema": "totebot_blackbook/v1",
    }


def cmd_scan(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.expanduser()
    output_path = base_dir / "state" / STATE_FILE_NAME
    if args.dry_run:
        payload = build_blackbook(base_dir, args.market_id)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize the complete read/merge/write transaction across scans.
        with (output_path.parent / ".tb_blackbook.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = build_blackbook(base_dir, args.market_id)
            atomic_write_json(output_path, payload)

    print(
        f"BLACKBOOK entries={payload['entry_count']} "
        f"closed_markets={payload['scanned_closed_markets']} "
        f"skipped={payload['skipped']} file={output_path}"
    )
    if args.dry_run:
        print("DRY RUN: no file written")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.expanduser()
    payload = load_blackbook(base_dir)
    entries = [e for e in payload.get("entries", []) if isinstance(e, dict)]
    min_wins = args.min_wins
    entries = [e for e in entries if int(e.get("wins_seen") or 0) >= min_wins]
    entries = entries[: args.limit]

    print(f"🏇 ToteBot Blackbook entries={payload.get('entry_count', len(entries))} generated={payload.get('generated_at', '—')}")
    print(f"{'Wins':>4}  {'Runner':<28} {'Last':<10} {'Track':<16} {'Move':>8}  Shape")
    print("-" * 82)
    for entry in entries:
        last = entry.get("last_win") if isinstance(entry.get("last_win"), dict) else {}
        print(
            f"{int(entry.get('wins_seen') or 0):>4}  "
            f"{str(entry.get('runner_name') or '')[:28]:<28} "
            f"{str(entry.get('last_seen') or '—'):<10} "
            f"{str(last.get('track') or '—')[:16]:<16} "
            f"{fmt_pct(last.get('move_pct')):>8}  "
            f"{last.get('shape') or '—'}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    query = normalise_name(args.query)
    payload = load_blackbook(args.base_dir.expanduser())
    entries = [e for e in payload.get("entries", []) if isinstance(e, dict)]

    matches = [
        e for e in entries
        if query in normalise_name(e.get("runner_name")) or query == normalise_name(e.get("key"))
    ]

    if not matches:
        print(f"No blackbook entry found for: {args.query}", file=sys.stderr)
        return 1

    entry = matches[0]
    print(f"🏇 {entry.get('runner_name')}  wins_seen={entry.get('wins_seen')} status={entry.get('status')}")
    print(f"selection_ids={entry.get('selection_ids')}")
    print(f"tracks_seen={', '.join(entry.get('tracks_seen') or [])}")
    if entry.get("tags"):
        print(f"tags={', '.join(entry.get('tags'))}")
    if entry.get("notes"):
        print(f"notes={entry.get('notes')}")
    print()
    print(f"{'Date':<10} {'Track':<16} {'Race':<22} {'T15':>6} {'T10':>6} {'T5':>6} {'T2':>6} {'T30':>6} {'Move':>8} Shape")
    print("-" * 104)
    for win in entry.get("wins", []):
        prices = win.get("prices") if isinstance(win.get("prices"), dict) else {}
        print(
            f"{str(win.get('date') or '—'):<10} "
            f"{str(win.get('track') or '—')[:16]:<16} "
            f"{str(win.get('market_name') or '—')[:22]:<22} "
            f"{fmt_price(prices.get('t15')):>6} "
            f"{fmt_price(prices.get('t10')):>6} "
            f"{fmt_price(prices.get('t5')):>6} "
            f"{fmt_price(prices.get('t2')):>6} "
            f"{fmt_price(prices.get('t30')):>6} "
            f"{fmt_pct(win.get('move_pct')):>8} "
            f"{win.get('shape') or '—'}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/list ToteBot blackbook from closed history folders.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)

    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Build derived blackbook from closed history.")
    scan.add_argument("--market-id", help="Refresh one market ID, preserving other registered wins.")
    scan.add_argument("--dry-run", action="store_true")

    listing = sub.add_parser("list", help="List blackbook entries.")
    listing.add_argument("--limit", type=int, default=40)
    listing.add_argument("--min-wins", type=int, default=1)

    show = sub.add_parser("show", help="Show one runner entry.")
    show.add_argument("query")

    args = parser.parse_args()

    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
