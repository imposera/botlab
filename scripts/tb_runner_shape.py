"""Shared descriptive runner price shapes; no trading decisions."""
from datetime import datetime
import math

VERSION = "1.0.0"
STAGES = ("T15", "T10", "T5", "T2", "T30")


def canonical(prices):
    return {str(k).upper().replace("-", ""): v for k, v in prices.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0}


def first_last(prices):
    prices = canonical(prices)
    values = [prices[s] for s in STAGES if s in prices]
    return (values[0], values[-1]) if values else (None, None)


def movement_directions(prices):
    prices = canonical(prices)
    values = [prices[s] for s in STAGES if s in prices]
    return [(b > a) - (b < a) for a, b in zip(values, values[1:])]


def shape_symbols(prices):
    prices = canonical(prices)
    if len([s for s in STAGES if s in prices]) < 2:
        return "—"
    return "".join("?" if a not in prices or b not in prices else
                   "▲" if prices[b] > prices[a] else
                   "▼" if prices[b] < prices[a] else "▬"
                   for a, b in zip(STAGES, STAGES[1:]))


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def price_with_source(runner):
    for key in ("last_price_traded", "lastPriceTraded", "price"):
        value = canonical({"p": runner.get(key)}).get("P")
        if value is not None:
            return value, "last_traded" if key != "price" else "price"
    for key in ("back_levels", "availableToBack", "lay_levels", "availableToLay"):
        levels = runner.get(key)
        if isinstance(levels, list) and levels and isinstance(levels[0], dict):
            value = canonical({"p": levels[0].get("price")}).get("P")
            if value is not None:
                return value, "best_back" if key in ("back_levels", "availableToBack") else "best_lay"
    return None, None


def shape_class(stage_prices: dict[str, float]) -> str:
    stage_prices = canonical(stage_prices)
    directions = movement_directions(stage_prices)
    nonzero = [direction for direction in directions if direction]
    first, _ = first_last(stage_prices)

    if len(stage_prices) < 2:
        return "insufficient"
    values = list(stage_prices.values())
    # A quiet hold needs a small overall range, not merely similar endpoints.
    if not nonzero or (first and (max(values) - min(values)) / first < 0.03):
        return "flat_hold"

    changes = sum(1 for a, b in zip(nonzero, nonzero[1:]) if a != b)
    if changes >= 2:
        return "whipsaw"
    if all(direction <= 0 for direction in directions):
        return "steady_firm"
    if all(direction >= 0 for direction in directions):
        return "steady_drift"
    if changes == 1 and nonzero[0] == -1 and nonzero[-1] == 1:
        return "v_shape"
    if nonzero[-1] == -1:
        return "late_firm"
    if nonzero[-1] == 1:
        return "late_drift"
    return "mixed"
