#!/usr/bin/env python3
"""
wbgt_trmnl.py

Pulls three independent, decision-oriented answers for a fixed location:
  1. Work outside, how long?  -- NWS native WBGT -> ACGIH TLV work/rest table
  2. Umbrella?                -- NWS probabilityOfPrecipitation + weather type
  3. Sun protection?          -- EPA UV Index (by ZIP) -> WHO bands

Writes the result to data/latest.json (committed by the GitHub Actions
workflow). Both the TRMNL Polling plugin and a phone widget read that same
file directly -- one computed value, two displays, no drift between them.

Each answer is computed independently. If one data source fails, the other
two still update, and the failed one keeps its last-known-good value
instead of going blank or being silently replaced with a guess.

NWS-only for WBGT -- no Liljegren/pywbgt fallback. If the native
`wetBulbGlobeTemperature` layer is missing for your grid cell, the
work-duration answer goes stale rather than substituting a DIY estimate.
See README "Check 1" for how to verify that layer is actually populated.
"""

from __future__ import annotations

import os
import re
import sys
import json
import datetime as dt

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LAT = float(os.environ.get("WBGT_LAT", "37.5407"))
LON = float(os.environ.get("WBGT_LON", "-77.4360"))
ZIP = os.environ.get("WBGT_ZIP", "23221")
LOOKAHEAD_HOURS = int(os.environ.get("WBGT_LOOKAHEAD_HOURS", "6"))  # matches cron cadence

NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "(personal-outdoor-dashboard, replace-with-your-email@example.com)",
)
NWS_HEADERS = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}

OUTPUT_PATH = os.environ.get("WBGT_OUTPUT_PATH", "data/latest.json")

# ACGIH TLV work/rest table, MODERATE workload, ACCLIMATIZED workers.
# WBGT in Fahrenheit. Source: ACGIH Threshold Limit Values for Heat Stress.
# Unacclimatized workers: subtract roughly 3.6F (2C) from each threshold --
# not applied here, no way for the script to know your acclimatization state.
WORK_REST_TABLE = [
    (80.0, "Work freely", 60),
    (82.0, "75% work / 25% rest", 45),
    (85.0, "50% work / 50% rest", 30),
    (88.0, "25% work / 75% rest", 15),
]
WORK_REST_STOP = "Don't work outside"

# WHO UV Index bands -> sun protection guidance.
UV_BANDS = [
    (3, "No protection needed"),
    (6, "SPF 30+, hat"),
    (8, "SPF 30+, hat, seek midday shade"),
    (999, "Minimize exposure 10am-4pm"),
]

# Deliberately below 50%. Cost of carrying an umbrella you didn't need is
# trivial; cost of not having one when it rains isn't. Minimax-regret
# framing, not an expected-value one.
UMBRELLA_POP_THRESHOLD = 30.0

# NDFD 'weather' layer keywords used to suppress the umbrella call when
# precip is frozen-only. NOT verified against a live response -- NWS's
# robots.txt blocks automated fetches from this build environment. Treat
# as a soft refinement on top of probabilityOfPrecipitation, not the
# primary signal; spot-check against a real winter forecast once.
FROZEN_ONLY_KEYWORDS = {"snow", "ice", "sleet", "graupel"}
LIQUID_KEYWORDS = {"rain", "shower", "drizzle", "thunderstorm"}


# --------------------------------------------------------------------------
# ISO8601 duration parsing (no pandas). NWS validTime durations only ever
# use days/hours/minutes/seconds in practice (PT3H, P1D, etc.), so a small
# regex covers real usage without pulling in a dependency for it.
# --------------------------------------------------------------------------

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso_duration(s: str) -> dt.timedelta:
    m = _ISO_DURATION_RE.match(s)
    if not m:
        raise ValueError(f"Unparseable ISO8601 duration: {s!r}")
    parts = {k: (int(v) if v else 0) for k, v in m.groupdict().items()}
    return dt.timedelta(days=parts["days"], hours=parts["hours"],
                         minutes=parts["minutes"], seconds=parts["seconds"])


def _values_in_window(layer, start: dt.datetime, end: dt.datetime):
    """Return (interval_start, interval_end, value) tuples overlapping [start, end)."""
    if not layer or "values" not in layer:
        return []
    out = []
    for entry in layer["values"]:
        vstart_str, vdur_str = entry["validTime"].split("/")
        vstart = dt.datetime.fromisoformat(vstart_str)
        vend = vstart + parse_iso_duration(vdur_str)
        if vstart < end and vend > start:
            out.append((vstart, vend, entry["value"]))
    return out


def _value_at(layer, when: dt.datetime):
    hits = _values_in_window(layer, when, when + dt.timedelta(seconds=1))
    return hits[0][2] if hits else None


# --------------------------------------------------------------------------
# NWS access
# --------------------------------------------------------------------------

def get_gridpoint_url(lat: float, lon: float) -> str:
    r = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                      headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()["properties"]["forecastGridData"]


def get_gridpoint_data(url: str) -> dict:
    r = requests.get(url, headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()["properties"]


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# --------------------------------------------------------------------------
# Answer 1: work outside, how long?
# --------------------------------------------------------------------------

def classify_work(wbgt_f: float):
    for threshold, label, minutes in WORK_REST_TABLE:
        if wbgt_f < threshold:
            return label, minutes
    return WORK_REST_STOP, 0


def compute_work_answer(grid: dict, now: dt.datetime) -> dict:
    wbgt_c = _value_at(grid.get("wetBulbGlobeTemperature"), now)
    if wbgt_c is None:
        raise RuntimeError(
            "wetBulbGlobeTemperature not populated for this gridpoint/time "
            "-- no fallback by design."
        )
    wbgt_f = c_to_f(wbgt_c)
    label, minutes = classify_work(wbgt_f)
    return {
        "wbgt_f": round(wbgt_f, 1),
        "label": label,
        "minutes_per_hour": minutes,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer 2: umbrella?
# --------------------------------------------------------------------------

def _is_frozen_only(weather_value) -> bool:
    """weather_value is a list of NDFD condition dicts for one interval.
    True only if we see a frozen-precip keyword and no liquid-precip one."""
    if not weather_value:
        return False
    types = {str(cond.get("weather", "")).lower() for cond in weather_value}
    has_frozen = any(any(k in t for k in FROZEN_ONLY_KEYWORDS) for t in types)
    has_liquid = any(any(k in t for k in LIQUID_KEYWORDS) for t in types)
    return has_frozen and not has_liquid


def compute_rain_answer(grid: dict, now: dt.datetime, lookahead_hours: int) -> dict:
    end = now + dt.timedelta(hours=lookahead_hours)
    pop_hits = _values_in_window(grid.get("probabilityOfPrecipitation"), now, end)
    weather_hits = _values_in_window(grid.get("weather"), now, end)

    pop_values = [v for _, _, v in pop_hits if v is not None]
    max_pop = max(pop_values) if pop_values else 0.0
    frozen_only_everywhere = bool(weather_hits) and all(
        _is_frozen_only(v) for _, _, v in weather_hits
    )

    bring_umbrella = (max_pop >= UMBRELLA_POP_THRESHOLD) and not frozen_only_everywhere
    return {
        "max_pop_pct": round(max_pop, 0),
        "bring_umbrella": bring_umbrella,
        "frozen_only": frozen_only_everywhere,
        "window_hours": lookahead_hours,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer 3: sun protection?
# --------------------------------------------------------------------------

def classify_uv(uv_index: float) -> str:
    for threshold, label in UV_BANDS:
        if uv_index < threshold:
            return label
    return UV_BANDS[-1][1]


def _find_uv_value(payload):
    """Defensive parse -- EPA's Envirofacts UV JSON schema was NOT
    verified live against a real response from this build environment
    (only the default-format endpoint was confirmed reachable; the /JSON
    variant couldn't be fetched under this tool's URL rules). Scans for
    any key that looks like a UV index field rather than hard-coding an
    unverified exact key name. If this returns None every run, inspect a
    real response once and hard-code the actual key -- see README."""
    records = payload if isinstance(payload, list) else [payload]
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key, val in rec.items():
            key_lower = key.lower()
            if "uv" in key_lower and ("index" in key_lower or key_lower.endswith("uvi")):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return None


def compute_sun_answer(zip_code: str, now: dt.datetime) -> dict:
    url = f"https://data.epa.gov/dmapservice/getEnvirofactsUVDAILY/ZIP/{zip_code}/JSON"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    uv = _find_uv_value(r.json())
    if uv is None:
        raise RuntimeError(
            "Could not locate a UV index field in the EPA response -- "
            "schema assumption failed, see _find_uv_value docstring."
        )
    return {"uv_index": uv, "label": classify_uv(uv), "as_of": now.isoformat()}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def load_existing(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(now: dt.datetime, existing: dict):
    """Pure-ish core: takes 'now' and the prior JSON, returns (new_result,
    had_failure). Split out from main() so it's testable without touching
    the filesystem or environment."""
    result = dict(existing)
    had_failure = False

    grid = None
    try:
        grid_url = get_gridpoint_url(LAT, LON)
        grid = get_gridpoint_data(grid_url)
    except Exception as e:
        print(f"NWS gridpoint fetch failed entirely: {e}", file=sys.stderr)
        had_failure = True

    if grid is not None:
        try:
            result["work"] = compute_work_answer(grid, now)
        except Exception as e:
            print(f"Work-outside answer failed: {e}", file=sys.stderr)
            had_failure = True
        try:
            result["rain"] = compute_rain_answer(grid, now, LOOKAHEAD_HOURS)
        except Exception as e:
            print(f"Rain answer failed: {e}", file=sys.stderr)
            had_failure = True
    else:
        had_failure = True

    try:
        result["sun"] = compute_sun_answer(ZIP, now)
    except Exception as e:
        print(f"Sun-protection answer failed: {e}", file=sys.stderr)
        had_failure = True

    result["generated_at"] = now.isoformat()
    return result, had_failure


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    existing = load_existing(OUTPUT_PATH)
    result, had_failure = run(now, existing)

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))
    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())
