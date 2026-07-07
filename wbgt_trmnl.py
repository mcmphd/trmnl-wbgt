#!/usr/bin/env python3
"""
wbgt_trmnl.py

Daily weather briefing, decisions-first: a synthesized verdict line plus a
small set of gated supporting metrics, not a dashboard of numbers.

Decisions answered (see docs/WEATHER_BRIEFING.md and
docs/WEATHER_FEED_CONTRACT.md in the Eos repo for the design this
follows):
  - Head indoors / delay plans -- NWS probabilityOfThunder, highest verdict priority
  - Hydrate / limit exertion  -- NWS native WBGT -> flag category
  - Umbrella vs raincoat      -- rain probability + intensity + wind
  - Windbreaker / layers      -- feels-like + wind gusts
  - Sunscreen / hat           -- EPA UV Index, gated (only shown when high+)
  - What to wear              -- feels-like range, not raw air temp

AQI/air quality is NOT implemented -- no NWS layer for it; every real
source (AirNow, PurpleAir) needs a separate API key. Pending that
decision, see the Eos repo's WEATHER_DATA_SOURCE.md for status.

Writes the result to data/latest.json (committed by the GitHub Actions
workflow). TRMNL's Polling plugin reads that file directly.

Every answer is computed independently. If one data source fails, the
others still update, and the failed one keeps its last-known-good value
instead of going blank or being silently replaced with a guess. The
verdict line is re-synthesized every run from whatever is currently in
the result (fresh values where available, stale ones where not) rather
than being gated on every input having succeeded this run.

NWS-only -- no Liljegren/pywbgt fallback, no derived feels-like (NWS
already publishes apparentTemperature, which folds in humidity and wind
chill; confirmed live against the real grid cell, see PROJECT_STATUS.md).
"""

from __future__ import annotations

import os
import re
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo

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

AIRNOW_API_KEY = os.environ.get("AIRNOW_API_KEY", "")
NWS_HEADERS = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}

OUTPUT_PATH = os.environ.get("WBGT_OUTPUT_PATH", "data/latest.json")

FALLBACK_TZ = "America/New_York"

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

# WBGT flag categories, Military TB MED 507 cutoffs (degrees F). Distinct
# from WORK_REST_TABLE above -- flags are the general-public "should I be
# careful today" signal, WORK_REST_TABLE is occupational shift planning.
# Both are kept: flag drives the verdict line, work/rest is a supporting
# Tier-2 detail for anyone who wants it.
WBGT_FLAG_TABLE = [
    (80.0, "white"),
    (85.0, "green"),
    (88.0, "yellow"),
    (90.0, "red"),
]
WBGT_FLAG_BLACK = "black"

# WHO UV Index bands -> sun protection guidance. First threshold (3) also
# doubles as the "show at all" gate -- below it, sun protection isn't
# surfaced anywhere, Tier 1 or Tier 2.
UV_BANDS = [
    (3, "No protection needed"),
    (6, "SPF 30+, hat"),
    (8, "SPF 30+, hat, seek midday shade"),
    (999, "Minimize exposure 10am-4pm"),
]
UV_SHOW_THRESHOLD = UV_BANDS[0][0]

# EPA's own AQI category boundary for "Unhealthy for Sensitive Groups" --
# matches WEATHER_FEED_CONTRACT.md's suggested gate exactly, not a value
# we picked ourselves.
AQI_SHOW_THRESHOLD = 101

# Deliberately below 50%. Cost of carrying an umbrella you didn't need is
# trivial; cost of not having one when it rains isn't. Minimax-regret
# framing, not an expected-value one.
UMBRELLA_POP_THRESHOLD = 30.0

# Same minimax-regret reasoning as UMBRELLA_POP_THRESHOLD, reused rather
# than re-derived: cost of a false "storms expected" is a glance at the
# sky, cost of a missed one is getting caught outside. Adjustable if 30%
# turns out too chatty in practice.
STORM_POT_THRESHOLD = 30.0

# "Windy" gate: crosses over into affecting the umbrella-vs-raincoat call
# and the windbreaker/layers signal. User-supplied, not derived --
# 15 mph sustained or 25 mph gust, whichever comes first.
WINDY_SUSTAINED_MPH = 15.0
WINDY_GUST_MPH = 25.0

# NDFD 'weather' layer keywords used to suppress the umbrella call when
# precip is frozen-only. Confirmed live: NWS values are snake_case tokens
# ("rain_showers", "thunderstorms") -- substring matching handles that.
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


def local_day_window(now: dt.datetime, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of the local calendar day containing `now`, in UTC."""
    local_now = now.astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + dt.timedelta(days=1)
    return local_start.astimezone(dt.timezone.utc), local_end.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------
# NWS access
# --------------------------------------------------------------------------

def get_point_metadata(lat: float, lon: float) -> dict:
    r = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                      headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    props = r.json()["properties"]
    return {"grid_url": props["forecastGridData"], "timezone": props["timeZone"]}


def get_gridpoint_data(url: str) -> dict:
    r = requests.get(url, headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()["properties"]


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371


# --------------------------------------------------------------------------
# Answer: hydrate / limit exertion (WBGT)
# --------------------------------------------------------------------------

def classify_work(wbgt_f: float):
    for threshold, label, minutes in WORK_REST_TABLE:
        if wbgt_f < threshold:
            return label, minutes
    return WORK_REST_STOP, 0


def classify_wbgt_flag(wbgt_f: float) -> str:
    for threshold, flag in WBGT_FLAG_TABLE:
        if wbgt_f < threshold:
            return flag
    return WBGT_FLAG_BLACK


def compute_work_answer(grid: dict, now: dt.datetime) -> dict:
    layer = grid.get("wetBulbGlobeTemperature")
    if not layer or not layer.get("values"):
        # Distinct from a transient fetch gap (raised below): the layer
        # itself is absent, which is how NWS is expected to behave in the
        # off-season when heat stress isn't a concern -- not verified
        # against a real winter response yet (built in July). Report an
        # explicit "not reported" state immediately rather than silently
        # preserving a months-old stale flag from the prior season.
        return {
            "wbgt_f": None,
            "flag": "white",
            "label": "Not reported this season",
            "minutes_per_hour": None,
            "reported": False,
            "as_of": now.isoformat(),
        }

    wbgt_c = _value_at(layer, now)
    if wbgt_c is None:
        raise RuntimeError(
            "wetBulbGlobeTemperature layer present but no value covers "
            "'now' -- treating as a transient gap, not off-season absence."
        )
    wbgt_f = c_to_f(wbgt_c)
    label, minutes = classify_work(wbgt_f)
    return {
        "wbgt_f": round(wbgt_f, 1),
        "flag": classify_wbgt_flag(wbgt_f),
        "label": label,
        "minutes_per_hour": minutes,
        "reported": True,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: umbrella vs raincoat
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


def _format_local_window(start: dt.datetime, end: dt.datetime, tz: ZoneInfo) -> str:
    fmt = "%-I%p"
    start_s = start.astimezone(tz).strftime(fmt).lower()
    end_s = end.astimezone(tz).strftime(fmt).lower()
    return f"{start_s}–{end_s}"


def compute_rain_answer(grid: dict, now: dt.datetime, lookahead_hours: int,
                         windy: bool, tz: ZoneInfo) -> dict:
    end = now + dt.timedelta(hours=lookahead_hours)
    pop_hits = _values_in_window(grid.get("probabilityOfPrecipitation"), now, end)
    weather_hits = _values_in_window(grid.get("weather"), now, end)

    pop_values = [v for _, _, v in pop_hits if v is not None]
    max_pop = max(pop_values) if pop_values else 0.0
    frozen_only_everywhere = bool(weather_hits) and all(
        _is_frozen_only(v) for _, _, v in weather_hits
    )

    rain_likely = (max_pop >= UMBRELLA_POP_THRESHOLD) and not frozen_only_everywhere
    if not rain_likely:
        action = "none"
    elif windy:
        action = "raincoat"
    else:
        action = "umbrella"

    likely_hits = [(s, e) for s, e, v in pop_hits
                   if v is not None and v >= UMBRELLA_POP_THRESHOLD]
    window_label = None
    if likely_hits:
        window_start = min(s for s, e in likely_hits)
        window_end = max(e for s, e in likely_hits)
        window_label = _format_local_window(window_start, window_end, tz)

    return {
        "max_pop_pct": round(max_pop, 0),
        "action": action,
        "window_label": window_label,
        "frozen_only": frozen_only_everywhere,
        "window_hours": lookahead_hours,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: thunderstorm advisory
# --------------------------------------------------------------------------
# NWS-native only, same as everything else here -- no SPC convective
# outlook (marginal/slight/enhanced/moderate/severe) integration. That's
# a genuinely different data source (Storm Prediction Center, not
# api.weather.gov gridpoint data); "severity" is left out of the output
# entirely rather than faked, until/unless that's worth adding as its
# own separate integration.

def compute_storm_answer(grid: dict, now: dt.datetime, lookahead_hours: int,
                          tz: ZoneInfo) -> dict:
    end = now + dt.timedelta(hours=lookahead_hours)
    hits = _values_in_window(grid.get("probabilityOfThunder"), now, end)
    values = [v for _, _, v in hits if v is not None]
    max_pot = max(values) if values else 0.0
    expected = max_pot >= STORM_POT_THRESHOLD

    window_label = None
    if expected:
        likely_hits = [(s, e) for s, e, v in hits if v is not None and v >= STORM_POT_THRESHOLD]
        window_start = min(s for s, e in likely_hits)
        window_end = max(e for s, e in likely_hits)
        window_label = _format_local_window(window_start, window_end, tz)

    return {
        "expected": expected,
        "max_pot_pct": round(max_pot, 0),
        "window_label": window_label,
        "window_hours": lookahead_hours,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: windbreaker / layers (wind)
# --------------------------------------------------------------------------

def compute_wind_answer(grid: dict, now: dt.datetime, lookahead_hours: int) -> dict:
    end = now + dt.timedelta(hours=lookahead_hours)
    speed_hits = _values_in_window(grid.get("windSpeed"), now, end)
    gust_hits = _values_in_window(grid.get("windGust"), now, end)

    speeds = [v for _, _, v in speed_hits if v is not None]
    gusts = [v for _, _, v in gust_hits if v is not None]
    max_speed_mph = kmh_to_mph(max(speeds)) if speeds else 0.0
    max_gust_mph = kmh_to_mph(max(gusts)) if gusts else 0.0

    windy = (max_speed_mph >= WINDY_SUSTAINED_MPH) or (max_gust_mph >= WINDY_GUST_MPH)
    return {
        "sustained_mph": round(max_speed_mph, 0),
        "gust_mph": round(max_gust_mph, 0),
        "windy": windy,
        "window_hours": lookahead_hours,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: what to wear (feels-like range) + optional air high/low
# --------------------------------------------------------------------------

def compute_feels_like_answer(grid: dict, now: dt.datetime, tz: ZoneInfo) -> dict:
    day_start, day_end = local_day_window(now, tz)
    window_start = max(day_start, now)
    hits = _values_in_window(grid.get("apparentTemperature"), window_start, day_end)
    vals_f = [c_to_f(v) for _, _, v in hits if v is not None]
    if not vals_f:
        raise RuntimeError("No apparentTemperature values for the rest of today.")
    return {
        "low_f": round(min(vals_f), 0),
        "high_f": round(max(vals_f), 0),
        "as_of": now.isoformat(),
    }


def compute_air_answer(grid: dict, now: dt.datetime, tz: ZoneInfo) -> dict:
    day_start, day_end = local_day_window(now, tz)
    max_hits = _values_in_window(grid.get("maxTemperature"), day_start, day_end)
    min_hits = _values_in_window(grid.get("minTemperature"), day_start, day_end)
    max_vals = [c_to_f(v) for _, _, v in max_hits if v is not None]
    min_vals = [c_to_f(v) for _, _, v in min_hits if v is not None]
    if not max_vals and not min_vals:
        raise RuntimeError("No min/max air temperature values for today.")
    return {
        "high_f": round(max(max_vals), 0) if max_vals else None,
        "low_f": round(min(min_vals), 0) if min_vals else None,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: sunscreen / hat (UV)
# --------------------------------------------------------------------------

def classify_uv(uv_index: float) -> str:
    for threshold, label in UV_BANDS:
        if uv_index < threshold:
            return label
    return UV_BANDS[-1][1]


def _find_uv_value(payload):
    """Defensive parse -- confirmed live the real field is UV_INDEX, which
    this scan already matches ("uv" + "index" in the lowercased key), so
    no hard-coding needed. Kept defensive rather than hard-coded in case
    EPA changes the schema; see PROJECT_STATUS.md Check 2."""
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
    return {
        "uv_index": uv,
        "label": classify_uv(uv),
        "show": uv >= UV_SHOW_THRESHOLD,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Answer: air quality (AQI)
# --------------------------------------------------------------------------
# EPA AirNow, per WEATHER_FEED_CONTRACT.md. Requires AIRNOW_API_KEY --
# a real credential, not a schema guess: confirmed live against ZIP 23221
# on 2026-07-07. Response is a list of per-pollutant records (O3, PM2.5,
# PM10, ...); the reported AQI for a location is the max across
# pollutants (EPA's own NowCast convention), not any single one of them.

def compute_air_quality_answer(zip_code: str, now: dt.datetime) -> dict:
    if not AIRNOW_API_KEY:
        raise RuntimeError("AIRNOW_API_KEY not set -- air quality answer skipped.")
    url = (
        "https://www.airnowapi.org/aq/observation/zipCode/current/"
        f"?format=application/json&zipCode={zip_code}&distance=25&API_KEY={AIRNOW_API_KEY}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    records = r.json()
    if not records:
        raise RuntimeError("AirNow returned no observation records for this ZIP.")
    dominant = max(records, key=lambda rec: rec.get("AQI", 0))
    aqi = dominant["AQI"]
    return {
        "aqi": aqi,
        "category": dominant.get("Category", {}).get("Name", "Unknown"),
        "show": aqi >= AQI_SHOW_THRESHOLD,
        "as_of": now.isoformat(),
    }


# --------------------------------------------------------------------------
# Tier 1: the verdict line
# --------------------------------------------------------------------------
# Deliberately rule-based and capped at two clauses, in priority order:
# storm > WBGT flag > air quality (safety) > rain action > windy > sun.
# Storm leads -- matches the Eos-side contract's "highest default
# priority" for thunderstorm advisories, and outranks the rest on plain
# safety grounds. WBGT and air quality are both grouped as health
# hazards, ahead of rain/wind/sun which are comfort/prep concerns, not
# hazards. This differs slightly from the illustrative ordering in the
# original design doc (which led with sun in one example) -- safety-
# relevant heat/exertion guidance outranks a sunscreen reminder here. No
# qualitative "cool"/"hot" descriptor is synthesized (e.g. the doc's
# "windy and cool") because no threshold for that was supplied; only
# "windy" itself is asserted, since that threshold was. Revisit if a
# feels-like qualitative band is ever wanted.

def compute_verdict(work: dict, rain: dict, wind: dict, uv: dict, storm: dict,
                     aq: dict) -> str:
    clauses = []

    if storm and storm.get("expected"):
        label = "Thunderstorms expected"
        if storm.get("window_label"):
            label += f" ({storm['window_label']})"
        label += " — head indoors"
        clauses.append(label)

    flag = work.get("flag") if work else None
    if flag in ("red", "black"):
        clauses.append(f"hydrate, limit exertion — WBGT {flag}")

    if aq and aq.get("show"):
        clauses.append(f"air quality {aq['category'].lower()} (AQI {aq['aqi']}) — limit time outside")

    action = rain.get("action") if rain else None
    if action in ("raincoat", "umbrella"):
        label = "Bring a raincoat" if action == "raincoat" else "Bring an umbrella"
        if rain.get("window_label"):
            label += f" ({rain['window_label']})"
        clauses.append(label)

    if wind and wind.get("windy"):
        clauses.append("windy")

    if uv and uv.get("show"):
        clauses.append(uv["label"])

    if not clauses:
        return "No extra prep needed today."

    text = "; ".join(clauses[:2])
    return text[0].upper() + text[1:] + "."


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
    tz = ZoneInfo(FALLBACK_TZ)
    try:
        meta = get_point_metadata(LAT, LON)
        grid = get_gridpoint_data(meta["grid_url"])
        tz = ZoneInfo(meta["timezone"])
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
            result["wind"] = compute_wind_answer(grid, now, LOOKAHEAD_HOURS)
        except Exception as e:
            print(f"Wind answer failed: {e}", file=sys.stderr)
            had_failure = True
        windy = result.get("wind", {}).get("windy", False)
        try:
            result["rain"] = compute_rain_answer(grid, now, LOOKAHEAD_HOURS, windy, tz)
        except Exception as e:
            print(f"Rain answer failed: {e}", file=sys.stderr)
            had_failure = True
        try:
            result["feels_like"] = compute_feels_like_answer(grid, now, tz)
        except Exception as e:
            print(f"Feels-like answer failed: {e}", file=sys.stderr)
            had_failure = True
        try:
            result["air"] = compute_air_answer(grid, now, tz)
        except Exception as e:
            print(f"Air high/low answer failed: {e}", file=sys.stderr)
            had_failure = True
        try:
            result["storm"] = compute_storm_answer(grid, now, LOOKAHEAD_HOURS, tz)
        except Exception as e:
            print(f"Storm answer failed: {e}", file=sys.stderr)
            had_failure = True
    else:
        had_failure = True

    try:
        result["sun"] = compute_sun_answer(ZIP, now)
    except Exception as e:
        print(f"Sun-protection answer failed: {e}", file=sys.stderr)
        had_failure = True

    try:
        result["air_quality"] = compute_air_quality_answer(ZIP, now)
    except Exception as e:
        print(f"Air-quality answer failed: {e}", file=sys.stderr)
        had_failure = True

    try:
        result["verdict"] = compute_verdict(
            result.get("work", {}), result.get("rain", {}),
            result.get("wind", {}), result.get("sun", {}),
            result.get("storm", {}), result.get("air_quality", {}),
        )
    except Exception as e:
        print(f"Verdict synthesis failed: {e}", file=sys.stderr)
        result["verdict"] = result.get("verdict", "No extra prep needed today.")
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
