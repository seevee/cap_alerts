#!/usr/bin/env python3
"""Read-only probe: which keys NWS publishes under ``properties.parameters``.

Issue #292 moves the NWS event codes into ``parameters`` under the GeoJSON
``eventCode`` scheme names (``NationalWeatherService``, ``SAME``). That is only
collision-free if NWS never publishes a *parameter* under either name. This
walks the national active set once and counts every parameter key, every
``eventCode`` key, and the ``ends``/``replacedBy`` fill rates. Stdlib only.

Usage: scripts/nws_parameters_probe.py [--json]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter

URL = "https://api.weather.gov/alerts/active"
UA = "cap_alerts probe (https://github.com/seevee/cap_alerts; read-only vocabulary check)"


def main() -> int:
    req = urllib.request.Request(
        URL, headers={"User-Agent": UA, "Accept": "application/geo+json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    features = data.get("features", [])
    params: Counter[str] = Counter()
    event_codes: Counter[str] = Counter()
    list_shapes: Counter[str] = Counter()
    multi_vtec = 0
    multi_code = Counter()
    filled: Counter[str] = Counter()
    for f in features:
        p = f.get("properties", {})
        for k, v in (p.get("parameters") or {}).items():
            params[k] += 1
            list_shapes[type(v).__name__] += 1
            if k == "VTEC" and isinstance(v, list) and len(v) > 1:
                multi_vtec += 1
        for k, v in (p.get("eventCode") or {}).items():
            event_codes[k] += 1
            if isinstance(v, list) and len(v) > 1:
                multi_code[k] += 1
        for k in ("ends", "replacedBy", "replacedAt", "affectedZones"):
            if p.get(k):
                filled[k] += 1
    out = {
        "features": len(features),
        "parameter_keys": dict(params.most_common()),
        "parameter_value_types": dict(list_shapes),
        "eventCode_keys": dict(event_codes),
        "eventCode_multi_valued": dict(multi_code),
        "vtec_multi_valued": multi_vtec,
        "filled": dict(filled),
        "collision": sorted(set(params) & set(event_codes)),
    }
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
