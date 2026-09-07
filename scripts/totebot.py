#!/usr/bin/env python3

import json
import re
from pathlib import Path
from datetime import datetime, date
import sys
import subprocess

TB_VERSION = "1.7-betfair-names"

BASE = Path.home() / "botlab" / "totebot"
STATE = BASE / "state"
STATE.mkdir(parents=True, exist_ok=True)

ACTIVE = STATE / "active"
ACTIVE.mkdir(parents=True, exist_ok=True)

FINISHED = STATE / "finished"
FINISHED.mkdir(parents=True, exist_ok=True)

CURRENT = STATE / "current.txt"
HISTORY = STATE / "tote_history.jsonl"
RESULTS = STATE / "results.jsonl"
MORNING_LINE = STATE / "morning_line.jsonl"

# Synced Betfair observer files created by Basecamp and consumed on core7070.
BETFAIR_TARGET = STATE / "betfair_t15_target.json"
BETFAIR_WATCH_STATUS = STATE / "betfair_watch_status.json"
BETFAIR_ACTIVE_BOOK = STATE / "active_market_book.json"
BETFAIR_HISTORY = BASE / "history"

BETFAIR_SNAPSHOT_FILES = {
    "t15": "market_book_t15.json",
    "t10": "market_book_t10.json",
    "t5": "market_book_t5.json",
    "t2": "market_book_t2.json",
    "t30": "market_book_t30.json",
}

STAGES = ("opn", "tdy", "t15", "t10", "t5", "t2", "t30", "yrd", "jmp")
STAGE_LABELS = {
    "opn": "OPN",
    "tdy": "TDY",
    "t15": "T15",
    "t10": "T10",
    "t5": "T5",
    "t2": "T2",
    "t30": "T30",
    "yrd": "YRD",
    "jmp": "JMP",
}
STAGE_ALIASES = {
    "o": "opn",
    "open": "opn",
    "opn": "opn",
    "today": "tdy",
    "tdy": "tdy",
    "t15": "t15",
    "15": "t15",
    "t10": "t10",
    "10": "t10",
    "t5": "t5",
    "5": "t5",
    "t2": "t2",
    "2": "t2",
    "t30": "t30",
    "30s": "t30",
    "30": "t30",
    "yard": "yrd",
    "yrd": "yrd",
    "jump": "jmp",
    "jmp": "jmp",
}

SHAPE_MOVE_CODES = {
    "▲▲▲": "Steam",
    "▲▲▬": "Held",
    "▲▲▼": "Lost Momentum",
    "▲▬▲": "Persistence",
    "▲▬▬": "Settled Support",
    "▲▬▼": "Softening",
    "▲▼▲": "Recovery",
    "▲▼▬": "Checked",
    "▲▼▼": "Reversal",
    "▬▲▲": "Late Support",
    "▬▲▬": "Support Held",
    "▬▲▼": "False Move",
    "▬▬▲": "Late Lift",
    "▬▬▬": "Flat",
    "▬▬▼": "Late Drift",
    "▬▼▲": "Bounce",
    "▬▼▬": "Drift Held",
    "▬▼▼": "Slide",
    "▼▲▲": "Strong Recovery",
    "▼▲▬": "Stabilised",
    "▼▲▼": "Choppy",
    "▼▬▲": "Rebound",
    "▼▬▬": "Settled Lower",
    "▼▬▼": "Fade",
    "▼▼▲": "Rescue",
    "▼▼▬": "Weakening",
    "▼▼▼": "Collapse",
}


def cmd_version():
    print(f"TB {TB_VERSION}")


def now():
    return datetime.now().isoformat(timespec="seconds")


def make_race_id(track, race):
    return f"{track.upper()}_R{race}"


def race_file(race_id):
    return ACTIVE / f"{race_id}.json"


def finished_race_file(race_id):
    return FINISHED / f"{race_id}.json"


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_json(path):
    return json.loads(path.read_text())


def append_jsonl(path, row):
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def iter_jsonl(path):
    if not path.exists():
        return

    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        yield line_no, json.loads(line)


def set_current(race_id):
    CURRENT.write_text(race_id)


def get_current():
    if not CURRENT.exists():
        return None
    return CURRENT.read_text().strip() or None


def load_active_race(race_id=None, quiet=False):
    race_id = race_id or get_current()
    if not race_id:
        if not quiet:
            print("No active race selected.")
        return None

    path = race_file(race_id)
    if not path.exists():
        if not quiet:
            print(f"Race not found: {race_id}")
        return None

    return load_json(path)


def get_active_races():
    races = []

    for path in sorted(ACTIVE.glob("*.json")):
        races.append(load_json(path))

    def sort_key(race):
        jt = race.get("jump_time", "")
        if jt:
            return (0, jt)
        return (1, race["track"], str(race["race"]))

    return sorted(races, key=sort_key)


def normalize_stage(point):
    key = (point or "").strip().lower()
    return STAGE_ALIASES.get(key)


def stage_prompt():
    return "/".join(label.lower() for label in STAGE_LABELS.values())


def stage_label(point):
    point = normalize_stage(point) or point
    return STAGE_LABELS.get(point, str(point).upper())


def minutes_to_jump(ts, jump_time):
    if not jump_time:
        return None

    try:
        snap_dt = datetime.fromisoformat(ts)
        jump_dt = datetime.combine(
            snap_dt.date(),
            datetime.strptime(jump_time, "%H:%M").time()
        )
        return int((jump_dt - snap_dt).total_seconds() // 60)
    except Exception:
        return None


def shape_symbols(pts):
    values = [pts.get(stage) for stage in STAGES]
    symbols = []

    for a, b in zip(values, values[1:]):
        if a is None or b is None:
            symbols.append("?")
            continue

        pct = ((b - a) / a) * 100

        if pct <= -3:
            symbols.append("▲")
        elif pct >= 3:
            symbols.append("▼")
        else:
            symbols.append("▬")

    return "".join(symbols)


def shape_move_descriptor(shape):
    if len(shape) < 4:
        return ""

    code = shape[1:4]

    if "?" in code:
        return ""

    return SHAPE_MOVE_CODES.get(code, "")


def classify_curve(open_odds, mid_odds, late_odds):
    total_chg = ((late_odds - open_odds) / open_odds) * 100
    early_chg = ((mid_odds - open_odds) / open_odds) * 100
    late_chg = ((late_odds - mid_odds) / mid_odds) * 100

    if abs(total_chg) < 5:
        return "FLAT"
    if total_chg <= -30 and late_chg <= -15:
        return "LATE_FIRM"
    if total_chg <= -20 and early_chg <= -15:
        return "EARLY_FIRM"
    if total_chg <= -10:
        return "STEADY_FIRM"
    if total_chg >= 25:
        return "BLOWOUT"
    if total_chg >= 10:
        return "DRIFT"

    return "MIXED"


def result_count():
    if not RESULTS.exists():
        return 0

    return sum(
        1 for line in RESULTS.read_text().splitlines()
        if line.strip()
    )


def tb_milestone(count):
    msgs = {
        10: "🏇 First Meeting Complete",
        25: "🏇 Silver Card",
        50: "🏇 Half Century",
        100: "🏇💯 TB CENTURY",
        250: "🏇📈 Pattern Territory",
        500: "🏇👀 Veteran Watcher",
    }

    return msgs.get(count, "")


def load_morning_line(race_id):
    for _, row in iter_jsonl(MORNING_LINE) or []:
        if row.get("race_id") == race_id:
            return {
                r["tab"]: r["odds"]
                for r in row.get("runners", [])
            }

    return {}



def parse_betfair_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def jump_time_from_betfair(value):
    parsed = parse_betfair_time(value)
    if not parsed:
        return ""
    return parsed.strftime("%H:%M")


def race_no_from_market_name(market_name):
    match = re.search(r"\bR\s*([0-9]+)\b", market_name or "", re.I)
    if match:
        return match.group(1)
    return input("Race number not found. Race no: ").strip()


def betfair_market_from_target():
    target = load_json(BETFAIR_TARGET) if BETFAIR_TARGET.exists() else None
    if not target:
        return None, None
    market = target.get("market")
    if not isinstance(market, dict):
        return target, None
    return target, market


def betfair_latest_history_dir():
    if not BETFAIR_HISTORY.exists():
        return None

    dirs = [
        path for path in BETFAIR_HISTORY.glob("*/*")
        if path.is_dir() and (path / "capture_manifest.json").exists()
    ]

    if not dirs:
        return None

    return max(dirs, key=lambda p: p.stat().st_mtime)


def betfair_history_dir(market_id, market_start_time=None):
    if not market_id:
        return None

    parsed = parse_betfair_time(market_start_time)
    if parsed:
        candidate = BETFAIR_HISTORY / parsed.strftime("%Y-%m-%d") / market_id
        if candidate.is_dir():
            return candidate

    matches = sorted(
        BETFAIR_HISTORY.glob(f"*/*/{market_id}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def betfair_price_for_runner(runner):
    if runner.get("last_price_traded") is not None:
        return float(runner["last_price_traded"])

    for level_name in ("back_levels", "lay_levels", "traded_levels"):
        levels = runner.get(level_name)
        if isinstance(levels, list) and levels:
            price = levels[0].get("price")
            if price is not None:
                return float(price)

    return None


def betfair_runners_from_catalogue(market):
    runners = []
    for rank, runner in enumerate(market.get("runners", []), start=1):
        tab = str(runner.get("cloth_number") or rank)
        runners.append({
            "rank": rank,
            "tab": tab,
            "selection_id": runner.get("selection_id"),
            "runner_name": runner.get("runner_name", ""),
        })
    return runners


def betfair_runners_from_book(book):
    runners = []
    for rank, runner in enumerate(book.get("runners", []), start=1):
        odds = betfair_price_for_runner(runner)
        if odds is None:
            continue
        runners.append({
            "rank": rank,
            "tab": str(runner.get("cloth_number") or rank),
            "odds": odds,
            "selection_id": runner.get("selection_id"),
            "runner_name": runner.get("runner_name", ""),
            "matched": runner.get("total_matched"),
        })
    return runners


def remove_betfair_snapshots(race_id, points):
    if not HISTORY.exists():
        return

    kept = []
    for line in HISTORY.read_text().splitlines():
        if not line.strip():
            continue

        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue

        if (
            row.get("race_id") == race_id
            and normalize_stage(row.get("point")) in points
            and row.get("source") == "betfair"
        ):
            continue

        kept.append(json.dumps(row))

    HISTORY.write_text("\n".join(kept) + ("\n" if kept else ""))


def record_betfair_snapshot(race, point, book, source_file):
    runners = betfair_runners_from_book(book)
    if not runners:
        return False

    snap = {
        "timestamp": now(),
        "race_id": race["race_id"],
        "track": race["track"],
        "race": race["race"],
        "jump_time": race.get("jump_time", ""),
        "point": point,
        "source": "betfair",
        "market_id": book.get("market", {}).get("market_id"),
        "snapshot_slot": book.get("snapshot_slot"),
        "captured_at": book.get("captured_at"),
        "seconds_to_jump": book.get("market", {}).get("seconds_to_jump"),
        "source_file": str(source_file),
        "runners": runners,
    }

    append_jsonl(HISTORY, snap)
    return True


def load_betfair_books(history_dir):
    books = {}

    if history_dir:
        for point, filename in BETFAIR_SNAPSHOT_FILES.items():
            path = history_dir / filename
            book = read_json_if_exists(path)
            if book:
                books[point] = (book, path)

    # Fall back to the current synced book when the archive has not arrived yet.
    active_book = read_json_if_exists(BETFAIR_ACTIVE_BOOK)
    if active_book:
        point = normalize_stage(str(active_book.get("snapshot_slot", "")).replace("-", ""))
        if point in BETFAIR_SNAPSHOT_FILES and point not in books:
            books[point] = (active_book, BETFAIR_ACTIVE_BOOK)

    return books


def read_json_if_exists(path):
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def cmd_bf(market_id=None):
    """
    Create/select a ToteBot race from the latest Betfair armed target and
    import available T-15/T-10/T-5/T-2/T-30 snapshots into normal history.

    Manual morning-line and result workflows remain unchanged:
      tb ml ...
      tb snap opn/tdy/yrd/jmp
      tb result
    """
    target, market = betfair_market_from_target()

    if market_id:
        history_dir = betfair_history_dir(market_id)
        if not history_dir:
            print(f"No Betfair history found for market {market_id}")
            return

        manifest = read_json_if_exists(history_dir / "capture_manifest.json") or {}
        market = manifest.get("market", {})
        market["market_id"] = market_id
        market["market_start_time"] = manifest.get("market_start_time")
        target = {"status": "history", "market": market, "arm_id": f"betfair:{market_id}"}
    else:
        if not market:
            history_dir = betfair_latest_history_dir()
            if not history_dir:
                print("No Betfair armed target or history found.")
                return
            market_id = history_dir.name
            manifest = read_json_if_exists(history_dir / "capture_manifest.json") or {}
            market = manifest.get("market", {})
            market["market_id"] = market_id
            market["market_start_time"] = manifest.get("market_start_time")
            target = {"status": "latest-history", "market": market, "arm_id": f"betfair:{market_id}"}
        else:
            market_id = market.get("market_id")
            history_dir = betfair_history_dir(market_id, market.get("market_start_time"))

    track = (market.get("track") or market.get("event_name") or "BETFAIR").upper()
    race_no = race_no_from_market_name(market.get("market_name", ""))
    jump_time = jump_time_from_betfair(market.get("market_start_time"))

    race_id = make_race_id(track, race_no)
    runners = betfair_runners_from_catalogue(market)

    # If the target runner map is thin, use the latest book's runner map.
    books = load_betfair_books(history_dir)
    if not runners and books:
        latest_book = list(books.values())[-1][0]
        runners = [
            {
                "rank": i,
                "tab": str(r.get("cloth_number") or i),
                "selection_id": r.get("selection_id"),
                "runner_name": r.get("runner_name", ""),
            }
            for i, r in enumerate(latest_book.get("runners", []), start=1)
        ]

    race = {
        "race_id": race_id,
        "track": track,
        "race": race_no,
        "jump_time": jump_time,
        "created": now(),
        "source": "betfair",
        "market_id": market_id,
        "arm_id": target.get("arm_id"),
        "event_name": market.get("event_name", ""),
        "market_name": market.get("market_name", ""),
        "market_start_time": market.get("market_start_time", ""),
        "runners": runners,
    }

    save_json(race_file(race_id), race)
    set_current(race_id)

    points = set(books.keys())
    remove_betfair_snapshots(race_id, points)

    imported = []
    for point in ("t15", "t10", "t5", "t2", "t30"):
        item = books.get(point)
        if not item:
            continue
        book, path = item
        if record_betfair_snapshot(race, point, book, path):
            imported.append(stage_label(point))

    print(f"🏇 Betfair race loaded: {race_id}")
    if market_id:
        print(f"Market: {market_id}")
    if history_dir:
        print(f"History: {history_dir}")
    if imported:
        print("Imported:", ", ".join(imported))
    else:
        print("No Betfair snapshot books imported yet.")

    print()
    cmd_view()


def cmd_bf_status():
    target, market = betfair_market_from_target()
    status = read_json_if_exists(BETFAIR_WATCH_STATUS)
    active_book = read_json_if_exists(BETFAIR_ACTIVE_BOOK)

    print("\n🏇 BETFAIR SYNC STATUS")
    print("-" * 70)

    if status:
        print(f"Discovery : {status.get('status')} - {status.get('reason', '')}")
        next_market = status.get("next_market") or {}
        if next_market:
            print(
                "Next      : "
                f"{next_market.get('track')} {next_market.get('market_name')} "
                f"T-{next_market.get('seconds_to_jump')}s"
            )

    if market:
        print(f"Target    : {target.get('status')} {market.get('track')} {market.get('market_name')}")
        print(f"Market ID : {market.get('market_id')}")
        print(f"Jump      : {jump_time_from_betfair(market.get('market_start_time'))}")
        hdir = betfair_history_dir(market.get("market_id"), market.get("market_start_time"))
        print(f"History   : {hdir or 'not synced yet'}")

    if active_book:
        bmarket = active_book.get("market", {})
        print(
            "Book      : "
            f"{active_book.get('snapshot_slot')} "
            f"{bmarket.get('track')} {bmarket.get('market_name')} "
            f"matched={bmarket.get('total_matched')}"
        )

    print("-" * 70)


def snapshots_for_race(race_id):
    snaps = []

    for _, row in iter_jsonl(HISTORY) or []:
        if row.get("race_id") == race_id:
            point = normalize_stage(row.get("point"))
            if point:
                row = dict(row)
                row["point"] = point
            snaps.append(row)

    return snaps


def input_runners(expected=5):
    print(f"\nEnter top {expected} TAB numbers")
    bulk_tabs = input("TABs by spaces, or one-by-one: ").strip()

    if bulk_tabs:
        tab_values = bulk_tabs.replace(",", " ").split()
        if len(tab_values) != expected:
            print(f"Expected {expected} TAB numbers, got {len(tab_values)}.")
            return None
        return [
            {"rank": rank, "tab": tab}
            for rank, tab in enumerate(tab_values, start=1)
        ]

    runners = []
    for rank in range(1, expected + 1):
        tab = input(f"Rank {rank} TAB no: ").strip()
        runners.append({"rank": rank, "tab": tab})
    return runners


def collect_odds(race):
    runners = []
    tab_list = ", ".join(r["tab"] for r in race["runners"])
    print(f"Enter TAB Odds: {tab_list}")
    bulk = input("Spaces or One by One: ").strip()

    if bulk:
        odds_values = bulk.replace(",", " ").split()

        if len(odds_values) != len(race["runners"]):
            print(f"Expected {len(race['runners'])} odds values, got {len(odds_values)}.")
            return None

        for r, odds_text in zip(race["runners"], odds_values):
            runners.append({
                "rank": r["rank"],
                "tab": r["tab"],
                "odds": float(odds_text),
            })
        return runners

    for r in race["runners"]:
        rank = r["rank"]
        tab = r["tab"]
        odds = float(input(f"Rank {rank} TAB {tab} odds: ").strip())
        runners.append({
            "rank": rank,
            "tab": tab,
            "odds": odds,
        })

    return runners


def record_snapshot(race, point, runners):
    snap = {
        "timestamp": now(),
        "race_id": race["race_id"],
        "track": race["track"],
        "race": race["race"],
        "jump_time": race.get("jump_time", ""),
        "point": point,
        "runners": runners,
    }

    append_jsonl(HISTORY, snap)
    print(f"✅ {stage_label(point)} snapshot recorded")


def create_race(track=None, race=None, jump_time=None):
    track = track or input("Track: ").strip()
    race = race or input("Race number: ").strip()
    jump_time = jump_time or input("Jump time optional HH:MM: ").strip()
    runners = input_runners()

    if not runners:
        return None

    race_id = make_race_id(track, race)
    data = {
        "race_id": race_id,
        "track": track.upper(),
        "race": race,
        "jump_time": jump_time,
        "created": now(),
        "runners": runners,
    }

    save_json(race_file(race_id), data)
    set_current(race_id)
    print(f"🏇 Race prepared: {race_id}")
    return data


def cmd_prep(track=None, race=None, jump_time=None):
    race_data = create_race(track, race, jump_time)
    if not race_data:
        return

    for point in ("opn", "tdy"):
        answer = input(f"Record {stage_label(point)} odds now? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            continue
        runners = collect_odds(race_data)
        if runners:
            record_snapshot(race_data, point, runners)

    print()
    cmd_view()


def cmd_new(track=None, race=None, jump_time=None):
    create_race(track, race, jump_time)


def cmd_ml_summary(track):
    track = track.upper()

    print(f"\n🌅 MORNING LINE - {track}")
    print("-" * 65)
    print(f"{'Race':<8}{'Jump':<8}{'TABs / Odds'}")
    print("-" * 65)

    rows = [
        row for _, row in iter_jsonl(MORNING_LINE) or []
        if row.get("track") == track
    ]

    if not rows:
        print("No morning lines saved.")
        return

    rows.sort(key=lambda r: r.get("jump_time", ""))

    for row in rows:
        pairs = "  ".join(
            f"{r['tab']}@{r['odds']:.1f}"
            for r in row.get("runners", [])
        )
        print(f"R{row['race']:<7}{row.get('jump_time',''):<8}{pairs}")

    print("-" * 65)


def cmd_ml(track=None, race=None, jump_time=None):
    track = track or input("Track: ").strip()
    race = race or input("Race number: ").strip()
    jump_time = jump_time or input("Jump time optional HH:MM: ").strip()

    race_id = make_race_id(track, race)
    tabs = input("Morning line TABs: ").strip().replace(",", " ").split()
    odds_values = input("Morning line odds: ").strip().replace(",", " ").split()

    if len(tabs) != len(odds_values):
        print("TAB count and odds count do not match.")
        return

    runners = []
    for rank, (tab, odds) in enumerate(zip(tabs, odds_values), start=1):
        runners.append({
            "rank": rank,
            "tab": tab,
            "odds": float(odds),
        })

    row = {
        "timestamp": now(),
        "race_id": race_id,
        "track": track.upper(),
        "race": race,
        "jump_time": jump_time,
        "runners": runners,
    }

    append_jsonl(MORNING_LINE, row)
    print(f"🌅 Morning line saved: {track.upper()} R{race}")


def cmd_edit():
    race = load_active_race()

    if not race:
        return

    print()
    print(f"{race['track']} R{race['race']}")
    print(f"Jump : {race.get('jump_time','')}")
    print("TABs :", " ".join(r["tab"] for r in race["runners"]))

    choice = input("\nEdit jump/tabs: ").strip().lower()

    if choice == "jump":
        race["jump_time"] = input("New jump time: ").strip()
    elif choice == "tabs":
        vals = input("New TABs: ").strip().replace(",", " ").split()
        if len(vals) != len(race["runners"]):
            print(f"Expected {len(race['runners'])} TABs, got {len(vals)}.")
            return

        for runner, tab in zip(race["runners"], vals):
            runner["tab"] = tab
    else:
        print("Unknown edit field.")
        return

    save_json(race_file(race["race_id"]), race)
    print("✅ Updated")


def cmd_snap(point=None):
    race = load_active_race(quiet=True)

    if not race:
        print("No active race. Run: tb prep")
        return

    raw_point = point or input(f"Stage {stage_prompt()}: ")
    point = normalize_stage(raw_point)
    if not point:
        print(f"Unknown stage: {raw_point}")
        return

    print(f"\nSnapshot for {race['track']} R{race['race']} - {stage_label(point)}")
    print("Enter odds only.\n")

    runners = collect_odds(race)
    if not runners:
        return

    record_snapshot(race, point, runners)
    cmd_view()


def cmd_result(finalise=False):
    race = load_active_race(quiet=True)

    if not race:
        print("No active race selected.")
        return

    winner = input(f"Winner TAB no for {race['track']} R{race['race']}: ").strip()
    result = {
        "timestamp": now(),
        "race_id": race["race_id"],
        "track": race["track"],
        "race": race["race"],
        "jump_time": race.get("jump_time", ""),
        "winner": winner,
    }

    count = result_count() + 1
    append_jsonl(RESULTS, result)

    print(f"🏆 {race['track']} R{race['race']} winner recorded: TAB {winner}")

    milestone = tb_milestone(count)
    if milestone:
        print()
        print(milestone)

    if finalise:
        src = race_file(race["race_id"])
        dst = finished_race_file(race["race_id"])

        if src.exists():
            src.rename(dst)

        print(f"✅ Finalised: {race['race_id']} removed from active list")

        if get_current() == race["race_id"]:
            select_next_active_race()


def cmd_list(select=False):
    races = get_active_races()
    current = get_current()

    print("\n🏇 ACTIVE RACES")
    print("-" * 50)

    if not races:
        print("No active races.")
        return

    for i, race in enumerate(races, start=1):
        race_id = race["race_id"]
        marker = "* " if race_id == current else "  "
        jump = race.get("jump_time", "")

        if jump:
            print(f"{i:<2} {marker}{race['track']} R{race['race']}  {jump}")
        else:
            print(f"{i:<2} {marker}{race['track']} R{race['race']}")

    print("-" * 50)

    if not select:
        return

    choice = input("Select race number, or Enter to keep current: ").strip()

    if not choice:
        print("Keeping current race.")
        return

    new_from_selection = choice.lower().endswith("n")
    if new_from_selection:
        choice = choice[:-1]

    try:
        idx = int(choice)
    except ValueError:
        print("Invalid selection.")
        return

    if idx < 1 or idx > len(races):
        print("Selection out of range.")
        return

    selected = races[idx - 1]

    if new_from_selection:
        track = selected["track"]
        print(f"\n🏇 New race for {track}")
        race_no = input("Race no: ").strip()
        jump_time = input("Jump time optional HH:MM: ").strip()
        cmd_prep(track, race_no, jump_time)
        return

    set_current(selected["race_id"])
    print(f"🏇 Using race: {selected['race_id']}")
    cmd_view()


def cmd_curve():
    race = load_active_race(quiet=True)

    if not race:
        print("No active race selected.")
        return

    snaps = snapshots_for_race(race["race_id"])
    if not snaps:
        print(f"No snapshots yet for {race['race_id']}")
        return

    jump_time = race.get("jump_time", "")
    by_tab = {}

    for snap in snaps:
        mtj = minutes_to_jump(snap["timestamp"], jump_time)
        for r in snap["runners"]:
            tab = r["tab"]
            by_tab.setdefault(tab, []).append({
                "timestamp": snap["timestamp"],
                "mtj": mtj,
                "odds": r["odds"],
                "rank": r["rank"],
            })

    def closest(points, target):
        valid = [p for p in points if p["mtj"] is not None]
        if not valid:
            return None
        return min(valid, key=lambda p: abs(p["mtj"] - target))

    print(f"\n🏇 CURVE - {race['track']} R{race['race']}")
    if jump_time:
        print(f"⏰ Jump: {jump_time}")

    print("-" * 64)
    print(f"{'TAB':<6}{'Open':<8}{'T-10':<8}{'Late':<8}{'Shape':<14}{'Late@'}")
    print("-" * 64)

    for tab, points in sorted(by_tab.items(), key=lambda x: x[1][0]["rank"]):
        points = sorted(points, key=lambda p: p["timestamp"])

        open_p = points[0]
        mid_p = closest(points, 10) or points[len(points) // 2]
        late_p = closest(points, 2) or points[-1]
        shape = classify_curve(open_p["odds"], mid_p["odds"], late_p["odds"])

        late_at = ""
        if late_p["mtj"] is not None:
            late_at = f"T-{late_p['mtj']}"

        print(
            f"{tab:<6}"
            f"{open_p['odds']:<8.2f}"
            f"{mid_p['odds']:<8.2f}"
            f"{late_p['odds']:<8.2f}"
            f"{shape:<14}"
            f"{late_at}"
        )

    print("-" * 64)


def cmd_view():
    race = load_active_race(quiet=True)

    if not race:
        print("No active race selected.")
        return

    snaps = snapshots_for_race(race["race_id"])

    if not snaps:
        print(f"No snapshots yet for {race['race_id']}")
        return

    first_seen = {}
    latest = {}
    point_odds = {}

    runner_names = {
        str(r.get("tab")): r.get("runner_name", "")
        for r in race.get("runners", [])
        if r.get("tab") is not None
    }

    for snap in snaps:
        point = normalize_stage(snap.get("point"))

        for r in snap["runners"]:
            tab = str(r["tab"])

            if r.get("runner_name") and not runner_names.get(tab):
                runner_names[tab] = r.get("runner_name", "")

            if point:
                point_odds.setdefault(tab, {})[point] = r["odds"]

            if tab not in first_seen:
                first_seen[tab] = {
                    "odds": r["odds"],
                    "rank": r["rank"],
                }

            latest[tab] = {
                "odds": r["odds"],
                "rank": r["rank"],
            }

    top5_total = sum(1 / r["odds"] for r in latest.values() if r.get("odds"))
    if top5_total <= 0:
        top5_total = 1

    print(f"\n🏇 TOTEBOARD WATCH - {race['track']} R{race['race']}")
    if race.get("jump_time"):
        print(f"⏰ Jump: {race['jump_time']}")
    if race.get("market_id"):
        print(f"🔁 Betfair: {race['market_id']}  {race.get('market_name', '')}")

    stage_cols = list(STAGES)
    name_width = 22
    width = 8 + name_width + (len(stage_cols) * 7) + 34
    print("-" * width)

    header = f"{'TAB':<6}{'Horse':<{name_width}}"
    for point in stage_cols:
        header += f"{stage_label(point):<7}"
    header += f"{'Δ%':<8}{'!':<2}{'5%':<7}{'Shape':<12}{'Move'}"
    print(header)
    print("-" * width)

    def pct_change_for_item(item):
        tab, now_data = item
        open_odds = first_seen[tab]["odds"]
        now_odds = now_data["odds"]
        return ((now_odds - open_odds) / open_odds) * 100

    for tab, now_data in sorted(latest.items(), key=pct_change_for_item):
        open_data = first_seen[tab]
        open_odds = open_data["odds"]
        latest_odds = now_data["odds"]
        share5 = (1 / latest_odds) / top5_total * 100
        pts = point_odds.get(tab, {})

        base_odds = pts.get("opn") or open_odds
        display_odds = latest_odds
        for point in reversed(stage_cols):
            if pts.get(point) is not None:
                display_odds = pts[point]
                break

        pct_chg = ((display_odds - base_odds) / base_odds) * 100
        pct_flag = "#" if pct_chg < -50 or pct_chg > 30 else ""
        shape = shape_symbols(pts)
        move = shape_move_descriptor(shape)

        def fmt_point(name):
            val = pts.get(name)
            return f"{val:.1f}" if val is not None else ""

        horse_name = (runner_names.get(tab) or "")[:name_width - 1]
        row = f"{tab:<6}{horse_name:<{name_width}}"
        for point in stage_cols:
            row += f"{fmt_point(point):<7}"
        row += (
            f"{pct_chg:<8.1f}"
            f"{pct_flag:<2}"
            f"{share5:<7.1f}"
            f"{shape[:11]:<12}"
            f"{move}"
        )
        print(row)

    print("-" * width)

    line = next_race_line()
    if line:
        print(line)


def cmd_reset():
    if CURRENT.exists():
        CURRENT.unlink()
    print("Race reset. History kept.")


def cmd_go(track=None, race=None, jump_time=None, stages=None):
    race_data = create_race(track, race, jump_time)
    if not race_data:
        return

    stages = stages or ("opn", "tdy")

    for point in stages:
        print()
        print(f"{stage_label(point)} snapshot")
        runners = collect_odds(race_data)
        if runners:
            record_snapshot(race_data, point, runners)

    print()
    cmd_view()


def parse_go_args(args):
    track = args[0] if len(args) > 0 else None
    race = args[1] if len(args) > 1 else None
    jump_time = None
    stages = ("opn", "tdy")
    rest = list(args[2:])

    if rest:
        stage = normalize_stage(rest[-1])
        if stage:
            stages = (stage,)
            rest = rest[:-1]

    if rest:
        jump_time = rest[0]

    return track, race, jump_time, stages


def cmd_use(track, race):
    race_id = make_race_id(track, race)
    if not race_file(race_id).exists():
        print(f"Race not found: {race_id}")
        return
    set_current(race_id)
    print(f"🏇 Using race: {race_id}")


def cmd_rebuild_active():
    finished_ids = set()

    for _, row in iter_jsonl(RESULTS) or []:
        finished_ids.add(row["race_id"])

    history_ids = {}

    for _, snap in iter_jsonl(HISTORY) or []:
        race_id = snap["race_id"]

        if race_id not in history_ids:
            history_ids[race_id] = {
                "race_id": race_id,
                "track": snap["track"],
                "race": snap["race"],
                "jump_time": snap.get("jump_time", ""),
                "created": snap["timestamp"],
                "runners": [
                    {"rank": r["rank"], "tab": r["tab"]}
                    for r in snap["runners"]
                ],
            }

        if snap.get("jump_time") and not history_ids[race_id].get("jump_time"):
            history_ids[race_id]["jump_time"] = snap["jump_time"]

    restored = 0

    for race_id, race in history_ids.items():
        if race_id in finished_ids:
            continue

        save_json(race_file(race_id), race)
        restored += 1

    print(f"Rebuilt {restored} unfinished active races from history.")


def select_next_active_race():
    races = get_active_races()

    if not races:
        if CURRENT.exists():
            CURRENT.unlink(missing_ok=True)
        print("No active races remaining.")
        return

    next_race = races[0]
    set_current(next_race["race_id"])
    print(f"➡️ Next active race: {next_race['track']} R{next_race['race']}")


def next_race_line():
    now_dt = datetime.now()
    upcoming = []

    for path in ACTIVE.glob("*.json"):
        race = load_json(path)
        jt = race.get("jump_time")

        if not jt:
            continue

        try:
            jump_dt = datetime.combine(
                date.today(),
                datetime.strptime(jt, "%H:%M").time()
            )
        except ValueError:
            continue

        mins = int((jump_dt - now_dt).total_seconds() // 60)

        if mins >= 0:
            upcoming.append((mins, race))

    if not upcoming:
        return None

    mins, race = sorted(upcoming, key=lambda x: x[0])[0]
    return f"Next: {race['track']} R{race['race']} in {mins} min"


def cmd_changes():
    subprocess.run(["git", "status", "--short"], check=False)


def cmd_doctor():
    problems = 0

    for path in ACTIVE.glob("*.json"):
        try:
            race = load_json(path)
        except json.JSONDecodeError as exc:
            print(f"BAD active JSON: {path} ({exc})")
            problems += 1
            continue

        missing = [key for key in ("race_id", "track", "race", "runners") if key not in race]
        if missing:
            print(f"BAD active race {path.name}: missing {', '.join(missing)}")
            problems += 1

    for path in (HISTORY, RESULTS, MORNING_LINE):
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"BAD jsonl {path.name}:{line_no}: {exc}")
                problems += 1

    current = get_current()
    if current and not race_file(current).exists():
        print(f"STALE current race: {current}")
        problems += 1

    print(f"Active races: {len(get_active_races())}")
    print(f"Results: {result_count()}")

    if problems:
        print(f"Doctor found {problems} problem(s).")
    else:
        print("Doctor found no problems.")


def cmd_save(message=None):
    message = message or input("Commit message: ").strip()

    if not message:
        print("No commit message.")
        return

    subprocess.run(["git", "add", "totebot.py"], check=False)
    subprocess.run(["git", "add", ".gitignore"], check=False)

    result = subprocess.run(
        ["git", "commit", "-m", message],
        text=True
    )

    if result.returncode == 0:
        print("✅ TB saved to git")
    else:
        print("⚠ Git commit may have failed or nothing changed.")


def cmd_push():
    subprocess.run(["git", "push"], check=False)


def usage():
    print("Usage: tb prep | snap | view | curve | result | edit | bf | bfstatus | save | changes | doctor")
    print("       tb go TRACK RACE [JUMP] [STAGE]")


def main():
    if len(sys.argv) < 2:
        usage()
        return

    cmd = sys.argv[1]

    if cmd == "prep":
        track = sys.argv[2] if len(sys.argv) > 2 else None
        race = sys.argv[3] if len(sys.argv) > 3 else None
        jump_time = sys.argv[4] if len(sys.argv) > 4 else None
        cmd_prep(track, race, jump_time)
    elif cmd == "new":
        cmd_new()
    elif cmd == "snap":
        point = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_snap(point)
    elif cmd == "view":
        cmd_view()
    elif cmd == "curve":
        cmd_curve()
    elif cmd == "result":
        finalise = len(sys.argv) > 2 and sys.argv[2] in ("final", "done", "f")
        cmd_result(finalise)
    elif cmd in ("edit", "e"):
        cmd_edit()
    elif cmd == "save":
        message = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        cmd_save(message)
    elif cmd == "changes":
        cmd_changes()
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "reset":
        cmd_reset()
    elif cmd == "go":
        track, race, jump_time, stages = parse_go_args(sys.argv[2:])
        cmd_go(track, race, jump_time, stages)
    elif cmd == "use":
        if len(sys.argv) < 4:
            print("Usage: tb use TRACK RACE")
            return
        cmd_use(sys.argv[2], sys.argv[3])
    elif cmd == "list":
        cmd_list()
    elif cmd == "select":
        cmd_list(select=True)
    elif cmd == "version":
        cmd_version()
    elif cmd == "rebuild":
        cmd_rebuild_active()
    elif cmd in ("bf", "betfair"):
        market_id = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_bf(market_id)
    elif cmd in ("bfstatus", "bfs"):
        cmd_bf_status()
    elif cmd == "ml":
        track = sys.argv[2] if len(sys.argv) > 2 else None
        race = sys.argv[3] if len(sys.argv) > 3 else None
        jump_time = sys.argv[4] if len(sys.argv) > 4 else None
        cmd_ml(track, race, jump_time)
    elif cmd == "mlsum":
        if len(sys.argv) < 3:
            print("Usage: tb mlsum TRACK")
            return
        cmd_ml_summary(sys.argv[2])
    elif cmd == "push":
        cmd_push()
    else:
        print("Unknown command")
        usage()


if __name__ == "__main__":
    main()
