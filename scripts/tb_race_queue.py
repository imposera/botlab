#!/usr/bin/env python3
"""
ToteBot rolling next-to-jump race queue.

Purpose
-------
- Pull the next Betfair Horse Racing WIN markets.
- Keep Betfair start-time order as the rolling queue order.
- Add conservative KEEP / REVIEW / PRUNE? recommendations.
- Preserve explicit human KEEP / REMOVE decisions across refreshes.
- Expose the first eligible race for a later emitter handoff.
- Log human decisions as labelled examples for future learning.

Important
---------
This version never auto-prunes a race.  A PRUNE? recommendation is advisory.
Only an explicit human REMOVE hides a race from the human queue.
Learned clash preferences can separately make it ineligible for automatic arming.

Files
-----
  state/tb_race_queue.json
  state/tb_race_queue_decisions.jsonl

Examples
--------
  python3 tb_race_queue.py refresh
  python3 tb_race_queue.py show
  python3 tb_race_queue.py remove 1.261917584 --reason "thin claiming race"
  python3 tb_race_queue.py keep 1.261920235 --reason "Randwick metro"
  python3 tb_race_queue.py restore 1.261917584
  python3 tb_race_queue.py next
  python3 tb_race_queue.py next --json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import fcntl
import re
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from tb_queue_policy import annotate, learn
from tb_track_priority import load as load_track_priority, clash_preferences

VERSION = "1.2.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_secrets(path: Path) -> None:
    # Offline show/next/decision commands do not require the gateway or requests.
    from betfair_gateway import load_secrets as gateway_load_secrets
    gateway_load_secrets(path.expanduser())


DEFAULT_STATE_DIR = Path.home() / "botlab" / "totebot" / "state"
DEFAULT_CONFIG_DIR = Path.home() / "botlab" / "totebot" / "config"
DEFAULT_SECRETS = Path(os.environ.get("BETFAIR_SECRETS_FILE", "/opt/betfair/secrets.env"))
DEFAULT_LIMIT = 10
DEFAULT_SEARCH_HOURS = 4

QUEUE_FILE_NAME = "tb_race_queue.json"
DECISION_LOG_FILE_NAME = "tb_race_queue_decisions.jsonl"
PRIORITY_CONFIG_FILE_NAME = "race_priority.json"

HARNESS_TERMS = (
    "trot",
    "trotting",
    "trotters",
    "harness",
    "pace",
    "pacing",
    "mobile pace",
)
HARNESS_TRACKS = ("globe derby",)


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

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
        tmp = Path(handle.name)
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


@contextmanager
def queue_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / ".tb_race_queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def locked_decision(function):
    @wraps(function)
    def wrapped(state_dir, *args, **kwargs):
        with queue_lock(state_dir):
            return function(state_dir, *args, **kwargs)
    return wrapped


def load_queue_for_update(state_dir: Path) -> dict[str, Any]:
    path = state_dir / QUEUE_FILE_NAME
    queue = read_json(path)
    if queue is None:
        if path.exists() or (state_dir / DECISION_LOG_FILE_NAME).exists():
            raise RuntimeError("Queue missing or unreadable; refusing to discard human decisions. Restore the queue before refreshing.")
        return {}
    if not isinstance(queue.get("decisions"), dict) or not isinstance(queue.get("races"), list):
        raise RuntimeError("Invalid queue structure; refusing to discard human decisions.")
    if any(not isinstance(row, dict) for row in queue["races"]) or any(
        not isinstance(decision, dict) or decision.get("action") not in {"keep", "remove"}
        for decision in queue["decisions"].values()
    ):
        raise RuntimeError("Invalid queue rows or decisions; restore the queue before refreshing.")
    return queue


def current_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        seconds = seconds_to_start(row["market_start_time"])
    except (KeyError, AttributeError, TypeError, ValueError):
        seconds = None
    return {**row, "seconds_to_jump": seconds,
            "minutes_to_jump": round(seconds / 60, 1) if seconds is not None else None}


# ---------------------------------------------------------------------------
# Betfair discovery
# ---------------------------------------------------------------------------

def is_harness_market(market: dict[str, Any]) -> bool:
    text = " ".join(
        str(market.get(field) or "")
        for field in ("track", "event_name", "market_name")
    ).lower()
    return (
        any(term in text for term in HARNESS_TERMS)
        or any(track in text for track in HARNESS_TRACKS)
    )


def seconds_to_start(value: str) -> int:
    start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return int((start - utc_now()).total_seconds())


def discover_markets(search_hours: int) -> list[dict[str, Any]]:
    from betfair_gateway import HORSE_RACING_EVENT_TYPE_ID, betfair_login, betting_api, market_summary
    now = utc_now()
    until = now + timedelta(hours=search_hours)
    token = betfair_login()

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
        token,
    )

    markets = [
        market_summary(item)
        for item in catalogue
        if item.get("marketId") and item.get("marketStartTime")
    ]
    markets = [m for m in markets if not is_harness_market(m)]
    markets = [m for m in markets if seconds_to_start(m["market_start_time"]) > 0]
    markets.sort(key=lambda m: m["market_start_time"])
    return markets


# ---------------------------------------------------------------------------
# Coaching inputs
# ---------------------------------------------------------------------------

def default_priority_config() -> dict[str, Any]:
    return {
        "country_scores": {},
        "track_scores": {},
        "race_name_scores": {},
        "exclude_terms": [],
        "track_liquidity_enabled": True,
        "track_liquidity_file": "track_liquidity.json",
    }


def load_priority_config(config_dir: Path) -> dict[str, Any]:
    config = default_priority_config()
    user = read_json(config_dir / PRIORITY_CONFIG_FILE_NAME)
    if user:
        config.update(user)
    return config


def load_track_liquidity(state_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("track_liquidity_enabled", True):
        return {}
    name = str(config.get("track_liquidity_file") or "track_liquidity.json")
    payload = read_json(state_dir / name) or {}
    tracks = payload.get("tracks")
    return tracks if isinstance(tracks, dict) else {}


def track_liquidity_for_market(
    market: dict[str, Any],
    lookup: dict[str, Any],
) -> dict[str, Any]:
    wanted = str(market.get("track") or "").strip().casefold()
    for track_name, stats in lookup.items():
        if str(track_name).strip().casefold() == wanted and isinstance(stats, dict):
            return stats
    return {}


def historical_liquidity_score(band: str, confidence: str) -> int:
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
    return scores.get((str(band).upper(), str(confidence).upper()), 0)


def score_race_quality(market: dict[str, Any]) -> tuple[int, list[str]]:
    text = f"{market.get('market_name', '')} {market.get('event_name', '')}".lower()
    score = 0
    reasons: list[str] = []

    # Deliberately mirrors the current emitter's broad race-quality prior.
    rules = [
        (("group 1", "grp 1", "grade 1", "grd 1", "g1"), 80, "Group 1"),
        (("group 2", "grp 2", "grade 2", "grd 2", "g2"), 60, "Group 2"),
        (("group 3", "grp 3", "grade 3", "grd 3", "g3"), 45, "Group 3"),
        (("listed",), 30, "Listed"),
        (("stakes", "stks"), 20, "Stakes"),
        (("starter allowance",), 5, "Starter allowance"),
        (("allowance", "alw"), 12, "Allowance"),
        (("handicap", "hcap"), 8, "Handicap"),
        (("optional claiming", "opt clm"), 0, "Optional claiming"),
        (("maiden special weight", "msw"), -2, "Maiden special weight"),
        (("maiden claiming", "mdn clm"), -25, "Maiden claiming"),
        (("maiden", "mdn"), -12, "Maiden"),
        (("claiming", "claim", "clm"), -18, "Claiming"),
    ]

    for terms, points, label in rules:
        if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) for term in terms):
            score += points
            reasons.append(f"class:{label} {points:+d}")
            break
    else:
        reasons.append("class:unclassified")

    runners = market.get("runners")
    if isinstance(runners, list):
        field = len(runners)
        if field >= 8:
            score += 8
            reasons.append(f"field:{field} +8")
        elif field <= 5:
            score -= 8
            reasons.append(f"field:{field} -8")

    return score, reasons


def queue_score(
    market: dict[str, Any],
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
) -> tuple[int, list[str], dict[str, Any]]:
    score, reasons = score_race_quality(market)
    text = " ".join(
        str(market.get(k) or "")
        for k in ("track", "event_name", "market_name", "country_code")
    ).lower()

    for term in config.get("exclude_terms", []):
        t = str(term).strip().lower()
        if t and t in text:
            score -= 80
            reasons.append(f"configured_exclude:{term} -80")

    country = str(market.get("country_code") or "").strip()
    country_points = int(config.get("country_scores", {}).get(country, 0))
    if country_points:
        score += country_points
        reasons.append(f"country:{country} {country_points:+d}")

    track = str(market.get("track") or "").strip()
    track_points = int(config.get("track_scores", {}).get(track, 0))
    if track_points:
        score += track_points
        reasons.append(f"track:{track} {track_points:+d}")

    market_name = str(market.get("market_name") or "")
    for term, raw in config.get("race_name_scores", {}).items():
        if str(term).lower() in market_name.lower():
            points = int(raw)
            score += points
            reasons.append(f"name:{term} {points:+d}")

    liq = track_liquidity_for_market(market, liquidity_lookup)
    liq_band = str(liq.get("liquidity_band") or "UNKNOWN")
    liq_conf = str(liq.get("confidence") or "DISCOVERY")
    liq_points = historical_liquidity_score(liq_band, liq_conf)
    score += liq_points

    if liq:
        reasons.append(
            f"liquidity:{liq_band}/{liq_conf} {liq_points:+d} "
            f"mean_matched={liq.get('mean_matched')}"
        )
    else:
        reasons.append("liquidity:no_track_history")

    meta = {
        "liquidity_band": liq_band,
        "liquidity_confidence": liq_conf,
        "historical_mean_matched": liq.get("mean_matched") if liq else None,
    }
    return score, reasons, meta


def recommendation(score: int) -> tuple[str, str]:
    """Advisory only: never removes a race automatically."""
    if score <= -25:
        return "PRUNE?", "high"
    if score <= -10:
        return "PRUNE?", "medium"
    if score >= 30:
        return "KEEP", "high"
    if score >= 15:
        return "KEEP", "medium"
    return "REVIEW", "low"


# ---------------------------------------------------------------------------
# Queue state
# ---------------------------------------------------------------------------

def row_for_market(
    market: dict[str, Any],
    position: int,
    config: dict[str, Any],
    liquidity_lookup: dict[str, Any],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    seconds = seconds_to_start(market["market_start_time"])
    score, reasons, meta = queue_score(market, config, liquidity_lookup)
    rec, confidence = recommendation(score)

    human = None
    if isinstance(decision, dict):
        human = decision.get("action")

    eligible = human != "remove"

    return {
        "position": position,
        "market_id": market.get("market_id"),
        "track": market.get("track"),
        "country_code": market.get("country_code"),
        "event_name": market.get("event_name"),
        "market_name": market.get("market_name"),
        "market_start_time": market.get("market_start_time"),
        "seconds_to_jump": seconds,
        "minutes_to_jump": round(seconds / 60, 1),
        "runner_count": len(market.get("runners") or []),
        "total_matched": market.get("total_matched"),
        "coach_score": score,
        "recommendation": rec,
        "recommendation_confidence": confidence,
        "recommendation_reasons": reasons,
        "human_action": human,
        "human_reason": decision.get("reason") if isinstance(decision, dict) else None,
        "eligible": eligible,
        **meta,
    }



def apply_track_preferences(rows, decisions, config_dir, now):
    try:
        priority = load_track_priority(config_dir)
    except (ValueError, OSError):
        return rows
    config = load_priority_config(config_dir)
    def allowed(row):
        text = ' '.join(str(row.get(k) or '').strip().lower() for k in ('track', 'event_name', 'market_name', 'country_code'))
        return not config.get('enabled') or not any(str(t).strip().lower() in text for t in config.get('exclude_terms', []) if str(t).strip())
    _, skipped = clash_preferences([r for r in rows if r.get('arming_eligible')], decisions,
                                   priority, now, alternative_allowed=allowed)
    by_id = {r['market_id']:r for r in skipped}
    for row in rows:
        if str(row['market_id']) in by_id:
            row.update(arming_eligible=False, clash_skip=by_id[str(row['market_id'])])
    return rows


def refresh_queue(
    state_dir: Path,
    config_dir: Path,
    secrets_path: Path,
    limit: int,
    search_hours: int,
) -> dict[str, Any]:
    load_secrets(secrets_path)
    config = load_priority_config(config_dir)
    liquidity = load_track_liquidity(state_dir, config)
    markets = discover_markets(search_hours)[:limit]

    with queue_lock(state_dir):
        prior = load_queue_for_update(state_dir)
        decisions = prior.get("decisions", {})
        rows = [
            row_for_market(
                market,
                idx,
                config,
                liquidity,
                decisions.get(str(market.get("market_id"))),
            )
            for idx, market in enumerate(markets, start=1)
        ]

        rows = annotate(rows, decisions, learn(state_dir, utc_now()), utc_now())
        rows = apply_track_preferences(rows, decisions, config_dir, utc_now())
        queue = {
            "schema": "tb_race_queue/v1",
            "version": VERSION,
            "updated_at": iso_z(utc_now()),
            "source": "betfair_next_to_jump",
            "search_hours": search_hours,
            "limit": limit,
            "policy": {
                "ordering": "betfair_market_start_time",
                "auto_prune": False,
                "learned_clash_skip": True,
                "human_remove_required_for_ineligible": True,
            },
            "decisions": decisions,
            "races": rows,
        }

        atomic_write_json(state_dir / QUEUE_FILE_NAME, queue)
        return queue


@locked_decision
def update_decision(
    state_dir: Path,
    market_id: str,
    action: str | None,
    reason: str | None,
    *, race_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = state_dir / QUEUE_FILE_NAME
    queue = load_queue_for_update(state_dir)
    if not queue:
        raise RuntimeError("queue does not exist; run 'refresh' first")

    decisions = queue.get("decisions")
    if not isinstance(decisions, dict):
        decisions = {}

    races = queue.get("races") if isinstance(queue.get("races"), list) else []
    row = next((r for r in races if str(r.get("market_id")) == market_id), None)
    if row is None and race_snapshot is not None:
        if str(race_snapshot.get('market_id')) != market_id:
            raise ValueError('Decision snapshot market mismatch')
        row = dict(race_snapshot)

    if action is None:
        decisions.pop(market_id, None)
        event_action = "restore"
    else:
        decisions[market_id] = {
            "action": action,
            "reason": reason,
            "updated_at": iso_z(utc_now()),
        }
        event_action = action

    queue["decisions"] = decisions
    queue["updated_at"] = iso_z(utc_now())

    # Apply immediately to the in-memory displayed row too.
    if row is not None:
        row["human_action"] = action
        row["human_reason"] = reason
        row["eligible"] = action != "remove"

    atomic_write_json(path, queue)

    append_jsonl(
        state_dir / DECISION_LOG_FILE_NAME,
        {
            "schema": "tb_race_queue_decision/v1",
            "recorded_at": iso_z(utc_now()),
            "market_id": market_id,
            "action": event_action,
            "reason": reason,
            "race_snapshot": row,
        },
    )
    queue['races'] = annotate(queue['races'], decisions, learn(state_dir, utc_now()), utc_now())
    queue['races'] = apply_track_preferences(queue['races'], decisions, state_dir.parent/'config', utc_now())
    atomic_write_json(path, queue)
    return queue


def next_eligible(queue: dict[str, Any]) -> dict[str, Any] | None:
    races = queue.get("races")
    if not isinstance(races, list):
        return None
    for row in races:
        if not isinstance(row, dict):
            continue
        row = current_row(row)
        if row.get("arming_eligible", row.get("eligible")) and row.get("seconds_to_jump") is not None and row["seconds_to_jump"] > 0:
            return row
    return None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def fmt_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}m"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:.0f}"


def display(queue: dict[str, Any]) -> None:
    races = queue.get("races") if isinstance(queue.get("races"), list) else []
    print("TOTEBOARD — ROLLING QUEUE")
    print(f"updated {queue.get('updated_at')}  races={len(races)}")
    print()
    print(
        f"{'#':>2} {'T-':>6} {'TRACK':<18} {'RACE':<22} "
        f"{'MATCH':>7} {'SCORE':>5} {'COACH':<8} {'HUMAN':<8}"
    )
    print("-" * 88)

    for saved_row in races:
        row = current_row(saved_row)
        human = "clash" if row.get("clash_skip") else (row.get("human_action") or "-")
        countdown = f"{row['minutes_to_jump']:.1f}" if row['minutes_to_jump'] is not None else "-"
        print(
            f"{int(row.get('position') or 0):>2} "
            f"{countdown:>6} "
            f"{str(row.get('track') or '')[:18]:<18} "
            f"{str(row.get('market_name') or '')[:22]:<22} "
            f"{fmt_money(row.get('total_matched')):>7} "
            f"{int(row.get('coach_score') or 0):>5} "
            f"{str(row.get('recommendation') or ''):<8} "
            f"{human:<8}"
        )

    for row in races:
        if row.get("clash_skip"):
            print(f"  {row.get('market_id')}: {row['clash_skip']['reason']}")

    nxt = next_eligible(queue)
    print()
    if nxt:
        print(
            "NEXT ELIGIBLE: "
            f"{nxt.get('market_id')}  {nxt.get('track')} "
            f"{nxt.get('market_name')}  T-{nxt.get('minutes_to_jump')}m"
        )
    else:
        print("NEXT ELIGIBLE: none")

    prune = [
        r for r in races
        if r.get("recommendation") == "PRUNE?" and r.get("human_action") != "remove"
    ]
    if prune:
        print("\nPRUNE SUGGESTIONS:")
        for row in prune:
            reasons = "; ".join(row.get("recommendation_reasons") or [])
            print(
                f"  {row.get('market_id')} {row.get('track')} {row.get('market_name')} "
                f"score={row.get('coach_score')} [{reasons}]"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="ToteBot rolling Betfair race queue")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"state directory (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"config directory (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=DEFAULT_SECRETS,
        help=f"Betfair secrets file (default: {DEFAULT_SECRETS})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="fetch and rebuild rolling queue")
    p_refresh.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p_refresh.add_argument("--search-hours", type=int, default=DEFAULT_SEARCH_HOURS)

    sub.add_parser("show", help="show current queue without calling Betfair")

    for command in ("remove", "keep"):
        p = sub.add_parser(command, help=f"mark one race {command}")
        p.add_argument("market_id")
        p.add_argument("--reason", default=None)

    p_restore = sub.add_parser("restore", help="clear human decision for a race")
    p_restore.add_argument("market_id")

    p_next = sub.add_parser("next", help="show first eligible race")
    p_next.add_argument("--json", action="store_true")

    args = parser.parse_args()
    state_dir = args.state_dir.expanduser()
    config_dir = args.config_dir.expanduser()

    try:
        if args.command == "refresh":
            queue = refresh_queue(
                state_dir,
                config_dir,
                args.secrets,
                max(1, args.limit),
                max(1, args.search_hours),
            )
            display(queue)
            return 0

        queue = read_json(state_dir / QUEUE_FILE_NAME)
        if not queue:
            raise RuntimeError("queue does not exist; run 'refresh' first")

        if args.command == "show":
            display(queue)
            return 0

        if args.command == "remove":
            queue = update_decision(
                state_dir,
                str(args.market_id),
                "remove",
                args.reason,
            )
            display(queue)
            return 0

        if args.command == "keep":
            queue = update_decision(
                state_dir,
                str(args.market_id),
                "keep",
                args.reason,
            )
            display(queue)
            return 0

        if args.command == "restore":
            queue = update_decision(
                state_dir,
                str(args.market_id),
                None,
                None,
            )
            display(queue)
            return 0

        if args.command == "next":
            row = next_eligible(queue)
            if args.json:
                print(json.dumps(row, indent=2, sort_keys=True))
            elif row:
                print(
                    f"{row.get('market_id')} {row.get('track')} "
                    f"{row.get('market_name')} T-{row.get('minutes_to_jump')}m "
                    f"coach={row.get('recommendation')} score={row.get('coach_score')}"
                )
            else:
                print("none")
            return 0

        return 2

    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
