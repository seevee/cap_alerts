#!/usr/bin/env python3
"""Walk every config flow menu and form on a running HA, without changing anything.

The config flow is the one part of the integration the test suite exercises
through Home Assistant's flow manager but never through a *live* instance, and
it is also the part a refactor breaks silently: a step method that lost its
mixin still imports fine, and only a real flow notices. This drives the actual
dialog over the REST API and asserts, for every step, that the menu offers the
options it should and the form asks for the fields it should.

**How this differs from the test suite.** ``tests/test_config_flow_setup.py``
and friends run the handler in-process against a fabricated ``hass``. This runs
against the instance you deployed to, so it also proves the module layout
imports under HA's loader, that the composed handler resolves every provider's
steps, and that the two live-fetch steps (MeteoAlarm's region picker, WMO's
source registry) still work against the real feeds.

**Read-only by construction.** Menu options that commit on click
(``*_country_only``, ``*_country_wide``, ``*_global``) are refused before the
walk starts, no form is ever submitted with values that would finish a flow,
every flow opened is aborted at the end, and the entry count is compared before
and after. It does fetch from MeteoAlarm and WMO unless ``--skip-network``.

Usage:
    scripts/flow_walk.py                     # walk everything
    scripts/flow_walk.py --skip-network      # omit the two live-fetch steps
    scripts/flow_walk.py --url http://ha.lan:8123 --token "$TOKEN"

Auth: uses $HA_TOKEN if set; otherwise mints a 30-minute access token from
the long-lived refresh token stored in the dev container (requires sudo).

Adding a provider means adding its rows to SETUP, RECONFIGURE, and
OPTIONS_SCHEMA below. Each row is one POST and one assertion.

Exit code is 1 if any check failed, so it can gate a release step.

Stdlib only — run with system python3, no venv needed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

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

PROVIDERS = ("nws", "eccc", "meteoalarm", "wmo", "gdacs")

# Steps that create or update an entry the moment they are selected. The walk
# refuses to run if any row names one, which is what keeps "read-only" a
# property of the file rather than a promise in the docstring.
COMMIT_STEPS = frozenset(
    {
        "meteoalarm_country_only",
        "wmo_country_wide",
        "gdacs_global",
        "reconfigure_meteoalarm_country_only",
        "reconfigure_wmo_country_wide",
        "reconfigure_gdacs_global",
    }
)


@dataclass(frozen=True)
class Expect:
    """What a step should render: a menu of options, or a form of fields."""

    kind: str  # "menu" | "form"
    step_id: str
    items: tuple[str, ...]
    # A step allowed to abort instead, when live data can legitimately be empty.
    allow_abort: tuple[str, ...] = ()
    network: bool = False


def menu(step_id: str, *options: str, **kw: object) -> Expect:
    return Expect("menu", step_id, options, **kw)  # type: ignore[arg-type]


def form(step_id: str, *fields: str, **kw: object) -> Expect:
    return Expect("form", step_id, fields, **kw)  # type: ignore[arg-type]


# Each row is (payload to POST, what the response should be). A payload with
# "next_step_id" picks a menu option; anything else is form input.
SETUP: dict[str, list[tuple[dict, Expect]]] = {
    "nws": [
        (
            {"next_step_id": "nws"},
            menu("nws", "nws_zone", "nws_gps_loc", "nws_gps_tracker", "user"),
        ),
        ({"next_step_id": "nws_zone"}, form("nws_zone", "zone_id")),
    ],
    "eccc": [
        (
            {"next_step_id": "eccc"},
            menu("eccc", "eccc_province", "eccc_gps_loc", "eccc_gps_tracker", "user"),
        ),
        ({"next_step_id": "eccc_province"}, form("eccc_province", "province")),
    ],
    "meteoalarm": [
        (
            {"next_step_id": "meteoalarm"},
            menu(
                "meteoalarm",
                "meteoalarm_country",
                "meteoalarm_country_source",
                "user",
            ),
        ),
        ({"next_step_id": "meteoalarm_country"}, form("meteoalarm_country", "country")),
        # Submitting a country advances to the filter menu; it creates nothing.
        (
            {"country": "FR"},
            menu(
                "meteoalarm_filter",
                "meteoalarm_country_only",
                "meteoalarm_gps_polygon",
                "meteoalarm_gps_tracker",
                "meteoalarm_region_picker",
                "meteoalarm",
            ),
        ),
        (
            {"next_step_id": "meteoalarm_region_picker"},
            # A country with nothing live aborts rather than showing an empty form.
            form(
                "meteoalarm_region_picker",
                "regions",
                allow_abort=("no_regions_available",),
                network=True,
            ),
        ),
    ],
    "wmo": [
        # No menu of its own: the provider hands straight to the source form.
        ({"next_step_id": "wmo"}, form("wmo_source", "source_id", network=True)),
        (
            {"source_id": "ph-pagasa-en"},
            menu(
                "wmo_filter",
                "wmo_country_wide",
                "wmo_gps_loc",
                "wmo_gps_tracker",
                "wmo_geocode",
                "wmo_source",
            ),
        ),
        ({"next_step_id": "wmo_geocode"}, form("wmo_geocode", "geocode_prefixes")),
    ],
    "gdacs": [
        (
            {"next_step_id": "gdacs"},
            menu("gdacs", "gdacs_global", "gdacs_gps_loc", "user"),
        ),
        ({"next_step_id": "gdacs_gps_loc"}, form("gdacs_gps_loc", "gps_loc")),
    ],
}

# Reconfigure needs a loaded entry for that provider; providers without one are
# reported as skipped rather than failed.
RECONFIGURE: dict[str, list[tuple[dict, Expect]]] = {
    "nws": [
        (
            {"next_step_id": "reconfigure_nws"},
            menu(
                "reconfigure_nws",
                "reconfigure_nws_zone",
                "reconfigure_nws_gps_loc",
                "reconfigure_nws_gps_tracker",
                "reconfigure",
            ),
        ),
        (
            {"next_step_id": "reconfigure_nws_zone"},
            form("reconfigure_nws_zone", "zone_id"),
        ),
    ],
    "eccc": [
        (
            {"next_step_id": "reconfigure_eccc"},
            menu(
                "reconfigure_eccc",
                "reconfigure_eccc_province",
                "reconfigure_eccc_gps_loc",
                "reconfigure_eccc_gps_tracker",
                "reconfigure",
            ),
        ),
        (
            {"next_step_id": "reconfigure_eccc_province"},
            form("reconfigure_eccc_province", "province"),
        ),
    ],
    "meteoalarm": [
        (
            {"next_step_id": "reconfigure_meteoalarm"},
            menu(
                "reconfigure_meteoalarm",
                "reconfigure_meteoalarm_country",
                "reconfigure_meteoalarm_country_source",
                "reconfigure",
            ),
        ),
        (
            {"next_step_id": "reconfigure_meteoalarm_country"},
            form("reconfigure_meteoalarm_country", "country"),
        ),
    ],
    "wmo": [
        (
            {"next_step_id": "reconfigure_wmo"},
            form("reconfigure_wmo_source", "source_id", network=True),
        ),
        (
            {"source_id": "ph-pagasa-en"},
            menu(
                "reconfigure_wmo_filter",
                "reconfigure_wmo_country_wide",
                "reconfigure_wmo_gps_loc",
                "reconfigure_wmo_gps_tracker",
                "reconfigure_wmo_geocode",
                "reconfigure_wmo_source",
            ),
        ),
        (
            {"next_step_id": "reconfigure_wmo_geocode"},
            form("reconfigure_wmo_geocode", "geocode_prefixes"),
        ),
    ],
    "gdacs": [
        (
            {"next_step_id": "reconfigure_gdacs"},
            menu(
                "reconfigure_gdacs",
                "reconfigure_gdacs_global",
                "reconfigure_gdacs_gps_loc",
                "reconfigure",
            ),
        ),
        (
            {"next_step_id": "reconfigure_gdacs_gps_loc"},
            form("reconfigure_gdacs_gps_loc", "gps_loc"),
        ),
    ],
}

# Field order is asserted, not just membership: the options form renders in
# schema order, so a reordered provider block is a user-visible change.
OPTIONS_SCHEMA: dict[str, list[str]] = {
    "nws": ["scan_interval", "timeout", "exclude_marine", "geocode_prefixes"],
    "eccc": [
        "scan_interval",
        "timeout",
        "language",
        "streaming",
        "feed_source",
        "exclude_marine",
        "geocode_prefixes",
    ],
    "meteoalarm": ["scan_interval", "timeout", "language", "geocode_prefixes"],
    "wmo": ["scan_interval", "timeout", "language", "geocode_prefixes"],
    "gdacs": ["scan_interval", "timeout", "gdacs_event_types", "alert_level"],
}


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

    def request(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self._url}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            return {"_http_error": err.code, "_body": err.read().decode()[:200]}

    def entries(self) -> list[dict]:
        return self.request("GET", "/api/config/config_entries/entry?domain=cap_alerts")


@dataclass
class Report:
    checks: int = 0
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks += 1
        if not ok:
            self.failures.append(label)
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{'   ' + detail if detail else ''}")

    def skip(self, label: str, why: str) -> None:
        self.skipped.append(label)
        print(f"  skip {label}   ({why})")


def verify_rows_are_read_only() -> None:
    """Refuse to run if any row would commit an entry."""
    for table in (SETUP, RECONFIGURE):
        for provider, rows in table.items():
            for payload, _expect in rows:
                target = payload.get("next_step_id")
                if target in COMMIT_STEPS:
                    raise SystemExit(
                        f"refusing to walk: {provider} selects {target}, which "
                        "creates or updates an entry"
                    )


def assert_step(report: Report, res: dict, expect: Expect) -> None:
    if res.get("type") == "abort" and res.get("reason") in expect.allow_abort:
        report.check(f"{expect.step_id} aborted as allowed", True, res["reason"])
        return
    if "_http_error" in res:
        report.check(expect.step_id, False, f"HTTP {res['_http_error']} {res['_body']}")
        return
    ok = res.get("type") == expect.kind and res.get("step_id") == expect.step_id
    report.check(
        f"{expect.kind} {expect.step_id}",
        ok,
        ""
        if ok
        else f"got {res.get('type')}/{res.get('step_id') or res.get('reason')}",
    )
    if not ok:
        return
    got = tuple(
        res.get("menu_options") or ()
        if expect.kind == "menu"
        else (f["name"] for f in res.get("data_schema") or [])
    )
    report.check(
        f"  {'options' if expect.kind == 'menu' else 'fields'} of {expect.step_id}",
        got == expect.items,
        "" if got == expect.items else f"got {list(got)}",
    )


def walk(
    ha: HAClient,
    report: Report,
    kind: str,
    rows: list[tuple[dict, Expect]],
    start_body: dict,
    skip_network: bool,
) -> list[tuple[str, str]]:
    """Run one flow's rows. Returns the (kind, flow_id) pairs to abort later."""
    opened: list[tuple[str, str]] = []
    sub = "options/flow" if kind == "options" else "flow"
    res = ha.request("POST", f"/api/config/config_entries/{sub}", start_body)
    if "_http_error" in res:
        report.check("start flow", False, f"HTTP {res['_http_error']} {res['_body']}")
        return opened
    flow_id = res["flow_id"]
    opened.append((kind, flow_id))
    for payload, expect in rows:
        if expect.network and skip_network:
            report.skip(f"{expect.kind} {expect.step_id}", "--skip-network")
            return opened
        res = ha.request("POST", f"/api/config/config_entries/{sub}/{flow_id}", payload)
        assert_step(report, res, expect)
        if res.get("type") == "abort":
            break
    return opened


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8123")
    parser.add_argument("--container", default="weather-alerts-card-ha")
    parser.add_argument("--token", default=None, help="overrides $HA_TOKEN")
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="omit steps that fetch from MeteoAlarm or WMO",
    )
    args = parser.parse_args()

    verify_rows_are_read_only()
    token = args.token or os.environ.get("HA_TOKEN") or mint_token(args.container)
    ha = HAClient(args.url, token)
    report = Report()
    opened: list[tuple[str, str]] = []

    before = ha.entries()
    if "_http_error" in getattr(before, "keys", lambda: [])():
        print(f"cannot list entries: {before}")
        return 1
    by_provider: dict[str, str] = {}
    for entry in before:
        if entry["state"] != "loaded":
            continue
        # Titles are "CAP Alerts <PROVIDER> (<location>)"; entry data is not
        # exposed over this endpoint, so the title is the only provider signal.
        parts = entry["title"].split()
        if len(parts) > 2:
            by_provider.setdefault(parts[2].lower(), entry["entry_id"])
    print(f"{len(before)} entries loaded, providers present: {sorted(by_provider)}\n")

    try:
        print("[setup] provider menu")
        res = ha.request(
            "POST",
            "/api/config/config_entries/flow",
            {"handler": "cap_alerts", "show_advanced_options": False},
        )
        opened.append(("setup", res["flow_id"]))
        assert_step(report, res, menu("user", *PROVIDERS))

        for provider, rows in SETUP.items():
            print(f"\n[setup] {provider}")
            opened += walk(
                ha,
                report,
                "setup",
                rows,
                {"handler": "cap_alerts", "show_advanced_options": False},
                args.skip_network,
            )

        for provider, rows in RECONFIGURE.items():
            print(f"\n[reconfigure] {provider}")
            entry_id = by_provider.get(provider)
            if not entry_id:
                report.skip(f"reconfigure {provider}", "no loaded entry")
                continue
            opened += walk(
                ha,
                report,
                "setup",
                rows,
                {"handler": "cap_alerts", "entry_id": entry_id},
                args.skip_network,
            )

        print("\n[options] schema per provider")
        for provider, expected in OPTIONS_SCHEMA.items():
            entry_id = by_provider.get(provider)
            if not entry_id:
                report.skip(f"options {provider}", "no loaded entry")
                continue
            res = ha.request(
                "POST", "/api/config/config_entries/options/flow", {"handler": entry_id}
            )
            if "_http_error" in res:
                report.check(f"options {provider}", False, str(res))
                continue
            opened.append(("options", res["flow_id"]))
            got = [f["name"] for f in res.get("data_schema") or []]
            report.check(
                f"options {provider}",
                got == expected,
                "" if got == expected else f"got {got}",
            )
    finally:
        print(f"\n[cleanup] aborting {len(opened)} flow(s)")
        for kind, flow_id in opened:
            sub = "options/flow" if kind == "options" else "flow"
            ha.request("DELETE", f"/api/config/config_entries/{sub}/{flow_id}")

    after = ha.entries()
    report.check(
        "entry count unchanged",
        len(after) == len(before),
        "" if len(after) == len(before) else f"{len(before)} -> {len(after)}",
    )

    print(
        f"\n{report.checks} checks, {len(report.failures)} failed, "
        f"{len(report.skipped)} skipped"
    )
    if report.failures:
        for label in report.failures:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
