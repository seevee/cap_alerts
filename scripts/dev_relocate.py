#!/usr/bin/env python3
"""Relocate the dev HA instance for manual mobile-mode testing (issue #16).

Simulates the camper moving: sets HA home coordinates, re-geocodes via
GeoLocator, moves the fake device tracker to the same spot, forces a
cap_alerts poll, and reports the resolved country, alert count, and live
alert entities.

Usage:
    scripts/dev_relocate.py 45.76 4.84          # Lyon    -> France
    scripts/dev_relocate.py 46.0569 14.5058     # Ljubljana -> Slovenia (dev default)
    scripts/dev_relocate.py 51.5074 -0.1278     # London  -> UK official-name alias path

Auth: uses $HA_TOKEN if set; otherwise mints a 30-minute access token from
the long-lived refresh token stored in the dev container (requires sudo).

Stdlib only — run with system python3, no venv needed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

MINT_SNIPPET = """
import json, jwt
from datetime import datetime, timedelta, timezone
auth = json.load(open("/config/.storage/auth"))
tok = next(t for t in auth["data"]["refresh_tokens"]
           if t.get("token_type") == "long_lived_access_token")
now = datetime.now(timezone.utc)
print(jwt.encode({"iss": tok["id"], "iat": now, "exp": now + timedelta(minutes=30)},
                 tok["jwt_key"], algorithm="HS256"))
"""


def mint_token(container: str) -> str:
    out = subprocess.run(
        ["sudo", "docker", "exec", container, "python3", "-c", MINT_SNIPPET],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


class HAClient:
    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self._url}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def state(self, entity_id: str) -> dict | None:
        try:
            return self._request("GET", f"/api/states/{entity_id}")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise

    def set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        self._request(
            "POST",
            f"/api/states/{entity_id}",
            {"state": state, "attributes": attributes},
        )

    def call(self, domain: str, service: str, data: dict | None = None) -> None:
        self._request("POST", f"/api/services/{domain}/{service}", data or {})

    def all_states(self) -> list[dict]:
        return self._request("GET", "/api/states")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--url", default="http://localhost:8123")
    parser.add_argument("--container", default="weather-alerts-card-ha")
    parser.add_argument("--tracker", default="device_tracker.test_phone")
    parser.add_argument("--country-entity", default="sensor.geolocator_country")
    parser.add_argument(
        "--count-entity", default="sensor.cap_alerts_meteoalarm_alert_count"
    )
    parser.add_argument(
        "--geocode-wait",
        type=int,
        default=20,
        help="max seconds to wait for the country sensor to update",
    )
    parser.add_argument(
        "--poll-wait",
        type=int,
        default=15,
        help="seconds to wait after forcing the cap_alerts poll",
    )
    args = parser.parse_args()

    token = os.environ.get("HA_TOKEN") or mint_token(args.container)
    ha = HAClient(args.url, token)

    before = ha.state(args.country_entity)
    country_before = before["state"] if before else "<missing>"
    print(f"country before: {country_before}")

    print(f"home -> {args.latitude}, {args.longitude}")
    ha.call(
        "homeassistant",
        "set_location",
        {"latitude": args.latitude, "longitude": args.longitude},
    )
    ha.call("geolocator", "update_location")
    ha.set_state(
        args.tracker,
        "not_home",
        {
            "latitude": args.latitude,
            "longitude": args.longitude,
            "source_type": "gps",
        },
    )

    deadline = time.monotonic() + args.geocode_wait
    country = country_before
    while time.monotonic() < deadline:
        state = ha.state(args.country_entity)
        country = state["state"] if state else "<missing>"
        if country != country_before:
            break
        time.sleep(2)
    print(f"country after:  {country}")
    if country == country_before:
        print("  (unchanged — same country, or geocode still pending)")

    print("forcing cap_alerts poll...")
    ha.call("homeassistant", "update_entity", {"entity_id": args.count_entity})
    time.sleep(args.poll_wait)

    count_state = ha.state(args.count_entity)
    count = count_state["state"] if count_state else "<missing>"
    print(f"alert count:    {count}")

    alerts = [
        s
        for s in ha.all_states()
        if s["entity_id"].startswith("sensor.cap_alerts_")
        and s["attributes"].get("provider") == "meteoalarm"
        and "event" in s["attributes"]
    ]
    for s in alerts:
        a = s["attributes"]
        print(
            f"  - {s['entity_id']}\n"
            f"    event: {a.get('event')} | severity: {a.get('severity')}"
            f" | expires: {a.get('expires')} | phase: {a.get('phase')}"
        )
    if count == "unavailable":
        print("NOTE: count is unavailable — last poll failed; check the HA log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
