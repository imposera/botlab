#!/usr/bin/env python3
"""
Read-only Betfair gateway for Botlab.

Runs on Basecamp only.

Commands:
    ./betfair_gateway.py health
    ./betfair_gateway.py next-racing

Security rules:
- Reads credentials and certificate files only on Basecamp.
- Never prints a password, application key, certificate data, or session token.
- Uses no betting/order-placement endpoints.
- Creates a fresh authenticated session for each invocation.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
try:
    from tb_names import clean_runner_name
except ModuleNotFoundError as exc:
    if exc.name != "tb_names":
        raise
    # The release bundle already includes the same cloth-prefix normalizer.
    from tb_liquidity import clean_runner_name

import requests


# ---- Basecamp-only configuration -------------------------------------------

SECRETS_FILE = Path(os.environ.get("BETFAIR_SECRETS_FILE", "/opt/betfair/secrets.env")).expanduser()

LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
BETTING_API_BASE = "https://api.betfair.com/exchange/betting/rest/v1.0"

REQUEST_TIMEOUT_SECONDS = 20
HORSE_RACING_EVENT_TYPE_ID = "7"


# ---- Helpers ----------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    """Return an ISO-8601 UTC timestamp suitable for Betfair filters."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    """Print only JSON, making SSH callers easy to parse."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    raise SystemExit(exit_code)


def fail(message: str, *, detail: str | None = None) -> None:
    payload: dict[str, Any] = {
        "gateway": "offline",
        "ok": False,
        "timestamp": iso_z(utc_now()),
        "error": message,
    }
    if detail:
        payload["detail"] = detail

    emit(payload, exit_code=1)


def load_secrets(path: Path) -> None:
    """
    Minimal .env reader.

    Supports:
      NAME=value
      export NAME=value

    Do not put shell expressions or command substitutions in secrets.env.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Secrets file not found: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[7:].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting missing from secrets.env: {name}")
    return value


def certificate_pair() -> tuple[str, str]:
    cert_dir = Path(require_env("BETFAIR_CERT_PATH")).expanduser()
    cert_file = cert_dir / "client-2048.crt"
    key_file = cert_dir / "client-2048.key"

    for file_path in (cert_file, key_file):
        if not file_path.is_file():
            raise FileNotFoundError(f"Certificate file not found: {file_path}")
        if not os.access(file_path, os.R_OK):
            raise PermissionError(f"Certificate file is not readable: {file_path}")

    return str(cert_file), str(key_file)


def betfair_login() -> str:
    """
    Certificate-login and return a session token internally.

    The caller must never print or persist this token.
    """
    username = require_env("BETFAIR_USERNAME")
    password = require_env("BETFAIR_PASSWORD")
    app_key = require_env("BETFAIR_API_KEY")

    response = requests.post(
        LOGIN_URL,
        data={
            "username": username,
            "password": password,
        },
        headers={
            "X-Application": app_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        cert=certificate_pair(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Betfair login returned non-JSON HTTP {response.status_code}"
        ) from exc

    login_status = data.get("loginStatus", "UNKNOWN")

    if response.status_code != 200 or login_status != "SUCCESS":
        # Betfair's status/error are safe to report. Never report token data.
        detail = data.get("error") or login_status
        raise RuntimeError(f"Betfair login failed: {detail}")

    session_token = data.get("sessionToken")
    if not session_token:
        raise RuntimeError("Betfair login succeeded but returned no session token")

    return session_token


def betting_api(
    operation: str,
    payload: dict[str, Any],
    session_token: str,
) -> Any:
    """Call a read-only Betting API operation."""
    app_key = require_env("BETFAIR_API_KEY")
    url = f"{BETTING_API_BASE}/{operation}/"

    response = requests.post(
        url,
        data=json.dumps(payload),
        headers={
            "X-Application": app_key,
            "X-Authentication": session_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{operation} returned non-JSON HTTP {response.status_code}"
        ) from exc

    if response.status_code != 200:
        # API error body is generally useful and contains no login token.
        raise RuntimeError(
            f"{operation} failed with HTTP {response.status_code}: {data}"
        )

    return data


# ---- Commands ---------------------------------------------------------------

def command_health() -> None:
    """Prove Basecamp can authenticate, without exposing a session token."""
    _session_token = betfair_login()

    emit(
        {
            "gateway": "online",
            "ok": True,
            "command": "health",
            "login_status": "SUCCESS",
            "timestamp": iso_z(utc_now()),
            "mode": "read-only",
        }
    )


def command_vaal_r1() -> None:
    """
    One-off gateway proof:
    Vaal (South Africa), Thu 2 Jul 2026, R1, 1000m maiden juvenile.

    Scheduled time:
    - Vaal local: 2026-07-02 12:25 SAST
    - UTC:        2026-07-02 10:25Z
    - Sydney:     2026-07-02 20:25 AEST
    """
    session_token = betfair_login()

    start = datetime(2026, 7, 2, 10, 10, tzinfo=timezone.utc)
    end = datetime(2026, 7, 2, 10, 40, tzinfo=timezone.utc)

    catalogue = betting_api(
        "listMarketCatalogue",
        {
            "filter": {
                "eventTypeIds": [HORSE_RACING_EVENT_TYPE_ID],
                "marketCountries": ["ZA"],
                "marketTypeCodes": ["WIN"],
                "marketStartTime": {
                    "from": iso_z(start),
                    "to": iso_z(end),
                },
            },
            "marketProjection": [
                "EVENT",
                "MARKET_START_TIME",
                "RUNNER_DESCRIPTION",
                "RUNNER_METADATA",
            ],
            "sort": "FIRST_TO_START",
            "maxResults": 20,
        },
        session_token,
    )

    markets = [market_summary(market) for market in catalogue]

    vaal_candidates = [
        market
        for market in markets
        if "vaal" in (
            f"{market.get('track', '')} "
            f"{market.get('event_name', '')}"
        ).lower()
    ]

    emit(
        {
            "gateway": "online",
            "ok": True,
            "command": "vaal-r1",
            "status": (
                "market_found"
                if vaal_candidates
                else "no_matching_betfair_market_found"
            ),
            "race_target": {
                "track": "Vaal",
                "country": "ZA",
                "scheduled_local": "2026-07-02T12:25:00+02:00",
                "scheduled_utc": "2026-07-02T10:25:00Z",
                "scheduled_sydney": "2026-07-02T20:25:00+10:00",
                "race": "R1",
                "distance_m": 1000,
            },
            "candidate_count": len(vaal_candidates),
            "markets": vaal_candidates,
            "timestamp": iso_z(utc_now()),
        }
    )

def runner_summary(runner: dict[str, Any]) -> dict[str, Any]:
    """Return stable race-card fields only; no pricing yet."""
    metadata = runner.get("metadata") or {}

    return {
        "selection_id": runner.get("selectionId"),
        "runner_name": clean_runner_name(runner.get("runnerName")),
        "sort_priority": runner.get("sortPriority"),
        "cloth_number": metadata.get("CLOTH_NUMBER"),
    }


def market_summary(market: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a MarketCatalogue result for ToteBot discovery."""
    event = market.get("event") or {}

    return {
        "market_id": market.get("marketId"),
        "market_name": market.get("marketName"),
        "market_start_time": market.get("marketStartTime"),
        "total_matched": market.get("totalMatched"),
        "event_id": event.get("id"),
        "event_name": event.get("name"),
        "track": event.get("venue"),
        "country_code": event.get("countryCode"),
        "runners": [
            runner_summary(runner)
            for runner in market.get("runners", [])
        ],
    }


def command_next_racing() -> None:
    """
    Find upcoming Australian Horse Racing WIN market candidates.

    This is deliberately discovery-only:
    - market metadata
    - runner names/numbers
    - no prices yet
    """
    session_token = betfair_login()

    now = utc_now()
    until = now + timedelta(hours=12)

    catalogue = betting_api(
        "listMarketCatalogue",
        {
            "filter": {
                "eventTypeIds": [HORSE_RACING_EVENT_TYPE_ID],
                "marketCountries": ["AU"],
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
            "maxResults": 20,
        },
        session_token,
    )

    markets = [market_summary(market) for market in catalogue]

    emit(
        {
            "gateway": "online",
            "ok": True,
            "command": "next-racing",
            "timestamp": iso_z(utc_now()),
            "window": {
                "from": iso_z(now),
                "to": iso_z(until),
            },
            "market_count": len(markets),
            "markets": markets,
        }
    )


# ---- CLI --------------------------------------------------------------------

def usage() -> None:
    emit(
        {
            "gateway": "online",
            "ok": False,
            "usage": [
                "betfair_gateway.py health",
                "betfair_gateway.py next-racing",
                "betfair_gateway.py vaal-r1",
            ],
        },
        exit_code=2,
    )


def main() -> None:
    try:
        load_secrets(SECRETS_FILE)

        if len(sys.argv) != 2:
            usage()

        command = sys.argv[1].strip().lower()

        if command == "health":
            command_health()
        elif command == "next-racing":
            command_next_racing()
        elif command == "vaal-r1":
            command_vaal_r1()
        else:
            usage()

    except KeyboardInterrupt:
        fail("Interrupted")
    except requests.exceptions.SSLError:
        fail(
            "TLS/certificate error",
            detail="Check certificate paths, permissions, and certificate validity.",
        )
    except requests.RequestException as exc:
        fail("Network/API request error", detail=str(exc))
    except Exception as exc:
        fail("Gateway error", detail=str(exc))


if __name__ == "__main__":
    main()
