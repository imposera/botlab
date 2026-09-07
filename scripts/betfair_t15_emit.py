#!/usr/bin/env python3
"""
Betfair T-15 race emitter.

Basecamp-only, read-only Betfair observer.

Purpose:
- Search worldwide Horse Racing WIN markets.
- Select the earliest market inside the T-15 arming window.
- Emit a stable state file for ToteBot or another watcher.
- Never print/store session tokens or credentials.
- Never place bets or call betting/order endpoints.

Files written into ~/botlab/state by default:

1. betfair_t15_target.json
   The machine-consumable event contract.
   Consumers should trigger only when:
       status == "armed"
       arm_id has not been seen before
       seconds_to_jump > 0 (initial arm only; a held observer may run past start)

2. betfair_watch_status.json
   Human/debug status on every run.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tb_race_lifecycle import evaluate, load_lifecycle, policy

from betfair_gateway import (
    HORSE_RACING_EVENT_TYPE_ID,
    betfair_login,
    betting_api,
    iso_z,
    load_secrets,
    market_summary,
    utc_now,
)


from manual_arm_request import (
    consume_request,
    pending_request,
    reject_request,
)

DEFAULT_STATE_DIR = Path.home() / "botlab" / "totebot" / "state"
DEFAULT_CONFIG_DIR = Path.home() / "botlab" / "totebot" / "config"

TARGET_FILE_NAME = "betfair_t15_target.json"
STATUS_FILE_NAME = "betfair_watch_status.json"
PRIORITY_CONFIG_FILE_NAME = "race_priority.json"


DEFAULT_SEARCH_HOURS = 4
DEFAULT_TARGET_MINUTES = 15
DEFAULT_WINDOW_MINUTES = 3
VERSION = "1.2.1"



# Early ToteBot scope: thoroughbred WIN markets only.
# Exclude common harness/trots wording found in Betfair event/market names.
HARNESS_TERMS = (
    "trot",
    "trotting",
    "trotters",
    "harness",
    "pace",
    "pacing",
    "mobile pace",
)

HARNESS_TRACKS = ( "globe derby", )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically so a watcher never sees half a JSON document."""
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
    """Read prior state safely; malformed/missing state is treated as absent."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def parse_betfair_time(value: str) -> datetime:
    """Parse Betfair's UTC market-start timestamp."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds_to_start(market_start_time: str) -> int:
    start = parse_betfair_time(market_start_time)
    return int((start - utc_now()).total_seconds())

def is_harness_market(market: dict[str, Any]) -> bool:
    """
    Return True for likely harness/trots markets.

    This is intentionally conservative and uses visible Betfair catalogue
    naming only. It keeps the first ToteBot phase focused on thoroughbreds.
    """
    text = " ".join(
        str(market.get(field) or "")
        for field in (
            "track",
            "event_name",
            "market_name",
        )
    ).lower()

    return (
        any(term in text for term in HARNESS_TERMS)
        or any(track in text for track in HARNESS_TRACKS)
    )

def discover_markets(search_hours: int) -> list[dict[str, Any]]:
    """
    Fetch upcoming worldwide Horse Racing WIN markets.

    Returns sanitized market catalogues sorted by actual Betfair start time.
    """
    now = utc_now()
    until = now + timedelta(hours=search_hours)

    session_token = betfair_login()

    catalogue = betting_api(
        "listMarketCatalogue",
        {
            "filter": {
                "eventTypeIds": [HORSE_RACING_EVENT_TYPE_ID],
                "marketTypeCodes": ["WIN"],
                "marketStartTime": {
                    "from": iso_z(now),
                    "to": iso_z(until),
                },
            },
            "marketProjection": [
                "EVENT",
                "MARKET_START_TIME",
                "RUNNER_DESCRIPTION",
                "RUNNER_METADATA",
            ],
            "sort": "FIRST_TO_START",
            "maxResults": 100,
        },
        session_token,
    )

    markets = [
        market_summary(item)
        for item in catalogue
        if item.get("marketId") and item.get("marketStartTime")
    ]

    # Keep the early observer focused on gallops/thoroughbreds.
    markets = [market for market in markets if not is_harness_market(market)]

    markets.sort(key=lambda item: item["market_start_time"])
    return markets

def find_t15_candidate(
    markets: list[dict[str, Any]],
    target_minutes: int,
    window_minutes: int,
    priority_config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Select a market inside the T-15 arming window.

    Default behaviour is the original earliest-in-window choice.
    If race_priority.json is enabled, choose the best scored candidate inside
    the current arming window and include an explanation for status output.
    """
    target_seconds = target_minutes * 60
    tolerance_seconds = window_minutes * 60

    lower_bound = target_seconds - tolerance_seconds
    upper_bound = target_seconds + tolerance_seconds

    candidates: list[dict[str, Any]] = []

    for market in markets:
        seconds_remaining = seconds_to_start(market["market_start_time"])

        if lower_bound <= seconds_remaining <= upper_bound:
            candidates.append(
                {
                    **market,
                    "seconds_to_jump": seconds_remaining,
                    "minutes_to_jump": round(seconds_remaining / 60, 1),
                }
            )

    priority_enabled = bool(priority_config and priority_config.get("enabled"))

    if not candidates:
        return None, {
            "selection_mode": "priority" if priority_enabled else "earliest",
            "priority_enabled": priority_enabled,
            "selection_reason": "no_market_inside_t15_arming_window",
            "candidates_considered": [],
        }

    if priority_enabled:
        chosen_market, debug = select_market_by_priority(
            candidates,
            priority_config or {},
        )

        if chosen_market is not None:
            return chosen_market, debug

        # Safety fallback: if scoring excludes everything unexpectedly, keep
        # the old behaviour rather than leaving the watcher idle.
        candidates.sort(key=lambda item: item["seconds_to_jump"])
        return candidates[0], {
            "selection_mode": "priority",
            "priority_enabled": True,
            "selection_reason": "priority_no_scored_candidate_fallback_earliest",
            "chosen_market_id": candidates[0].get("market_id"),
            "chosen_race": f"{candidates[0].get('track', '')} {candidates[0].get('market_name', '')}".strip(),
            "chosen_seconds_to_jump": candidates[0].get("seconds_to_jump"),
            "candidates_considered": [],
        }

    candidates.sort(key=lambda item: item["seconds_to_jump"])
    chosen = candidates[0]
    return chosen, {
        "selection_mode": "earliest",
        "priority_enabled": False,
        "selection_reason": "earliest_market_inside_t15_arming_window",
        "chosen_market_id": chosen.get("market_id"),
        "chosen_race": f"{chosen.get('track', '')} {chosen.get('market_name', '')}".strip(),
        "chosen_seconds_to_jump": chosen.get("seconds_to_jump"),
        "candidates_considered": [
            {
                "market_id": item.get("market_id"),
                "race": f"{item.get('track', '')} {item.get('market_name', '')}".strip(),
                "seconds_to_jump": item.get("seconds_to_jump"),
            }
            for item in candidates[:10]
        ],
    }

def next_future_market(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the next upcoming market, useful for status/debug output."""
    for market in markets:
        seconds_remaining = seconds_to_start(market["market_start_time"])

        if seconds_remaining > 0:
            return {
                **market,
                "seconds_to_jump": seconds_remaining,
                "minutes_to_jump": round(seconds_remaining / 60, 1),
            }

    return None


def market_from_existing_target(
    existing_target: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not existing_target:
        return None

    market = existing_target.get("market")
    return market if isinstance(market, dict) else None


def existing_target_is_active(existing_target: dict[str, Any] | None, state_dir: Path | None = None) -> bool:
    """
    Hold through delays using fresh observations, with bounded stale/max waits.

    This prevents the emitter replacing a target every minute while ToteBot
    is still observing the chosen race.
    """
    if not existing_target:
        return False

    if existing_target.get("status") != "armed":
        return False

    market = market_from_existing_target(existing_target)
    if not market or not market.get("market_start_time"):
        return False

    base = (state_dir or DEFAULT_STATE_DIR).parent
    return evaluate(market, load_lifecycle(base, market), utc_now(), policy(base))["hold"]


def find_market_by_id(
    markets: list[dict[str, Any]], market_id: str,
) -> dict[str, Any] | None:
    for market in markets:
        if str(market.get("market_id")) == market_id:
            seconds = market_seconds_to_jump(market)
            return {**market, "seconds_to_jump": seconds,
                    "minutes_to_jump": round(seconds / 60, 1) if seconds is not None else None}
    return None


def with_runner_matched(market: dict[str, Any]) -> dict[str, Any]:
    """Enrich only the selected market; volume failures must not stop arming.

    EX_TRADED requests runner volumes, which catalogue discovery cannot supply.
    Missing data stays null; it is never replaced with a fabricated zero.
    """
    result = {**market}
    runners = [dict(r) for r in market.get("runners", []) if isinstance(r, dict)]
    for runner in runners:
        runner.update(total_matched=None, traded_levels=[])
    result.update(runners=runners, total_matched=None,
                  matched_data_status="unavailable", matched_updated_at=None)
    try:
        books = betting_api(
            "listMarketBook",
            {"marketIds": [str(market["market_id"])],
             "priceProjection": {"priceData": ["EX_TRADED"]}},
            betfair_login(),
        )
        book = next((b for b in books if str(b.get("marketId")) == str(market["market_id"])), None)
        if book is None:
            return result
        by_id = {str(r.get("selection_id", r.get("selectionId"))): r for r in runners}
        for live_runner in book.get("runners", []):
            selection_id = live_runner.get("selectionId")
            runner = by_id.get(str(selection_id))
            if runner is None:
                runner = {"selection_id": selection_id}
                runners.append(runner)
            levels = (live_runner.get("ex") or {}).get("tradedVolume") or []
            total = live_runner.get("totalMatched")
            if total is None and levels:
                total = sum(level["size"] for level in levels)
            runner.update(total_matched=total, traded_levels=levels)
        result.update(total_matched=book.get("totalMatched"),
                      is_market_data_delayed=book.get("isMarketDataDelayed"),
                      matched_data_status="available", matched_updated_at=iso_z(utc_now()))
        return result
    except Exception:
        # Do not leak gateway errors or credentials into the target contract.
        for runner in runners:
            runner.update(total_matched=None, traded_levels=[])
        return result


def build_armed_target(
    candidate: dict[str, Any],
    target_minutes: int,
    window_minutes: int,
    previous_target: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Build the machine-consumable event file.

    arm_id remains stable for the market so external watchers can dedupe.
    emitted_at remains stable when this is an update of the same target.
    """
    candidate = with_runner_matched(candidate)
    previous_market = market_from_existing_target(previous_target)
    same_market = (
        previous_market is not None
        and previous_market.get("market_id") == candidate.get("market_id")
    )

    emitted_at = (
        previous_target.get("emitted_at")
        if same_market and previous_target
        else iso_z(utc_now())
    )

    market_id = str(candidate["market_id"])

    return {
        "schema": "betfair_t15_target/v1",
        "version": VERSION,
        "watcher": "basecamp-betfair-t15-emitter",
        "mode": "read-only",
        "ok": True,
        "status": "armed",
        "event_type": "race_arm",
        "arm_id": f"betfair:{market_id}",
        "emitted_at": emitted_at,
        "updated_at": iso_z(utc_now()),
        "target_minutes_before_jump": target_minutes,
        "window_minutes": window_minutes,
        "consume": {
            "trigger_when": (
                "status == 'armed' AND arm_id has not been seen "
                "AND market.seconds_to_jump > 0"
            ),
            "dedupe_key": f"betfair:{market_id}",
        },
        "market": candidate,
    }


def build_expired_target(existing_target: dict[str, Any], reason: str = "observation_ended") -> dict[str, Any]:
    """Release a target after confirmed start, closure, or bounded timeout."""
    market = market_from_existing_target(existing_target) or {}

    return {
        **existing_target,
        "status": "expired",
        "event_type": "race_expired",
        "updated_at": iso_z(utc_now()),
        "reason": reason,
        "market": {
            **market,
            "seconds_to_jump": (
                seconds_to_start(market["market_start_time"])
                if market.get("market_start_time")
                else None
            ),
        },
    }


def build_status(
    *,
    search_hours: int,
    target_minutes: int,
    window_minutes: int,
    status: str,
    reason: str,
    next_market: dict[str, Any] | None,
    active_target: dict[str, Any] | None,
    selection_debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "betfair_watch_status/v1",
        "watcher": "basecamp-betfair-t15-emitter",
        "mode": "read-only",
        "ok": True,
        "updated_at": iso_z(utc_now()),
        "selection": selection_debug or {
            "selection_mode": "earliest",
            "priority_enabled": False,
        },
        "status": status,
        "reason": reason,
        "search_horizon_hours": search_hours,
        "target_minutes_before_jump": target_minutes,
        "window_minutes": window_minutes,
        "next_market": next_market,
        "active_target_market_id": (
            active_target.get("market", {}).get("market_id")
            if active_target
            else None
        ),
    }

def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def default_priority_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "selection_mode": "earliest",
        "lookahead_minutes": 30,
        "minimum_seconds_to_jump": 120,
        "maximum_seconds_to_jump": 1800,
        "prefer_earlier_if_score_within": 15,

        # Observer-first arming controls. Disabled by default so legacy
        # behaviour remains unchanged until explicitly enabled.
        "hold_for_better_candidate": False,
        "hold_min_score_delta": 30,
        "rearm_enabled": False,
        "rearm_min_score_delta": 30,
        "rearm_min_seconds_to_jump": 720,
        "hard_lock_snapshot": "market_book_t15.json",

        # Machine-readable Track Review prior.
        "track_liquidity_enabled": True,
        "track_liquidity_file": "track_liquidity.json",

        "country_scores": {},
        "track_scores": {},
        "weekday_scores": {},
        "race_name_scores": {},
        "exclude_terms": [],
    }


def load_priority_config(config_dir: Path) -> dict[str, Any]:
    config = default_priority_config()
    user_config = read_json_file(config_dir / PRIORITY_CONFIG_FILE_NAME)

    if user_config:
        config.update(user_config)

    return config


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def lower_market_text(market: dict[str, Any]) -> str:
    parts = [
        market.get("track"),
        market.get("event_name"),
        market.get("market_name"),
        market.get("country_code"),
    ]
    return " ".join(norm_text(part).lower() for part in parts)


def market_seconds_to_jump(market: dict[str, Any]) -> int | None:
    start_text = market.get("market_start_time")
    if not isinstance(start_text, str):
        return None

    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    except ValueError:
        return None

    return int((start - utc_now()).total_seconds())


def market_weekday_name(market: dict[str, Any]) -> str:
    start_text = market.get("market_start_time")
    if not isinstance(start_text, str):
        return ""

    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    except ValueError:
        return ""

    return start.strftime("%A")


def priority_excluded(market: dict[str, Any], config: dict[str, Any]) -> str | None:
    text = lower_market_text(market)

    for term in config.get("exclude_terms", []):
        term_text = str(term).strip().lower()
        if term_text and term_text in text:
            return f"excluded_term:{term}"

    return None


def load_track_liquidity(
    state_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Load Track Review liquidity state if coaching is enabled."""
    if not config.get("track_liquidity_enabled", True):
        return {}

    file_name = str(config.get("track_liquidity_file") or "track_liquidity.json")
    payload = read_json_file(state_dir / file_name)
    if not payload:
        return {}

    tracks = payload.get("tracks")
    return tracks if isinstance(tracks, dict) else {}


def historical_liquidity_score(liquidity_band: str, confidence: str) -> int:
    """Conservative historical arming prior from Track Review."""
    scores = {
        ("PREMIUM", "STRONG"): 40,
        ("PREMIUM", "USABLE"): 30,
        ("PREMIUM", "DISCOVERY"): 15,
        ("STRONG", "STRONG"): 30,
        ("STRONG", "USABLE"): 22,
        ("STRONG", "DISCOVERY"): 10,
        ("USABLE", "STRONG"): 18,
        ("USABLE", "USABLE"): 12,
        ("USABLE", "DISCOVERY"): 5,
        ("THIN", "STRONG"): -20,
        ("THIN", "USABLE"): -12,
        ("THIN", "DISCOVERY"): -5,
    }
    return scores.get(
        (
            str(liquidity_band or "UNKNOWN").upper(),
            str(confidence or "DISCOVERY").upper(),
        ),
        0,
    )


def track_liquidity_for_market(
    market: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> dict[str, Any]:
    """Case-insensitive lookup by Betfair track name."""
    wanted = norm_text(market.get("track")).casefold()
    for track_name, stats in liquidity_lookup.items():
        if str(track_name).strip().casefold() == wanted and isinstance(stats, dict):
            return stats
    return {}


def history_dir_for_market(
    state_dir: Path,
    market: dict[str, Any],
) -> Path | None:
    """Locate history/YYYY-MM-DD/<market_id> for the active market."""
    market_id = norm_text(market.get("market_id"))
    if not market_id:
        return None

    history_root = state_dir.parent / "history"
    start_text = market.get("market_start_time")

    if isinstance(start_text, str):
        try:
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            candidate = history_root / start.strftime("%Y-%m-%d") / market_id
            if candidate.exists():
                return candidate
        except ValueError:
            pass

    matches = sorted(
        (
            path for path in history_root.glob("*/*")
            if path.is_dir() and path.name == market_id
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def active_target_is_hard_locked(
    state_dir: Path,
    active_market: dict[str, Any],
    config: dict[str, Any],
) -> tuple[bool, str]:
    """Lock the race after the first meaningful snapshot (T15 by default)."""
    snapshot_name = str(config.get("hard_lock_snapshot") or "market_book_t15.json")
    race_dir = history_dir_for_market(state_dir, active_market)

    if race_dir is None:
        return False, "no_history_dir_yet"

    if (race_dir / snapshot_name).exists():
        return True, f"hard_lock:{snapshot_name}"

    return False, f"unlocked:{snapshot_name}_not_captured"


def coached_score_market(
    market: dict[str, Any],
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> tuple[int, list[str]]:
    """Existing race-quality score plus historical Track Review liquidity."""
    score, reasons = score_market(market, config)
    if score <= -9999:
        return score, reasons

    stats = track_liquidity_for_market(market, liquidity_lookup)
    if not stats:
        return score, [*reasons, "liquidity:no_track_history"]

    band = str(stats.get("liquidity_band") or "UNKNOWN")
    confidence = str(stats.get("confidence") or "DISCOVERY")
    points = historical_liquidity_score(band, confidence)
    matched = stats.get("mean_matched")

    return (
        score + points,
        [
            *reasons,
            f"liquidity:{band}/{confidence} {points:+d} mean_matched={matched}",
        ],
    )


def scored_market_rows(
    markets: list[dict[str, Any]],
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for market in markets:
        seconds = market_seconds_to_jump(market)
        if seconds is None:
            continue

        score, reasons = coached_score_market(market, config, liquidity_lookup)
        if score <= -9999:
            continue

        rows.append(
            {
                "market": market,
                "market_id": market.get("market_id"),
                "race": f"{market.get('track', '')} {market.get('market_name', '')}".strip(),
                "seconds_to_jump": seconds,
                "score": score,
                "reasons": reasons,
            }
        )

    rows.sort(
        key=lambda item: (
            -int(item["score"]),
            int(item["seconds_to_jump"]),
        )
    )
    return rows


def choose_hold_challenger(
    current_choice: dict[str, Any],
    all_markets: list[dict[str, Any]],
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> dict[str, Any] | None:
    """Optionally HOLD rather than arm a weak race when a better one is nearby."""
    if not config.get("hold_for_better_candidate"):
        return None

    current_rows = scored_market_rows([current_choice], config, liquidity_lookup)
    if not current_rows:
        return None

    current = current_rows[0]
    current_seconds = int(current["seconds_to_jump"])
    current_id = current_choice.get("market_id")
    lookahead_seconds = int(config.get("lookahead_minutes", 30)) * 60
    min_seconds = int(config.get("rearm_min_seconds_to_jump", 720))
    min_delta = int(config.get("hold_min_score_delta", 30))

    challengers = [
        row
        for row in scored_market_rows(all_markets, config, liquidity_lookup)
        if row["market_id"] != current_id
        and min_seconds <= int(row["seconds_to_jump"]) <= lookahead_seconds
        and int(row["seconds_to_jump"]) > current_seconds
        and int(row["score"]) >= int(current["score"]) + min_delta
    ]
    return challengers[0] if challengers else None


def choose_rearm_challenger(
    active_market: dict[str, Any],
    all_markets: list[dict[str, Any]],
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Find a materially better race while the active target is still unlocked."""
    active_rows = scored_market_rows([active_market], config, liquidity_lookup)
    if not active_rows:
        return None, {"selection_reason": "active_market_not_scoreable"}

    active = active_rows[0]
    active_id = active_market.get("market_id")
    lookahead_seconds = int(config.get("lookahead_minutes", 30)) * 60
    min_seconds = int(config.get("rearm_min_seconds_to_jump", 720))
    min_delta = int(config.get("rearm_min_score_delta", 30))

    challengers = [
        row
        for row in scored_market_rows(all_markets, config, liquidity_lookup)
        if row["market_id"] != active_id
        and min_seconds <= int(row["seconds_to_jump"]) <= lookahead_seconds
        and int(row["score"]) >= int(active["score"]) + min_delta
    ]

    if not challengers:
        return None, {
            "selection_reason": "no_materially_better_challenger",
            "active_score": active["score"],
            "active_reasons": active["reasons"],
            "required_delta": min_delta,
        }

    challenger = challengers[0]
    return challenger, {
        "selection_reason": "materially_better_challenger_found",
        "active_score": active["score"],
        "active_reasons": active["reasons"],
        "challenger_score": challenger["score"],
        "challenger_reasons": challenger["reasons"],
        "score_delta": int(challenger["score"]) - int(active["score"]),
        "required_delta": min_delta,
    }

def score_race_quality(
    market: dict[str, Any],
) -> tuple[int, str, list[str]]:
    """
    Estimate basic race quality from Betfair's market description.

    This is a review/attention score only. It is not a betting rating.
    """
    market_name = norm_text(market.get("market_name"))
    event_name = norm_text(market.get("event_name"))

    text = f"{market_name} {event_name}".lower()

    score = 0
    reasons: list[str] = []

    # Highest-quality race descriptions first.
    quality_rules = [
        (("group 1", "grp 1", "g1"), 80, "Group 1"),
        (("group 2", "grp 2", "g2"), 60, "Group 2"),
        (("group 3", "grp 3", "g3"), 45, "Group 3"),
        (("listed",), 30, "Listed"),
        (("stakes", "stks"), 20, "Stakes"),
        (("allowance", "alw"), 12, "Allowance"),
        (("handicap", "hcap"), 8, "Handicap"),

        # Neutral-to-lower ordinary race types.
        (("starter allowance",), 5, "Starter allowance"),
        (("optional claiming", "opt clm"), 0, "Optional claiming"),
        (("maiden special weight", "msw"), -2, "Maiden special weight"),
        (("maiden", "mdn"), -12, "Maiden"),
        (("claiming", "claim", "clm"), -18, "Claiming"),
        (("maiden claiming", "mdn clm"), -25, "Maiden claiming"),
    ]

    matched_label = "Unclassified"

    for terms, points, label in quality_rules:
        if any(term in text for term in terms):
            score += points
            matched_label = label
            reasons.append(f"class:{label} {points:+d}")
            break

    # Small distance/context notes. These do not define quality,
    # but help the observer understand the race.
    if any(term in text for term in ("5f", "5.5f", "6f", "6.5f", "1000m", "1200m")):
        reasons.append("shape:sprint")
    elif any(term in text for term in ("1m", "8f", "1600m")):
        reasons.append("shape:mile")
    elif any(term in text for term in ("1m2f", "10f", "2000m")):
        reasons.append("shape:middle-distance")

    # Crude quality bands for display.
    if score >= 60:
        quality_label = "elite"
    elif score >= 30:
        quality_label = "high"
    elif score >= 10:
        quality_label = "solid"
    elif score >= -5:
        quality_label = "ordinary"
    elif score >= -20:
        quality_label = "low"
    else:
        quality_label = "very-low"

    if matched_label == "Unclassified":
        reasons.append("class:unclassified")

    return score, quality_label, reasons

def score_market(
    market: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, list[str]]:
    """
    Return attention score and explanation list.

    This does not predict winners. It only decides which race deserves
    the observer's attention.
    """
    score = 0
    reasons: list[str] = []

    excluded_reason = priority_excluded(market, config)
    if excluded_reason:
        return -9999, [excluded_reason]

    seconds = market_seconds_to_jump(market)
    if seconds is None:
        return -9999, ["invalid_start_time"]

    min_seconds = int(config.get("minimum_seconds_to_jump", 0))
    max_seconds = int(config.get("maximum_seconds_to_jump", 1800))

    if seconds < min_seconds:
        return -9999, [f"too_close:{seconds}s"]

    if seconds > max_seconds:
        return -9999, [f"too_far:{seconds}s"]

    quality_score, quality_label, quality_reasons = score_race_quality(market)

    score += quality_score
    reasons.extend(quality_reasons)        

    country = norm_text(market.get("country_code"))
    country_scores = config.get("country_scores", {})
    country_score = int(country_scores.get(country, 0))
    if country_score:
        score += country_score
        reasons.append(f"country:{country} {country_score:+d}")

    track = norm_text(market.get("track"))
    track_scores = config.get("track_scores", {})
    track_score = int(track_scores.get(track, 0))
    if track_score:
        score += track_score
        reasons.append(f"track:{track} {track_score:+d}")

    weekday = market_weekday_name(market)
    weekday_scores = config.get("weekday_scores", {})
    weekday_score = int(weekday_scores.get(weekday, 0))
    if weekday_score:
        score += weekday_score
        reasons.append(f"weekday:{weekday} {weekday_score:+d}")

    market_name = norm_text(market.get("market_name"))
    race_name_scores = config.get("race_name_scores", {})
    lower_name = market_name.lower()

    for term, term_score_raw in race_name_scores.items():
        term_text = str(term).lower()
        if term_text and term_text in lower_name:
            term_score = int(term_score_raw)
            score += term_score
            reasons.append(f"name:{term} {term_score:+d}")

    # Timing bonus: best observation target is near T-15, but do not be precious.
    if 720 <= seconds <= 1080:
        score += 15
        reasons.append("timing:T12-T18 +15")
    elif 600 <= seconds <= 1200:
        score += 8
        reasons.append("timing:T10-T20 +8")
    elif seconds < 300:
        score -= 20
        reasons.append("timing:late -20")

    # Field size bonus if runners are present in catalogue.
    runners = market.get("runners")
    if isinstance(runners, list):
        field_size = len(runners)
        if field_size >= 8:
            score += 8
            reasons.append(f"field:{field_size} +8")
        elif field_size <= 5:
            score -= 8
            reasons.append(f"field:{field_size} -8")

    if not reasons:
        reasons.append("base_score")

    return score, reasons


def select_market_by_priority(
    markets: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Choose highest-scoring candidate.

    If the top scores are close, prefer the earlier race so the bot does not
    skip races endlessly while chasing a perfect one.
    """
    scored: list[dict[str, Any]] = []

    for market in markets:
        score, reasons = score_market(market, config)
        seconds = market_seconds_to_jump(market)

        if score <= -9999:
            continue

        scored.append(
            {
                "market": market,
                "score": score,
                "reasons": reasons,
                "seconds_to_jump": seconds,
                "market_id": market.get("market_id"),
                "race": f"{market.get('track', '')} {market.get('market_name', '')}".strip(),
            }
        )

    if not scored:
        return None, {
            "selection_mode": "priority",
            "priority_enabled": True,
            "candidates_considered": [],
            "selection_reason": "no_scored_candidates",
        }

    scored.sort(
        key=lambda item: (
            -int(item["score"]),
            int(item["seconds_to_jump"] or 999999),
        )
    )

    top = scored[0]
    close_margin = int(config.get("prefer_earlier_if_score_within", 15))

    close_candidates = [
        item for item in scored
        if int(top["score"]) - int(item["score"]) <= close_margin
    ]

    close_candidates.sort(
        key=lambda item: int(item["seconds_to_jump"] or 999999)
    )

    chosen = close_candidates[0]

    debug = {
        "selection_mode": "priority",
        "priority_enabled": True,
        "chosen_score": chosen["score"],
        "chosen_reasons": chosen["reasons"],
        "chosen_market_id": chosen["market_id"],
        "chosen_race": chosen["race"],
        "chosen_seconds_to_jump": chosen["seconds_to_jump"],
        "tie_break": (
            f"preferred earlier if within {close_margin} points"
        ),
        "candidates_considered": [
            {
                "market_id": item["market_id"],
                "race": item["race"],
                "score": item["score"],
                "seconds_to_jump": item["seconds_to_jump"],
                "reasons": item["reasons"],
            }
            for item in scored[:10]
        ],
    }

    return chosen["market"], debug

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit a worldwide Betfair Horse Racing T-15 target."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--secrets-file", type=Path,
                        default=Path(os.environ.get("BETFAIR_SECRETS_FILE", "/opt/betfair/secrets.env")),
                        help="Gateway credentials file (default: BETFAIR_SECRETS_FILE or /opt/betfair/secrets.env).")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Directory for shared state files (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--target-minutes",
        type=int,
        default=DEFAULT_TARGET_MINUTES,
        help="Target time before jump (default: 15)",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=DEFAULT_WINDOW_MINUTES,
        help="Arming tolerance either side of target (default: 3)",
    )
    parser.add_argument(
        "--search-hours",
        type=int,
        default=DEFAULT_SEARCH_HOURS,
        help="How far ahead to search in hours (default: 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print state but do not write files.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"ToteBot config directory (default: {DEFAULT_CONFIG_DIR})",
    )
    args = parser.parse_args()

    state_dir = args.state_dir.expanduser()
    target_path = state_dir / TARGET_FILE_NAME
    status_path = state_dir / STATUS_FILE_NAME
    priority_config = load_priority_config(args.config_dir.expanduser())
    liquidity_lookup = load_track_liquidity(state_dir, priority_config)
    selection_debug: dict[str, Any] = {
        "selection_mode": "priority" if priority_config.get("enabled") else "earliest",
        "priority_enabled": bool(priority_config.get("enabled")),
        "selection_reason": "not_evaluated_yet",
    }

    try:
        load_secrets(args.secrets_file.expanduser())

        previous_target = read_json(target_path)

        # Keep an already armed race stable unless an explicit pre-lock re-arm
        # rule finds a materially better candidate.
        if existing_target_is_active(previous_target, state_dir):
            active_market = market_from_existing_target(previous_target) or {}
            active_market = {
                **active_market,
                "seconds_to_jump": (
                    seconds_to_start(active_market["market_start_time"])
                    if active_market.get("market_start_time")
                    else None
                ),
            }

            hard_locked, lock_reason = active_target_is_hard_locked(
                state_dir,
                active_market,
                priority_config,
            )
            timing = evaluate(active_market, load_lifecycle(state_dir.parent, active_market),
                              utc_now(), policy(state_dir.parent))
            if (active_market.get("seconds_to_jump") or 0) <= 0:
                hard_locked, lock_reason = True, "delayed_race_observation_hold"

            # ---------------------------------------------------------
            # ONE-SHOT MANUAL ARM
            #
            # Precedence:
            #   hard lock
            #   manual request
            #   automatic coach/rearm
            # ---------------------------------------------------------

            manual = pending_request(state_dir)

            if manual:
                requested_market_id = str(manual["market_id"])

                if hard_locked:
                    if not args.dry_run:
                        reject_request(
                            state_dir,
                            result="current_market_hard_locked",
                            market=active_market,
                        )

                    print(
                        "MANUAL_ARM rejected "
                        f"requested={requested_market_id} "
                        f"active={active_market.get('market_id')} "
                        f"reason={lock_reason}"
                    )

                elif requested_market_id == str(
                    active_market.get("market_id") or ""
                ):
                    if not args.dry_run:
                        consume_request(
                            state_dir,
                            result="already_tracking_requested_market",
                            market=active_market,
                        )

                    print(
                        "MANUAL_ARM consumed "
                        f"market={requested_market_id} "
                        "reason=already_tracking"
                    )

                else:
                    markets = discover_markets(args.search_hours)

                    manual_candidate = find_market_by_id(
                        markets,
                        requested_market_id,
                    )

                    if manual_candidate is None:
                        if not args.dry_run:
                            reject_request(
                                state_dir,
                                result="requested_market_not_found",
                            )

                        print(
                            "MANUAL_ARM rejected "
                            f"requested={requested_market_id} "
                            "reason=requested_market_not_found"
                        )

                    else:
                        seconds = manual_candidate.get("seconds_to_jump")

                        if seconds is None or seconds <= 0:
                            if not args.dry_run:
                                reject_request(
                                    state_dir,
                                    result="requested_market_already_started",
                                    market=manual_candidate,
                                )

                            print(
                                "MANUAL_ARM rejected "
                                f"requested={requested_market_id} "
                                "reason=requested_market_already_started"
                            )

                        else:
                            target = build_armed_target(
                                manual_candidate,
                                args.target_minutes,
                                args.window_minutes,
                                previous_target,
                            )

                            selection_debug = {
                                "selection_mode": "manual_one_shot",
                                "priority_enabled": bool(
                                    priority_config.get("enabled")
                                ),
                                "selection_reason": "manual_arm_request",
                                "from_market_id": active_market.get(
                                    "market_id"
                                ),
                                "to_market_id": manual_candidate.get(
                                    "market_id"
                                ),
                                "lock_state": lock_reason,
                            }

                            status = build_status(
                                search_hours=args.search_hours,
                                target_minutes=args.target_minutes,
                                window_minutes=args.window_minutes,
                                status="rearmed",
                                reason="manual_one_shot_arm",
                                next_market=manual_candidate,
                                active_target=target,
                                selection_debug=selection_debug,
                            )

                            if not args.dry_run:
                                atomic_write_json(
                                    target_path,
                                    target,
                                )
                                atomic_write_json(
                                    status_path,
                                    status,
                                )

                                consume_request(
                                    state_dir,
                                    result="armed",
                                    market=manual_candidate,
                                )

                            print(
                                "MANUAL_ARM armed "
                                f"market={manual_candidate.get('market_id')} "
                                f"track={manual_candidate.get('track')} "
                                f"race={manual_candidate.get('market_name')} "
                                f"seconds_to_jump={seconds}"
                            )

                            return 0
                            
            rearm_enabled = bool(
                priority_config.get("enabled")
                and priority_config.get("rearm_enabled")
            )

            if rearm_enabled and not hard_locked:
                markets = discover_markets(args.search_hours)
                challenger, rearm_debug = choose_rearm_challenger(
                    active_market,
                    markets,
                    priority_config,
                    liquidity_lookup,
                )

                if challenger is not None:
                    candidate = challenger["market"]
                    target = build_armed_target(
                        candidate,
                        args.target_minutes,
                        args.window_minutes,
                        previous_target,
                    )

                    selection_debug = {
                        "selection_mode": "rearm",
                        "priority_enabled": True,
                        "selection_reason": "rearmed_to_materially_better_candidate",
                        "from_market_id": active_market.get("market_id"),
                        "from_race": f"{active_market.get('track', '')} {active_market.get('market_name', '')}".strip(),
                        "to_market_id": candidate.get("market_id"),
                        "to_race": challenger.get("race"),
                        "lock_state": lock_reason,
                        **rearm_debug,
                    }

                    status = build_status(
                        search_hours=args.search_hours,
                        target_minutes=args.target_minutes,
                        window_minutes=args.window_minutes,
                        status="rearmed",
                        reason="materially_better_candidate_before_t15_lock",
                        next_market=candidate,
                        active_target=target,
                        selection_debug=selection_debug,
                    )

                    if not args.dry_run:
                        atomic_write_json(target_path, target)
                        atomic_write_json(status_path, status)

                    print(json.dumps(status, indent=2, sort_keys=True))
                    return 0

            active_market = with_runner_matched(active_market)
            refreshed_target = {
                **previous_target,
                "version": VERSION,
                "updated_at": iso_z(utc_now()),
                "market": active_market,
                "observation": timing,
            }

            status = build_status(
                search_hours=args.search_hours,
                target_minutes=args.target_minutes,
                window_minutes=args.window_minutes,
                status="tracking",
                reason=(
                    "existing_target_hard_locked"
                    if hard_locked
                    else "existing_target_remains_active"
                ),
                next_market=active_market,
                active_target=refreshed_target,
                selection_debug={
                    "selection_mode": "tracking_existing",
                    "priority_enabled": bool(priority_config.get("enabled")),
                    "selection_reason": (
                        "existing_target_hard_locked"
                        if hard_locked
                        else "no_rearm_performed"
                    ),
                    "lock_state": lock_reason,
                    "rearm_enabled": rearm_enabled,
                },
            )

            if not args.dry_run:
                atomic_write_json(target_path, refreshed_target)
                atomic_write_json(status_path, status)

            print(
                f"status={status['status']} "
                f"reason={status['reason']} "
                f"next_market={status.get('next_market', {}).get('market_id')} "
                f"track={status.get('next_market', {}).get('track')} "
                f"seconds_to_jump={status.get('next_market', {}).get('seconds_to_jump')}"
            )
            return 0

        # ---------------------------------------------------------
        # Manual arm when there is currently no active target.
        # ---------------------------------------------------------

        manual = pending_request(state_dir)

        if manual:
            requested_market_id = str(manual["market_id"])
            markets = discover_markets(args.search_hours)

            manual_candidate = find_market_by_id(
                markets,
                requested_market_id,
            )

            if manual_candidate is None:
                if not args.dry_run:
                    reject_request(
                        state_dir,
                        result="requested_market_not_found",
                    )

                print(
                    "MANUAL_ARM rejected "
                    f"requested={requested_market_id} "
                    "reason=requested_market_not_found"
                )

            else:
                seconds = manual_candidate.get("seconds_to_jump")

                if seconds is None or seconds <= 0:
                    if not args.dry_run:
                        reject_request(
                            state_dir,
                            result="requested_market_already_started",
                            market=manual_candidate,
                        )

                else:
                    target = build_armed_target(
                        manual_candidate,
                        args.target_minutes,
                        args.window_minutes,
                        previous_target,
                    )

                    status = build_status(
                        search_hours=args.search_hours,
                        target_minutes=args.target_minutes,
                        window_minutes=args.window_minutes,
                        status="armed",
                        reason="manual_one_shot_arm",
                        next_market=manual_candidate,
                        active_target=target,
                        selection_debug={
                            "selection_mode": "manual_one_shot",
                            "priority_enabled": bool(
                                priority_config.get("enabled")
                            ),
                            "selection_reason": "manual_arm_request",
                        },
                    )

                    if not args.dry_run:
                        atomic_write_json(target_path, target)
                        atomic_write_json(status_path, status)

                        consume_request(
                            state_dir,
                            result="armed",
                            market=manual_candidate,
                        )

                    print(
                        "MANUAL_ARM armed "
                        f"market={manual_candidate.get('market_id')} "
                        f"track={manual_candidate.get('track')} "
                        f"race={manual_candidate.get('market_name')} "
                        f"seconds_to_jump={seconds}"
                    )

                    return 0

        # Expire the old target before selecting the next one.
        if previous_target and previous_target.get("status") == "armed":
            previous_market = market_from_existing_target(previous_target) or {}
            timing = evaluate(previous_market, load_lifecycle(state_dir.parent, previous_market),
                              utc_now(), policy(state_dir.parent))
            expired_target = build_expired_target(previous_target, timing["reason"])

            if not args.dry_run:
                atomic_write_json(target_path, expired_target)

        markets = discover_markets(args.search_hours)
        candidate, selection_debug = find_t15_candidate(
            markets,
            args.target_minutes,
            args.window_minutes,
            priority_config,
        )
        next_market = next_future_market(markets)

        if (
            candidate
            and priority_config.get("enabled")
            and priority_config.get("hold_for_better_candidate")
        ):
            challenger = choose_hold_challenger(
                candidate,
                markets,
                priority_config,
                liquidity_lookup,
            )

            if challenger is not None:
                current_rows = scored_market_rows(
                    [candidate],
                    priority_config,
                    liquidity_lookup,
                )
                current_score = current_rows[0]["score"] if current_rows else None

                selection_debug = {
                    "selection_mode": "hold",
                    "priority_enabled": True,
                    "selection_reason": "holding_for_materially_better_candidate",
                    "current_candidate_market_id": candidate.get("market_id"),
                    "current_candidate_race": f"{candidate.get('track', '')} {candidate.get('market_name', '')}".strip(),
                    "current_candidate_score": current_score,
                    "challenger_market_id": challenger.get("market_id"),
                    "challenger_race": challenger.get("race"),
                    "challenger_score": challenger.get("score"),
                    "challenger_seconds_to_jump": challenger.get("seconds_to_jump"),
                    "challenger_reasons": challenger.get("reasons"),
                    "required_delta": int(priority_config.get("hold_min_score_delta", 30)),
                }

                status = build_status(
                    search_hours=args.search_hours,
                    target_minutes=args.target_minutes,
                    window_minutes=args.window_minutes,
                    status="holding",
                    reason="better_candidate_inside_lookahead",
                    next_market=challenger["market"],
                    active_target=None,
                    selection_debug=selection_debug,
                )

                if not args.dry_run:
                    atomic_write_json(status_path, status)

                print(json.dumps(status, indent=2, sort_keys=True))
                return 0

        if candidate:
            target = build_armed_target(
                candidate,
                args.target_minutes,
                args.window_minutes,
                previous_target,
            )

            status = build_status(
                search_hours=args.search_hours,
                target_minutes=args.target_minutes,
                window_minutes=args.window_minutes,
                status="armed",
                reason="market_inside_t15_arming_window",
                next_market=candidate,
                active_target=target,
                selection_debug=selection_debug,
            )

            if not args.dry_run:
                atomic_write_json(target_path, target)
                atomic_write_json(status_path, status)

            print(
                f"status=armed "
                f"market={candidate.get('market_id')} "
                f"track={candidate.get('track')} "
                f"race={candidate.get('market_name')!r} "
                f"seconds_to_jump={candidate.get('seconds_to_jump')} "
                f"matched={candidate.get('total_matched')}"
            )

            return 0

        if next_market is None:
            state_status = "idle"
            reason = "no_worldwide_horse_racing_win_market_in_search_horizon"
        else:
            state_status = "waiting"
            reason = "no_market_currently_inside_t15_arming_window"

        status = build_status(
            search_hours=args.search_hours,
            target_minutes=args.target_minutes,
            window_minutes=args.window_minutes,
            status=state_status,
            reason=reason,
            next_market=next_market,
            active_target=None,
            selection_debug=selection_debug,
        )

        if not args.dry_run:
            atomic_write_json(status_path, status)

        print(f"status={state_status} reason={reason}")

        return 0

    except Exception as exc:
        error = {
            "schema": "betfair_watch_status/v1",
            "watcher": "basecamp-betfair-t15-emitter",
            "updated_at": iso_z(utc_now()),
            "ok": False,
            "status": "error",
            "error": str(exc),
        }

        if not args.dry_run:
            atomic_write_json(status_path, error)

        print(json.dumps(error, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
