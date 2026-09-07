#!/usr/bin/env python3
"""
ToteBot Mini Wall

Tiny read-only HTTP watcher wall for core7070.

Purpose:
- Fast glance at the currently armed / active ToteBot race.
- Read synced state/history files only.
- No Betfair credentials.
- No writes.
- Read-only previous/current/next race navigation.
- No betting.

Default:
    http://127.0.0.1:8787

LAN/Tailscale:
    python3 tb_wall.py --host 0.0.0.0 --port 8787
"""

from __future__ import annotations

import argparse
import html
import json
import math
import time
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from tb_blackbook_wall import cell as blackbook_cell, enrich_rows
from tb_market_volume import panel as volume_panel
from tb_review import shape_class
from tb_race_lifecycle import load_lifecycle, policy as observation_policy, scratching_break
from tb_race_observation_wall import panel as observation_panel, timing_label


VERSION = "1.5.0"
HISTORY_CACHE_SECONDS = 60.0
DEFAULT_BASE_DIR = Path.home() / "botlab" / "totebot"
STATE_DIR_NAME = "state"
HISTORY_DIR_NAME = "history"

SNAPSHOT_FILES = {
    "T-15": "market_book_t15.json",
    "T-10": "market_book_t10.json",
    "T-5": "market_book_t5.json",
    "T-2": "market_book_t2.json",
    "T-30": "market_book_t30.json",
}

STAGES = ["T-15", "T-10", "T-5", "T-2", "T-30"]

SHAPE_STYLES = {
    "steady_firm": ("Steady firm", "Consistent shortening", "#80d8a3", "#163e2c"),
    "steady_drift": ("Steady drift", "Consistent lengthening", "#f0cc78", "#44351c"),
    "late_firm": ("Late firm", "Latest direction is shortening", "#79d9d0", "#153d3b"),
    "late_drift": ("Late drift", "Latest direction is lengthening", "#ffb779", "#493020"),
    "v_shape": ("V-shape", "Shortened, then lengthened", "#d6b5ff", "#372749"),
    "whipsaw": ("Whipsaw", "Repeated direction changes", "#d6b5ff", "#372749"),
    "flat_hold": ("Flat hold", "Small overall price range", "#a9c6e5", "#243448"),
    "insufficient": ("Insufficient", "Fewer than two usable prices", "#c0c7d0", "#303640"),
    "mixed": ("Mixed", "No other shape classification applies", "#c0c7d0", "#303640"),
    "scratching_break": ("Scratching break", "Runner removal may have adjusted the field's prices", "#f0cc78", "#44351c"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def file_age_seconds(path: Path) -> int | None:
    try:
        return int(utc_now().timestamp() - path.stat().st_mtime)
    except OSError:
        return None


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    minutes = minutes % 60
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days = hours // 24
    return f"{days}d"


def fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number) or number <= 0:
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
    if not math.isfinite(number):
        return "—"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.1f}%"


def fmt_money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}m"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return f"{number:.0f}"


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


def price_from_runner(runner: dict[str, Any]) -> float | None:
    for key in ("last_price_traded", "lastPriceTraded", "price"):
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
            value = first.get("price") if isinstance(first, dict) else None
            if value is not None:
                try:
                    number = float(value)
                    if math.isfinite(number) and number > 0:
                        return number
                except (TypeError, ValueError):
                    pass
    return None


def runner_key(runner: dict[str, Any]) -> str:
    selection_id = runner.get("selection_id") or runner.get("selectionId")
    if selection_id is not None:
        return f"sid:{selection_id}"
    cloth = runner.get("cloth_number") or runner.get("clothNumber") or ""
    name = runner.get("runner_name") or runner.get("runnerName") or ""
    return f"cloth:{cloth}:{name}"


def runner_no(runner: dict[str, Any]) -> str:
    value = runner.get("cloth_number") or runner.get("clothNumber") or ""
    return str(value)


def runner_name(runner: dict[str, Any]) -> str:
    return str(runner.get("runner_name") or runner.get("runnerName") or "")


def runner_selection_id(runner: dict[str, Any]) -> str:
    value = runner.get("selection_id") or runner.get("selectionId") or ""
    return str(value)


def find_history_dir(base_dir: Path, market_id: str, market_start_time: str | None = None) -> Path | None:
    history_dir = base_dir / HISTORY_DIR_NAME
    if market_start_time:
        dt = parse_time(market_start_time)
        if dt:
            candidate = history_dir / dt.strftime("%Y-%m-%d") / market_id
            if candidate.exists():
                return candidate
    matches = sorted(
        [path for path in history_dir.glob("*/*") if path.is_dir() and path.name == market_id],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def load_snapshot_prices(history_dir: Path | None) -> dict[str, dict[str, float]]:
    prices: dict[str, dict[str, float]] = {}
    if history_dir is None:
        return prices
    for stage, file_name in SNAPSHOT_FILES.items():
        snapshot = read_json(history_dir / file_name)
        if not snapshot:
            continue
        for runner in snapshot.get("runners", []):
            if not isinstance(runner, dict):
                continue
            key = runner_key(runner)
            price = price_from_runner(runner)
            if price is not None:
                prices.setdefault(key, {})[stage] = price
    return prices


def result_files(history_dir: Path | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if history_dir is None:
        return None, None
    return read_json(history_dir / "result.json"), read_json(history_dir / "closed.json")


def latest_snapshot(history_dir: Path | None) -> dict[str, Any] | None:
    """Return the latest available stored book for a historical race."""
    if history_dir is None:
        return None
    for stage in reversed(STAGES):
        snapshot = read_json(history_dir / SNAPSHOT_FILES[stage])
        if snapshot:
            return snapshot
    return None


def history_entry(race_dir: Path) -> dict[str, Any] | None:
    """Build a lightweight navigation entry for a completed race directory."""
    result = read_json(race_dir / "result.json")
    closed = read_json(race_dir / "closed.json")
    if not result and not closed:
        return None

    snapshot = latest_snapshot(race_dir) or {}
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    start_text = market.get("market_start_time")
    start_dt = parse_time(start_text)

    try:
        fallback_ts = race_dir.stat().st_mtime
    except OSError:
        fallback_ts = 0.0

    sort_ts = start_dt.timestamp() if start_dt else fallback_ts
    return {
        "market_id": race_dir.name,
        "track": str(market.get("track") or ""),
        "market_name": str(market.get("market_name") or ""),
        "market_start_time": start_text,
        "sort_ts": sort_ts,
        "path": race_dir,
    }


def scan_completed_history(base_dir: Path) -> list[dict[str, Any]]:
    """Return completed races oldest to newest for simple wall navigation."""
    history_root = base_dir / HISTORY_DIR_NAME
    entries: list[dict[str, Any]] = []
    if not history_root.is_dir():
        return entries

    for race_dir in history_root.glob("*/*"):
        if not race_dir.is_dir():
            continue
        entry = history_entry(race_dir)
        if entry:
            entries.append(entry)

    entries.sort(key=lambda item: (item["sort_ts"], item["market_id"]))
    return entries


class HistoryIndex:
    """Share lightweight entries across requests; never retain runner books."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.lock = Lock()
        self.expires_at = 0.0
        self.entries: list[dict[str, Any]] = []

    def get(self) -> list[dict[str, Any]]:
        with self.lock:
            if time.monotonic() >= self.expires_at:
                self.entries = scan_completed_history(self.base_dir)
                self.expires_at = time.monotonic() + HISTORY_CACHE_SECONDS
            return self.entries


@lru_cache(maxsize=4)
def history_index(base_dir: Path) -> HistoryIndex:
    return HistoryIndex(base_dir)


def completed_history(base_dir: Path) -> list[dict[str, Any]]:
    """Cached navigation, with archive changes visible within 60 seconds."""
    return history_index(base_dir.resolve()).get()


def history_navigation(base_dir: Path, market_id: str | None, live_market_id: str) -> dict[str, Any]:
    """Resolve previous/current/next links for live or historical views."""
    entries = completed_history(base_dir)
    by_id = {entry["market_id"]: index for index, entry in enumerate(entries)}

    if market_id is None:
        previous = next(
            (entry for entry in reversed(entries) if entry["market_id"] != live_market_id),
            None,
        )
        return {"previous": previous, "next": None, "is_live": True}

    index = by_id.get(market_id)
    if index is None:
        return {"previous": None, "next": None, "is_live": False}

    previous = entries[index - 1] if index > 0 else None
    if index + 1 < len(entries):
        next_entry: dict[str, Any] | None = entries[index + 1]
        if next_entry["market_id"] == live_market_id:
            next_entry = {"market_id": live_market_id, "live": True}
    else:
        next_entry = (
            {"market_id": live_market_id, "live": True}
            if live_market_id and live_market_id != market_id else None
        )

    return {"previous": previous, "next": next_entry, "is_live": False}


def winner_keys(result: dict[str, Any] | None) -> set[str]:
    if not result:
        return set()
    winners = result.get("winners")
    if not isinstance(winners, list):
        winner = result.get("winner")
        winners = [winner] if isinstance(winner, dict) else []
    keys = set()
    for runner in winners:
        if isinstance(runner, dict):
            keys.add(runner_key(runner))
    return keys


def pct_move(first: float | None, last: float | None) -> float | None:
    if not first or not last:
        return None
    return ((last - first) / first) * 100.0


def shape_for_prices(stage_prices: dict[str, float]) -> str:
    pieces: list[str] = []
    previous: float | None = None
    for stage in STAGES:
        current = stage_prices.get(stage)
        if previous is not None and current is not None:
            if current < previous:
                pieces.append("▼")
            elif current > previous:
                pieces.append("▲")
            else:
                pieces.append("▬")
        if current is not None:
            previous = current
    return "".join(pieces) or "—"


def shape_badge(row: dict[str, Any], live: bool) -> str:
    category = row["shape_class"]
    label, description, _, _ = SHAPE_STYLES.get(category, SHAPE_STYLES["mixed"])
    if category not in SHAPE_STYLES:
        category = "mixed"
    prices = row["prices"]
    stages = ", ".join("T−30s" if stage == "T-30" else stage.replace("-", "−") + "m"
                       for stage in STAGES if stage in prices) or "none"
    first, last = first_last(prices)
    suffix = " · so far" if live else ""
    tooltip = (f"{label}{suffix}: {description}. Available stages: {stages}. "
               f"First → last: {fmt_price(first)} → {fmt_price(last)}. "
               f"Net movement: {fmt_pct(row['move'])}.")
    return (f'<span class="shape-badge shape-{category}" tabindex="0" '
            f'title="{html.escape(tooltip, quote=True)}" aria-label="{html.escape(tooltip, quote=True)}">'
            f'{html.escape(row["shape"])} · {label}{suffix}</span>')


def shape_legend() -> str:
    badges = " ".join(
        f'<span class="shape-badge shape-{category}" title="{description}">{label}</span>'
        for category, (label, description, _, _) in SHAPE_STYLES.items() if category != "mixed"
    )
    return ('<details class="shape-legend"><summary>Shape color legend</summary>'
            f'<div class="strip">{badges}</div>'
            '<p class="small">Shapes describe the available price path. Move shows the net change. '
            'Live shapes are “so far”. Colors describe price direction, not a betting rating.</p></details>')


def first_last(stage_prices: dict[str, float]) -> tuple[float | None, float | None]:
    first = None
    last = None
    for stage in STAGES:
        value = stage_prices.get(stage)
        if value is None:
            continue
        if first is None:
            first = value
        last = value
    return first, last


def build_rows(active_book: dict[str, Any] | None, snapshot_prices: dict[str, dict[str, float]], result: dict[str, Any] | None) -> list[dict[str, Any]]:
    source_runners: list[dict[str, Any]] = []
    if active_book and isinstance(active_book.get("runners"), list):
        source_runners = [r for r in active_book["runners"] if isinstance(r, dict)]
    if not source_runners and result and isinstance(result.get("runners"), list):
        source_runners = [r for r in result["runners"] if isinstance(r, dict)]
    known = {runner_key(r) for r in source_runners}
    for key in sorted(snapshot_prices):
        if key not in known:
            source_runners.append({"selection_id": key.removeprefix("sid:"), "runner_name": "", "cloth_number": ""})
    winners = winner_keys(result)
    rows: list[dict[str, Any]] = []
    for runner in source_runners:
        key = runner_key(runner)
        prices = snapshot_prices.get(key, {})
        first, last = first_last(prices)
        move = pct_move(first, last) if len(prices) >= 2 else None
        rows.append(
            {
                "key": key,
                "winner": key in winners,
                "no": runner_no(runner),
                "name": runner_name(runner),
                "selection_id": runner_selection_id(runner),
                "status": runner.get("status") or "",
                "matched": matched_from_runner(runner),
                "prices": prices,
                "move": move,
                "shape": shape_for_prices(prices),
                "shape_class": shape_class(prices),
                "latest": last,
            }
        )
    rows.sort(key=lambda row: (row["latest"] is None, row["latest"] if row["latest"] is not None else 999999, row["no"], row["name"]))
    return rows


def seconds_to_start(market_start_time: Any) -> int | None:
    dt = parse_time(market_start_time)
    if not dt:
        return None
    return int((dt - utc_now()).total_seconds())


def fmt_tminus(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    return f"{sign}{minutes}m{sec:02d}s"


def status_chip(label: str, good: bool | None = None) -> str:
    cls = "chip"
    if good is True:
        cls += " good"
    elif good is False:
        cls += " warn"
    return f'<span class="{cls}">{html.escape(label)}</span>'


def latest_market_from_state(target: dict[str, Any], active_book: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(target.get("market"), dict):
        return target["market"]
    if active_book and isinstance(active_book.get("market"), dict):
        return active_book["market"]
    return {}


def html_page(base_dir: Path, requested_market_id: str | None = None,
              blackbook_only: bool = False, sort_by: str = "price") -> str:
    state_dir = base_dir / STATE_DIR_NAME

    target_path = state_dir / "betfair_t15_target.json"
    active_book_path = state_dir / "active_market_book.json"
    watch_status_path = state_dir / "betfair_watch_status.json"
    result_status_path = state_dir / "betfair_result_watch_status.json"
    interest_alert_path = state_dir / "betfair_interest_alert.json"

    target = read_json(target_path) or {}
    active_book = read_json(active_book_path)
    watch_status = read_json(watch_status_path) or {}
    result_watch_status = read_json(result_status_path) or {}
    interest_alert = read_json(interest_alert_path) or {}

    live_market = latest_market_from_state(target, active_book)
    live_market_id = str(live_market.get("market_id") or "")
    is_live_view = requested_market_id is None

    if is_live_view:
        market = live_market
        market_id = live_market_id
        market_start_time = market.get("market_start_time")
        history_dir = find_history_dir(
            base_dir,
            market_id,
            str(market_start_time) if market_start_time else None,
        ) if market_id else None
        display_book = active_book
        lifecycle = load_lifecycle(base_dir, market)
        observed_book = lifecycle.get('latest_book')
        received = parse_time(lifecycle.get('last_received_at'))
        if (isinstance(observed_book, dict) and received and not lifecycle.get('feed_error')
                and 0 <= (utc_now() - received).total_seconds() <= observation_policy(base_dir)['fresh_seconds']
                and lifecycle.get('state') in {'preplay', 'delayed', 'suspended'}):
            display_book = observed_book
            market = {**market, **observed_book.get('market', {})}
    else:
        market_id = str(requested_market_id or "")
        history_dir = find_history_dir(base_dir, market_id) if market_id else None
        display_book = latest_snapshot(history_dir)
        market = (
            display_book.get("market")
            if display_book and isinstance(display_book.get("market"), dict)
            else {}
        )
        market_start_time = market.get("market_start_time")

    market_matched = matched_from_runner(market)
    if market_matched is None and display_book:
        market_matched = matched_from_runner(display_book)

    snapshot_prices = load_snapshot_prices(history_dir)
    result, closed = result_files(history_dir)
    rows = build_rows(display_book, snapshot_prices, result)
    if history_dir and scratching_break([read_json(history_dir / file_name) for file_name in SNAPSHOT_FILES.values()]):
        for row in rows:
            row['shape_class'], row['move'] = 'scratching_break', None
    blackbook = enrich_rows(rows, {**market, "market_id": market_id}, base_dir)
    if blackbook_only:
        rows = [row for row in rows if row['blackbook']['w'] or row['blackbook']['tags']]
    if sort_by == "td":
        rows.sort(key=lambda row: -(row['blackbook']['td'] or 0))
    navigation = history_navigation(base_dir, requested_market_id, live_market_id)

    tminus = seconds_to_start(market_start_time) if is_live_view else None
    closed_ok = bool(closed and closed.get("complete") and closed.get("has_result"))

    slots = "\n".join(
        status_chip(stage, history_dir is not None and (history_dir / file_name).exists())
        for stage, file_name in SNAPSHOT_FILES.items()
    )

    target_age = fmt_age(file_age_seconds(target_path))
    active_age = fmt_age(file_age_seconds(active_book_path))
    watch_age = fmt_age(file_age_seconds(watch_status_path))
    result_age = fmt_age(file_age_seconds(result_status_path))
    interest_age = fmt_age(file_age_seconds(interest_alert_path))

    title_bits = [str(market.get("track") or "ToteBot"), str(market.get("market_name") or "Watch Wall")]
    title = " · ".join([bit for bit in title_bits if bit])

    winner_label = "Pending"
    if result and isinstance(result.get("winner"), dict):
        winner = result["winner"]
        winner_label = f'{winner.get("cloth_number") or ""} {winner.get("runner_name") or ""}'.strip()

    rows_html = []
    for row in rows:
        cls = "winner" if row["winner"] else ""
        name = row["name"] or row["selection_id"]
        if len(name) > 28:
            name = name[:27] + "…"
        price_cells = "".join(f"<td>{html.escape(fmt_price(row['prices'].get(stage)))}</td>" for stage in STAGES)
        rows_html.append(
            f"""
            <tr class="{cls}">
              <td class="num">{html.escape(str(row['no']))}</td>
              <td class="horse">{html.escape(name)}</td>
              <td class="blackbook">{blackbook_cell(row['blackbook'])}</td>
              {price_cells}
              <td>{html.escape(fmt_pct(row['move']))}</td>
              <td class="shape">{shape_badge(row, is_live_view)}</td>
              <td>{html.escape(fmt_money(row['matched']))}</td>
            </tr>
            """
        )

    if not rows_html:
        empty = 'No matching blackbook runners.' if blackbook_only else 'No runner snapshot available yet.'
        rows_html.append(f'<tr><td colspan="11" class="muted centre">{empty}</td></tr>')

    interest_line = "—"
    if interest_alert:
        status = interest_alert.get("status") or "alert"
        reason = interest_alert.get("reason") or ""
        alert_market = interest_alert.get("market") if isinstance(interest_alert.get("market"), dict) else {}
        alert_track = alert_market.get("track") or ""
        alert_race = alert_market.get("market_name") or ""
        interest_line = f"{status}: {alert_track} {alert_race} {reason}".strip()

    capture_line = str(watch_status.get("status") or "—")
    if result_watch_status:
        result_line = (
            f'checked={result_watch_status.get("checked", "—")} '
            f'resolved={result_watch_status.get("resolved", "—")} '
            f'errors={len(result_watch_status.get("errors", []) or [])}'
        )
    else:
        result_line = "—"

    status_chips = [
        status_chip(f"target {target_age}", target_age != "—"),
        status_chip(f"book {active_age}", active_age != "—"),
        status_chip(f"watch {watch_age}", watch_age != "—"),
        status_chip(f"results {result_age}", result_age != "—"),
        status_chip(f"interest {interest_age}", interest_age != "—"),
        status_chip("closed" if closed_ok else "open", True if closed_ok else None),
    ]

    def nav_link(entry: dict[str, Any] | None, label: str, disabled_label: str) -> str:
        if not entry:
            return f'<span class="navbtn disabled">{html.escape(disabled_label)}</span>'
        if entry.get("live"):
            return f'<a class="navbtn" href="/">{html.escape(label)}</a>'
        target_id = str(entry.get("market_id") or "")
        if not target_id:
            return f'<span class="navbtn disabled">{html.escape(disabled_label)}</span>'
        return f'<a class="navbtn" href="/race/{html.escape(target_id)}">{html.escape(label)}</a>'

    previous_link = nav_link(navigation.get("previous"), "← Previous", "← Previous")
    next_link = nav_link(navigation.get("next"), "Next →", "Next →")
    current_link = (
        '<span class="navbtn current disabled">● Current</span>'
        if is_live_view
        else '<a class="navbtn current" href="/">● Current</a>'
    )
    view_badge = "● LIVE" if is_live_view else "FINAL"
    view_badge_class = "viewbadge live" if is_live_view else "viewbadge final"

    generated = utc_now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    blackbook_age = fmt_age(file_age_seconds(state_dir / 'tb_blackbook.json'))
    blackbook_label = f'Register updated {blackbook_age} ago' if blackbook else 'Register unavailable'
    volume_book = display_book if display_book and str((display_book.get('market') or {}).get('market_id')) == market_id else None
    volume_html = volume_panel(base_dir, volume_book, history_dir, live=is_live_view)
    observation_html = observation_panel(base_dir, {**market, 'market_id': market_id}, live=is_live_view)
    observation_label = timing_label(base_dir, {**market, 'market_id': market_id}) if is_live_view else None
    shape_css = "\n".join(
        f'.shape-badge.shape-{category} {{ color:{foreground}; background:{background}; }}'
        for category, (_, _, foreground, background) in SHAPE_STYLES.items()
    )

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  {'<meta http-equiv="refresh" content="15">' if is_live_view else ''}
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ToteBot Mini Wall</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b0f14; --panel:#121923; --panel2:#172131; --text:#e9eef5; --muted:#95a3b8; --line:#263447; --good:#1f7a43; --warn:#9a6a18; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.35; }}
    main {{ max-width:1180px; margin:0 auto; padding:16px; }}
    .top {{ display:flex; gap:12px; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; margin-bottom:12px; }}
    h1 {{ margin:0; font-size:clamp(1.3rem,3vw,2.1rem); letter-spacing:.01em; }}
    .sub {{ color:var(--muted); margin-top:4px; font-size:.95rem; }}
    .tminus {{ font-size:clamp(1.4rem,4vw,2.5rem); font-weight:800; text-align:right; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:12px; min-height:84px; }}
    .label {{ color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.08em; margin-bottom:6px; }}
    .value {{ font-size:1rem; font-weight:650; overflow-wrap:anywhere; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .chip {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:4px 8px; color:var(--muted); font-size:.82rem; background:var(--panel2); white-space:nowrap; }}
    .chip.good {{ color:#dff7e8; border-color:var(--good); background:rgba(31,122,67,.35); }}
    .chip.warn {{ color:#ffe8b8; border-color:var(--warn); background:rgba(154,106,24,.35); }}
    table {{ width:100%; border-collapse:collapse; overflow:hidden; border-radius:14px; background:var(--panel); border:1px solid var(--line); font-variant-numeric:tabular-nums; }}
    th,td {{ padding:8px 9px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }}
    th {{ color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.06em; background:var(--panel2); }}
    td.horse,th.horse {{ text-align:left; max-width:310px; overflow:hidden; text-overflow:ellipsis; }}
    td.num,th.num {{ text-align:left; width:52px; }}
    .table-scroll {{ overflow-x:auto; }}
    td.blackbook {{ text-align:left; white-space:normal; min-width:210px; max-width:420px; }}
    .blackbook summary {{ cursor:pointer; white-space:nowrap; }}
    .blackbook a {{ color:#9fcaff; }}
    .blackbook li {{ margin:8px 0; }}
    .filters {{ display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin:12px 0; }}
    .volume-panel {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px; margin:12px 0; }}
    .volume-panel h2 {{ margin:0; font-size:1.15rem; }}
    .volume-bar {{ margin:16px 0; }}
    .volume-bar p {{ margin:6px 0; }}
    .volume-track {{ position:relative; height:18px; background:#263447; border-radius:6px; overflow:hidden; }}
    .volume-fill {{ display:block; height:100%; }}
    .volume-fill.amber {{ background:#d6a340; }}
    .volume-fill.blue {{ background:#529de0; }}
    .volume-fill.green {{ background:#50bb85; }}
    .volume-fill.grey {{ background:#8793a3; }}
    .volume-median {{ position:absolute; top:0; bottom:0; width:2px; background:#fff; }}
    .volume-trend {{ width:100%; max-width:600px; display:block; }}
    .volume-trend text {{ fill:var(--muted); font-size:12px; }}
    .winner td {{ background:rgba(31,122,67,.20); font-weight:750; }}
    .shape {{ letter-spacing:.06em; }}
    .shape-badge {{ display:inline-block; border:1px solid currentColor; border-radius:7px; padding:4px 7px; font-size:.8rem; letter-spacing:normal; white-space:nowrap; }}
    .shape-badge:focus-visible {{ outline:2px solid #fff; outline-offset:3px; }}
    .shape-legend {{ margin:10px 0; }}
    .shape-legend summary {{ cursor:pointer; }}
    {shape_css}
    .muted {{ color:var(--muted); }} .centre {{ text-align:center; }}
    .strip {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
    .small {{ font-size:.86rem; color:var(--muted); }}
    .nav {{ display:flex; gap:8px; align-items:center; justify-content:center; flex-wrap:wrap; margin:0 0 12px; }}
    .navbtn {{ display:inline-block; color:var(--text); text-decoration:none; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:8px 12px; font-weight:700; }}
    .navbtn:hover {{ background:var(--panel2); }}
    .navbtn.current {{ min-width:110px; text-align:center; }}
    .navbtn.disabled {{ color:var(--muted); opacity:.55; cursor:default; }}
    .viewbadge {{ display:inline-block; margin-left:8px; border-radius:999px; padding:3px 8px; font-size:.72rem; font-weight:800; letter-spacing:.08em; vertical-align:middle; }}
    .viewbadge.live {{ background:rgba(31,122,67,.35); border:1px solid var(--good); }}
    .viewbadge.final {{ background:var(--panel2); border:1px solid var(--line); color:var(--muted); }}
    @media (max-width:760px) {{ main {{ padding:10px; }} .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} table {{ font-size:.86rem; }} th,td {{ padding:6px 5px; }} .hide-small {{ display:none; }} td.horse,th.horse {{ max-width:150px; }} .tminus {{ text-align:left; }} }}
  </style>
</head>
<body>
<main>
  <nav class="nav">{previous_link}{current_link}{next_link}</nav>
  <section class="top">
    <div>
      <h1>🏇 {html.escape(title)} <span class="{view_badge_class}">{view_badge}</span></h1>
      <div class="sub">Market {html.escape(market_id or "—")} · Start {html.escape(str(market_start_time or "—"))}</div>
      <div class="strip">{" ".join(status_chips)}</div>
    </div>
    <div class="tminus">{html.escape(observation_label) if observation_label else ('T ' + html.escape(fmt_tminus(tminus))) if is_live_view else 'FINAL'}</div>
  </section>

  <section class="grid">
    <div class="card"><div class="label">Track</div><div class="value">{html.escape(str(market.get("track") or "—"))}</div></div>
    <div class="card"><div class="label">Race</div><div class="value">{html.escape(str(market.get("market_name") or "—"))}</div></div>
    <div class="card"><div class="label">Winner</div><div class="value">{html.escape(winner_label)}</div></div>
    <div class="card"><div class="label">Slots</div><div class="chips">{slots}</div></div>
  </section>

  <p class="small">Blackbook: {html.escape(blackbook_label)}. Recorded wins before this race: W = all, T = track, D = exact distance, T+D = both. ★ = tagged. — = unknown. Manual metadata is current.</p>
  <form class="filters" method="get">
    <label><input type="checkbox" name="blackbook" value="1" {'checked' if blackbook_only else ''}> Blackbook runners only</label>
    <label>Sort <select name="sort"><option value="price" {'selected' if sort_by != 'td' else ''}>Price</option><option value="td" {'selected' if sort_by == 'td' else ''}>Track + distance wins</option></select></label>
    <button type="submit">Apply</button>
  </form>
  <div class="table-scroll"><table>
    <thead><tr><th class="num">No</th><th class="horse">Horse</th><th>Blackbook</th><th>T15</th><th>T10</th><th>T5</th><th>T2</th><th>T30</th><th>Move</th><th>Shape</th><th>Matched</th></tr></thead>
    <tbody>{"".join(rows_html)}</tbody>
  </table></div>
  {shape_legend()}
  {observation_html}

  <p class="small">Market matched: {html.escape(fmt_money(market_matched))} · Runner matched amounts above are as reported; — means unavailable.</p>
  {volume_html}

  <section class="grid" style="margin-top:12px;">
    <div class="card"><div class="label">Capture watcher</div><div class="value">{html.escape(capture_line)}</div></div>
    <div class="card"><div class="label">Result watcher</div><div class="value">{html.escape(result_line)}</div></div>
    <div class="card"><div class="label">Interest alert</div><div class="value">{html.escape(interest_line)}</div></div>
    <div class="card"><div class="label">Generated</div><div class="value">{html.escape(generated)}</div></div>
  </section>

  <p class="small">Read-only mini wall v{VERSION}. {('Live view auto-refreshes every 15 seconds.' if is_live_view else 'Historical view is fixed; use Current to return live.')} Base directory: {html.escape(str(base_dir))}</p>
</main>
</body>
</html>
'''


def json_status(base_dir: Path) -> dict[str, Any]:
    state_dir = base_dir / STATE_DIR_NAME
    history_dir = base_dir / HISTORY_DIR_NAME
    target_path = state_dir / "betfair_t15_target.json"
    active_book_path = state_dir / "active_market_book.json"
    target = read_json(target_path) or {}
    active_book = read_json(active_book_path) or {}
    market = latest_market_from_state(target, active_book)
    return {
        "ok": True,
        "version": VERSION,
        "base_dir": str(base_dir),
        "state_dir": str(state_dir),
        "history_dir": str(history_dir),
        "target_age_seconds": file_age_seconds(target_path),
        "active_book_age_seconds": file_age_seconds(active_book_path),
        "target_status": target.get("status"),
        "market": market,
        "generated_at": utc_now().isoformat(),
    }


class WallHandler(BaseHTTPRequestHandler):
    base_dir: Path = DEFAULT_BASE_DIR

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_text(self, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        options = {'blackbook_only': query.get('blackbook') == ['1'],
                   'sort_by': 'td' if query.get('sort') == ['td'] else 'price'}
        if path in ("/", "/wall", "/current"):
            self.send_text(html_page(self.base_dir, **options), "text/html; charset=utf-8")
            return
        if path.startswith("/race/"):
            market_id = path.removeprefix("/race/").strip()
            history_dir = find_history_dir(self.base_dir, market_id) if market_id else None
            if not history_dir:
                self.send_text("race not found\n", status=404)
                return
            self.send_text(
                html_page(self.base_dir, requested_market_id=market_id, **options),
                "text/html; charset=utf-8",
            )
            return
        if path == "/prev":
            market = json_status(self.base_dir)["market"]
            nav = history_navigation(self.base_dir, None, str(market.get("market_id") or ""))
            previous = nav.get("previous")
            if previous and previous.get("market_id"):
                self.send_response(302)
                self.send_header("Location", f"/race/{previous['market_id']}")
                self.end_headers()
            else:
                self.send_text("no previous completed race\n", status=404)
            return
        if path == "/health":
            self.send_text(json.dumps(json_status(self.base_dir), indent=2, sort_keys=True) + "\n", "application/json; charset=utf-8")
            return
        if path == "/latest":
            state_dir = self.base_dir / STATE_DIR_NAME
            payload = {
                "target": read_json(state_dir / "betfair_t15_target.json"),
                "active_market_book": read_json(state_dir / "active_market_book.json"),
                "interest_alert": read_json(state_dir / "betfair_interest_alert.json"),
                "result_watch_status": read_json(state_dir / "betfair_result_watch_status.json"),
                "generated_at": utc_now().isoformat(),
            }
            self.send_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "application/json; charset=utf-8")
            return
        self.send_text("not found\n", status=404)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tiny read-only ToteBot HTTP wall.")
    parser.add_argument("--version", action="version", version=f"ToteBot Mini Wall {VERSION}")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    base_dir = args.base_dir.expanduser()
    WallHandler.base_dir = base_dir
    server = ThreadingHTTPServer((args.host, args.port), WallHandler)
    print(f"ToteBot mini wall v{VERSION}: http://{args.host}:{args.port}")
    print(f"Reading: {base_dir}")
    print("Read-only. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
