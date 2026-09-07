#!/usr/bin/env python3
"""
ToteBot one-shot manual race-arm request.

Small state helper shared by:
- one-shot HTTP wall
- betfair_t15_emit.py

Lifecycle:

    pending
       |
       +--> consumed
       |
       +--> rejected

The request is intentionally retained after processing for audit/debug.
A new request simply replaces the old completed request.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUEST_FILE_NAME = "manual_arm_request.json"


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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
        temp_path = Path(handle.name)

    os.replace(temp_path, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def request_path(state_dir: Path) -> Path:
    return state_dir / REQUEST_FILE_NAME


def read_request(state_dir: Path) -> dict[str, Any] | None:
    return read_json(request_path(state_dir))


def pending_request(state_dir: Path) -> dict[str, Any] | None:
    request = read_request(state_dir)

    if not request:
        return None

    if request.get("status") != "pending":
        return None

    market_id = str(request.get("market_id") or "").strip()
    if not market_id:
        return None

    return request


def create_request(
    state_dir: Path,
    market_id: str,
    *,
    source: str = "next_to_jump_wall",
    race: str | None = None,
) -> dict[str, Any]:
    market_id = str(market_id).strip()

    if not market_id:
        raise ValueError("market_id is required")

    payload: dict[str, Any] = {
        "schema": "manual_arm_request/v1",
        "status": "pending",
        "market_id": market_id,
        "requested_at": utc_now_iso(),
        "source": source,
    }

    if race:
        payload["race"] = race

    atomic_write_json(request_path(state_dir), payload)
    return payload


def finish_request(
    state_dir: Path,
    *,
    status: str,
    result: str,
    market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    request = read_request(state_dir)

    if not request:
        return None

    payload = {
        **request,
        "status": status,
        "processed_at": utc_now_iso(),
        "result": result,
    }

    if market:
        payload["resolved_market"] = {
            "market_id": market.get("market_id"),
            "track": market.get("track"),
            "market_name": market.get("market_name"),
            "market_start_time": market.get("market_start_time"),
        }

    atomic_write_json(request_path(state_dir), payload)
    return payload


def consume_request(
    state_dir: Path,
    *,
    result: str = "armed",
    market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return finish_request(
        state_dir,
        status="consumed",
        result=result,
        market=market,
    )


def reject_request(
    state_dir: Path,
    *,
    result: str,
    market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return finish_request(
        state_dir,
        status="rejected",
        result=result,
        market=market,
    )
