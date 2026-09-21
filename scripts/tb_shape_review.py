#!/usr/bin/env python3
"""
ToteBot Shape Review

Discovery lens for runner market shapes across ToteBot history.

Reads:
    ~/botlab/totebot/history/YYYY-MM-DD/<market_id>/

Snapshots:
    T15 -> T10 -> T5 -> T2 -> T30

For each runner:
- shape symbols (▲ drift, ▼ firm, ▬ flat)
- total percentage move
- first/last observed price
- first observed market rank
- top-half flag
- early slope (T15 -> T5 where available)
- late slope (T5 -> T30 where available)
- quadratic fit to log(price) vs capture time (flagged nominal fallback)
- R² fit quality
- simple regime label

This is an observer/review tool. It does not select winners or place bets.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from tb_race_lifecycle import scratching_break
from tb_runner_shape import (VERSION as ANALYSIS_VERSION, shape_symbols, shape_class,
                             price_with_source, parse_time, movement_directions)
from pathlib import Path
from typing import Any


DEFAULT_HISTORY_DIR = Path.home() / "botlab" / "totebot" / "history"

STAGES = ("T15", "T10", "T5", "T2", "T30")

SNAPSHOT_FILES = {
    "T15": "market_book_t15.json",
    "T10": "market_book_t10.json",
    "T5": "market_book_t5.json",
    "T2": "market_book_t2.json",
    "T30": "market_book_t30.json",
}

STAGE_MINUTES = {
    "T15": -15.0,
    "T10": -10.0,
    "T5": -5.0,
    "T2": -2.0,
    "T30": -0.5,
}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        value = float(value)
        if math.isfinite(value) and value > 0:
            return value
    return None


def runner_key(runner: dict[str, Any]) -> str:
    sid = runner.get("selection_id") or runner.get("selectionId")
    if sid is not None:
        return f"sid:{sid}"
    cloth = runner.get("cloth_number") or runner.get("clothNumber") or ""
    name = runner.get("runner_name") or runner.get("runnerName") or ""
    return f"cloth:{cloth}:{name}"


def runner_name(runner: dict[str, Any]) -> str:
    return str(
        runner.get("runner_name")
        or runner.get("runnerName")
        or runner.get("selection_id")
        or runner.get("selectionId")
        or "UNKNOWN"
    )


def runner_no(runner: dict[str, Any]) -> str:
    return str(runner.get("cloth_number") or runner.get("clothNumber") or "")


def price_from_runner(runner: dict[str, Any]) -> float | None:
    return price_with_source(runner)[0]


def load_race_snapshots(
    race_dir: Path,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    by_stage: dict[str, dict[str, dict[str, Any]]] = {}
    market_meta: dict[str, Any] = {}

    for stage in STAGES:
        snapshot = read_json(race_dir / SNAPSHOT_FILES[stage])
        if not snapshot:
            continue

        if not market_meta and isinstance(snapshot.get("market"), dict):
            market_meta = snapshot["market"]

        stage_runners: dict[str, dict[str, Any]] = {}
        runners = snapshot.get("runners")
        if not isinstance(runners, list):
            continue

        for runner in runners:
            if isinstance(runner, dict):
                stage_runners[runner_key(runner)] = runner

        by_stage[stage] = stage_runners

    return by_stage, market_meta


def shape_symbol(previous: float, current: float) -> str:
    if current > previous:
        return "▲"
    if current < previous:
        return "▼"
    return "▬"


def build_shape(stage_prices: dict[str, float]) -> str:
    return shape_symbols(stage_prices)


def total_move_pct(
    stage_prices: dict[str, float],
) -> tuple[float | None, float | None, float | None]:
    values = [stage_prices[s] for s in STAGES if s in stage_prices]

    if len(values) < 2:
        return None, values[0] if values else None, values[-1] if values else None

    first = values[0]
    last = values[-1]
    return ((last - first) / first) * 100.0, first, last


def slope_between(
    stage_prices: dict[str, float],
    stage_a: str,
    stage_b: str,
    times: dict[str, float] | None = None,
) -> float | None:
    p1 = stage_prices.get(stage_a)
    p2 = stage_prices.get(stage_b)

    if p1 is None or p2 is None:
        return None

    times = STAGE_MINUTES if times is None else times
    if stage_a not in times or stage_b not in times:
        return None
    x1, x2 = times[stage_a], times[stage_b]
    if x2 <= x1:
        return None

    return (math.log(p2) - math.log(p1)) / (x2 - x1)


def solve_3x3(
    matrix: list[list[float]],
    vector: list[float],
) -> tuple[float, float, float] | None:
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]

    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(a[row][col]))

        if abs(a[pivot][col]) < 1e-12:
            return None

        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]

        divisor = a[col][col]
        for j in range(col, 4):
            a[col][j] /= divisor

        for row in range(3):
            if row == col:
                continue

            factor = a[row][col]
            for j in range(col, 4):
                a[row][j] -= factor * a[col][j]

    return a[0][3], a[1][3], a[2][3]


def quadratic_fit(stage_prices: dict[str, float], times: dict[str, float] | None = None) -> dict[str, float] | None:
    times = STAGE_MINUTES if times is None else times
    points = [
        (times[stage], math.log(stage_prices[stage]))
        for stage in STAGES
        if stage in stage_prices and stage in times
    ]

    if len(points) < 4:
        return None

    n = float(len(points))
    sx = sum(x for x, _ in points)
    sx2 = sum(x * x for x, _ in points)
    sx3 = sum(x ** 3 for x, _ in points)
    sx4 = sum(x ** 4 for x, _ in points)
    sy = sum(y for _, y in points)
    sxy = sum(x * y for x, y in points)
    sx2y = sum((x * x) * y for x, y in points)

    solved = solve_3x3(
        [
            [n, sx, sx2],
            [sx, sx2, sx3],
            [sx2, sx3, sx4],
        ],
        [sy, sxy, sx2y],
    )

    if solved is None:
        return None

    a, b, c = solved
    observed = [y for _, y in points]
    mean_y = sum(observed) / len(observed)
    ss_tot = sum((y - mean_y) ** 2 for y in observed)
    ss_res = sum((y - (a + b * x + c * x * x)) ** 2 for x, y in points)

    if ss_tot <= 1e-15:
        r2 = 1.0 if ss_res <= 1e-15 else 0.0
    else:
        r2 = max(0.0, min(1.0, 1.0 - ss_res / ss_tot))

    start_x = min(x for x, _ in points)
    end_x = max(x for x, _ in points)

    return {
        "a": a,
        "b": b,
        "c": c,
        "r2": r2,
        "start_slope": b + 2.0 * c * start_x,
        "end_slope": b + 2.0 * c * end_x,
    }


def slope_state(value: float | None, epsilon: float = 0.003) -> str:
    if value is None:
        return "UNKNOWN"
    if value > epsilon:
        return "DRIFT"
    if value < -epsilon:
        return "FIRM"
    return "FLAT"


def fit_quality(r2: float | None) -> str:
    if r2 is None:
        return "NO_FIT"
    if r2 >= 0.90:
        return "CLEAN"
    if r2 >= 0.70:
        return "MIXED"
    return "NOISY"


def curve_label(fit: dict[str, float] | None, epsilon: float = 0.003) -> str:
    if not fit:
        return "NO_FIT"

    start = fit["start_slope"]
    end = fit["end_slope"]

    start_state = slope_state(start, epsilon)
    end_state = slope_state(end, epsilon)

    if start_state == "FLAT" and end_state == "FLAT":
        return "STRAIGHT"

    if (
        start_state in ("DRIFT", "FIRM")
        and end_state in ("DRIFT", "FIRM")
        and start_state != end_state
    ):
        return "REVERSING"

    if start_state == "FLAT" and end_state != "FLAT":
        return "LATE_MOVE"

    if start_state != "FLAT" and end_state == "FLAT":
        return "FADING"

    if abs(end) > abs(start) * 1.35:
        return "ACCELERATING"

    if abs(end) < abs(start) * 0.74:
        return "DECELERATING"

    return "STRAIGHT"


def regime_label(
    early_slope: float | None,
    late_slope: float | None,
    epsilon: float = 0.003,
) -> str:
    early = slope_state(early_slope, epsilon)
    late = slope_state(late_slope, epsilon)

    if early == "UNKNOWN" or late == "UNKNOWN":
        return "PARTIAL"
    if early == "FLAT" and late == "FLAT":
        return "ANCHORED"
    if early == "FLAT" and late == "DRIFT":
        return "LATE_DRIFT"
    if early == "FLAT" and late == "FIRM":
        return "LATE_FIRM"
    if early == "DRIFT" and late == "FIRM":
        return "LATE_REVERSAL_FIRM"
    if early == "FIRM" and late == "DRIFT":
        return "LATE_REVERSAL_DRIFT"

    if early == late and early in ("DRIFT", "FIRM"):
        if abs(late_slope or 0.0) > abs(early_slope or 0.0) * 1.35:
            return f"{early}_ACCEL"
        if abs(late_slope or 0.0) < abs(early_slope or 0.0) * 0.74:
            return f"{early}_FADING"
        return f"{early}_STEADY"

    return "MIXED"


def first_stage_rank(
    stage_prices_by_runner: dict[str, dict[str, float]],
) -> dict[str, tuple[int, int, bool]]:
    first_stage = None

    for stage in STAGES:
        count = sum(1 for prices in stage_prices_by_runner.values() if stage in prices)
        if count >= 2:
            first_stage = stage
            break

    if first_stage is None:
        return {}

    ranked = sorted(
        (
            (key, prices[first_stage])
            for key, prices in stage_prices_by_runner.items()
            if first_stage in prices
        ),
        key=lambda item: item[1],
    )

    field = len(ranked)
    top_half_cutoff = (field + 1) // 2

    return {
        key: (rank, field, rank <= top_half_cutoff)
        for rank, (key, _) in enumerate(ranked, start=1)
    }


def race_runner_records(race_dir: Path) -> list[dict[str, Any]]:
    by_stage, market = load_race_snapshots(race_dir)

    if not by_stage:
        return []

    runner_meta: dict[str, dict[str, Any]] = {}
    stage_prices_by_runner: dict[str, dict[str, float]] = defaultdict(dict)

    for stage in STAGES:
        for key, runner in by_stage.get(stage, {}).items():
            runner_meta.setdefault(key, runner)
            price = price_from_runner(runner)
            if price is not None:
                stage_prices_by_runner[key][stage] = price

    snapshots = {stage: read_json(race_dir / filename) for stage, filename in SNAPSHOT_FILES.items()}
    price_break = scratching_break([snapshots[s] for s in STAGES])
    rank_info = first_stage_rank(stage_prices_by_runner)
    records: list[dict[str, Any]] = []

    for key, prices in stage_prices_by_runner.items():
        if len(prices) < 2:
            continue

        meta = runner_meta.get(key, {})
        move_pct, first_price, last_price = total_move_pct(prices)

        captured = {stage: (snapshots.get(stage) or {}).get("captured_at") for stage in prices}
        parsed = {stage: parse_time(value) for stage, value in captured.items()}
        ordered_times = [parsed[stage] for stage in STAGES if stage in prices]
        complete_times = all(value is not None for value in ordered_times)
        increasing = complete_times and all(b > a for a, b in zip(ordered_times, ordered_times[1:]))
        flags = []
        if not complete_times:
            flags.append("nominal_time_fallback")
        elif not increasing:
            flags.append("non_monotonic_capture_times")
        times = ({stage: (value - ordered_times[0]).total_seconds() / 60
                  for stage, value in parsed.items()} if increasing else
                 {} if complete_times else STAGE_MINUTES)
        sources = {stage: price_with_source(by_stage[stage][key])[1] for stage in prices}
        if len(set(sources.values())) > 1:
            flags.append("mixed_price_sources")
        if len(prices) < len(STAGES):
            flags.append("missing_stages")
        if any((snapshots.get(stage) or {}).get("market", {}).get("is_market_data_delayed") for stage in prices):
            flags.append("delayed_feed")
        if price_break:
            flags.append("scratching_break")
        early_slope = slope_between(prices, "T15", "T5", times)
        if early_slope is None:
            early_slope = slope_between(prices, "T15", "T10", times)

        late_slope = slope_between(prices, "T5", "T30", times)
        if late_slope is None:
            late_slope = slope_between(prices, "T2", "T30", times)

        fit = quadratic_fit(prices, times)
        if price_break:
            move_pct = early_slope = late_slope = fit = None
        rank, field, top_half = rank_info.get(key, (None, None, None))

        directions = [d for d in movement_directions(prices) if d]
        reversal_count = sum(a != b for a, b in zip(directions, directions[1:]))
        records.append(
            {
                "reversal_count": None if price_break else reversal_count,
                "analysis_version": ANALYSIS_VERSION,
                "timing_basis": "scheduled",
                "slope_time_basis": "captured_at" if increasing else "invalid" if complete_times else "nominal_stage_offsets",
                "captured_at": captured,
                "price_sources": sources,
                "quality_flags": flags,
                "coverage": [stage for stage in STAGES if stage in prices],
                "missing_stages": [stage for stage in STAGES if stage not in prices],
                "shape_class": "scratching_break" if price_break else shape_class(prices),
                "date": race_dir.parent.name,
                "market_id": str(market.get("market_id") or race_dir.name),
                "track": str(market.get("track") or "UNKNOWN"),
                "country": str(
                    market.get("country_code")
                    or market.get("country")
                    or "UNKNOWN"
                ),
                "state": str(
                    market.get("state")
                    or market.get("region")
                    or "UNKNOWN"
                ),
                "market_name": str(market.get("market_name") or ""),
                "runner_key": key,
                "runner_no": runner_no(meta),
                "runner_name": runner_name(meta),
                "stage_prices": dict(prices),
                "points": len(prices),
                "shape": "SCRATCHING_BREAK" if price_break else build_shape(prices),
                "first_price": first_price,
                "last_price": last_price,
                "move_pct": move_pct,
                "first_rank": rank,
                "field_size": field,
                "top_half": top_half,
                "early_slope": early_slope,
                "late_slope": late_slope,
                "regime": "SCRATCHING_BREAK" if price_break else regime_label(early_slope, late_slope),
                "curve": curve_label(fit),
                "fit_r2": fit["r2"] if fit else None,
                "fit_quality": fit_quality(fit["r2"] if fit else None),
                "quad_c": fit["c"] if fit else None,
            }
        )

    return records


def discover_runner_records(history_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for day_dir in sorted(history_dir.iterdir()):
        if not day_dir.is_dir():
            continue

        for race_dir in sorted(day_dir.iterdir()):
            if race_dir.is_dir():
                records.extend(race_runner_records(race_dir))

    return records


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def aggregate_shapes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        grouped[(record["shape"], tuple(record["coverage"]))].append(record)

    rows: list[dict[str, Any]] = []

    for (shape, coverage), group in grouped.items():
        races = len({(r["date"], r["market_id"]) for r in group})
        moves = [float(r["move_pct"]) for r in group if r["move_pct"] is not None]
        r2_values = [float(r["fit_r2"]) for r in group if r["fit_r2"] is not None]

        top_half_known = [r for r in group if isinstance(r["top_half"], bool)]
        top_half_count = sum(1 for r in top_half_known if r["top_half"])
        clean_count = sum(1 for r in group if r["fit_quality"] == "CLEAN")
        reversal_count = sum(
            1 for r in group
            if (r["reversal_count"] or 0) > 0
        )

        regime_counts = Counter(r["regime"] for r in group)
        curve_counts = Counter(r["curve"] for r in group)

        rows.append(
            {
                "shape": shape,
                "coverage": list(coverage),
                "runners": len(group),
                "races": races,
                "avg_move": mean(moves),
                "avg_r2": mean(r2_values),
                "clean_pct": pct(clean_count, len(group)),
                "reversal_pct": pct(reversal_count, len(group)),
                "top_half_pct": (
                    pct(top_half_count, len(top_half_known))
                    if top_half_known else None
                ),
                "common_regime": regime_counts.most_common(1)[0][0] if regime_counts else "—",
                "common_curve": curve_counts.most_common(1)[0][0] if curve_counts else "—",
            }
        )

    rows.sort(key=lambda row: (-row["runners"], row["shape"]))
    return rows


def fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def print_shape_table(rows: list[dict[str, Any]], limit: int) -> None:
    print()
    print("TOTE BOT — SHAPE REVIEW")
    print()
    print(
        f"{'SHAPE':16} {'COVERAGE':22} {'RUNNERS':>7} {'RACES':>6} {'MOVE%':>8} "
        f"{'TOP½%':>7} {'CLEAN%':>7} {'REV%':>7} "
        f"{'REGIME':22} {'CURVE':14}"
    )
    print("-" * 96)

    for row in rows[:limit]:
        top_half = "—" if row["top_half_pct"] is None else f"{row['top_half_pct']:.1f}"
        print(
            f"{row['shape']:16} "
            f"{','.join(row['coverage']):22} "
            f"{row['runners']:>7} "
            f"{row['races']:>6} "
            f"{fmt(row['avg_move']):>8} "
            f"{top_half:>7} "
            f"{row['clean_pct']:>7.1f} "
            f"{row['reversal_pct']:>7.1f} "
            f"{row['common_regime'][:22]:22} "
            f"{row['common_curve'][:14]:14}"
        )


def print_shape_details(
    records: list[dict[str, Any]],
    wanted_shape: str,
    limit: int,
) -> None:
    matching = [r for r in records if r["shape"] == wanted_shape]

    matching.sort(
        key=lambda r: (
            r["fit_quality"] != "CLEAN",
            -(r["fit_r2"] or 0.0),
            -(abs(r["move_pct"] or 0.0)),
        )
    )

    print()
    print(f"SHAPE DETAIL — {wanted_shape}")
    print()
    print(
        f"{'DATE':10} {'TRACK':18} {'RUNNER':24} {'RANK':>6} "
        f"{'FIRST':>7} {'LAST':>7} {'MOVE%':>8} {'R²':>6} {'REGIME':20}"
    )
    print("-" * 113)

    for r in matching[:limit]:
        rank = (
            f"{r['first_rank']}/{r['field_size']}"
            if r["first_rank"] is not None and r["field_size"] is not None
            else "—"
        )
        print(
            f"{r['date'][:10]:10} "
            f"{r['track'][:18]:18} "
            f"{r['runner_name'][:24]:24} "
            f"{rank:>6} "
            f"{fmt(r['first_price'], 2):>7} "
            f"{fmt(r['last_price'], 2):>7} "
            f"{fmt(r['move_pct']):>8} "
            f"{fmt(r['fit_r2'], 2):>6} "
            f"{r['regime'][:20]:20}"
        )

    print()
    print(f"Occurrences: {len(matching)}")


def print_summary(records: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    full_shapes = [r for r in records if r["points"] == 5]
    clean = sum(1 for r in records if r["fit_quality"] == "CLEAN")
    reversals = sum(
        1 for r in records
        if (r["reversal_count"] or 0) > 0
    )

    print()
    print("OBSERVER NOTES")
    print()
    print(
        f"Runner profiles: {len(records):,} across "
        f"{len({(r['date'], r['market_id']) for r in records}):,} races."
    )
    print(f"Distinct shape/coverage groups: {len(rows)}.")
    print(f"Complete five-price paths: {len(full_shapes):,}/{len(records):,}.")
    print(
        f"Clean quadratic fits (R² >= 0.90): "
        f"{clean:,}/{len(records):,} ({pct(clean, len(records)):.1f}%)."
    )
    print(
        f"Observed paths with reversals: "
        f"{reversals:,}/{len(records):,} ({pct(reversals, len(records)):.1f}%)."
    )
    print()
    print("▲ = price drift, ▼ = price firm, ▬ = unchanged, ? = missing interval.")
    print("TOP½ is market-rank context, not a winner statistic.")
    print("REV% counts observed direction reversals; CURVE describes a fitted polynomial only.")
    print(
        "Quadratic fit uses valid capture timestamps; missing timestamps use flagged nominal offsets "
        "(-15, -10, -5, -2, -0.5 minutes)."
    )


def export_json(
    output_path: Path,
    records: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    payload = {
        "schema": "totebot.shape_review.v2",
        "notes": [
            "observer/review data only",
            "▲ means Betfair price increased (drift)",
            "▼ means Betfair price decreased (firm)",
            "▬ means unchanged",
            "quadratic fit uses capture timestamps or flagged nominal offsets; fitted curvature is not observed reversal",
        ],
        "summary": {
            "runner_profiles": len(records),
            "races": len({(r["date"], r["market_id"]) for r in records}),
            "distinct_shapes": len(rows),
        },
        "shapes": rows,
        "runners": records,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    temp.replace(output_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review ToteBot runner market shapes."
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=DEFAULT_HISTORY_DIR,
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="Number of shape rows to display (default: 30).",
    )
    parser.add_argument(
        "--shape",
        default="",
        help="Show detailed runner examples for one exact shape.",
    )
    parser.add_argument(
        "--details",
        type=int,
        default=40,
        help="Maximum detailed examples (default: 40).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON export path.",
    )
    args = parser.parse_args()

    history_dir = args.history_dir.expanduser()

    if not history_dir.is_dir():
        print(f"History directory not found: {history_dir}")
        return 1

    records = discover_runner_records(history_dir)

    if not records:
        print(f"No usable runner snapshots found under: {history_dir}")
        return 1

    rows = aggregate_shapes(records)

    print_shape_table(rows, max(1, args.top))
    print_summary(records, rows)

    if args.shape:
        print_shape_details(
            records,
            args.shape.strip(),
            max(1, args.details),
        )

    if args.output is not None:
        output_path = args.output.expanduser()
        export_json(output_path, records, rows)
        print()
        print(f"Shape JSON: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
