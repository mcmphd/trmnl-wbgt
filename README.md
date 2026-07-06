# Outdoor Conditions: TRMNL + phone widget

Answers three questions, not a metric:

1. **Should I work outside, and for how long?** (WBGT, NWS-native only)
2. **Do I need an umbrella?** (NWS precipitation probability + type)
3. **Do I need sun protection?** (EPA UV Index)

Runs on GitHub Actions every 6 hours, writes one JSON file, committed back
to the repo. TRMNL's Polling strategy and a Scriptable phone widget both
read that same file directly — one computed value, two displays, no drift
between them, no webhook, no secret UUID to manage.

## Status: tested where it can be, unverified where it can't

Everything testable offline **was** tested: ISO8601 duration parsing, the
ACGIH work classification, UV banding, the precip-type filter, and —
specifically — all three failure-isolation scenarios (NWS down, EPA down,
both up), confirming that a failed data source keeps its last-known-good
value instead of going blank or silently overwriting with a guess.

Two things could not be verified live, because `api.weather.gov` blocks
automated fetches with `robots.txt` and this environment's `web_fetch`
tool can't be pointed at arbitrary constructed URLs. Do these once:

### Check 1 — is `wetBulbGlobeTemperature` actually populated for your grid cell?

```bash
curl -s -H "User-Agent: (your-app, you@example.com)" \
  "https://api.weather.gov/points/YOUR_LAT,YOUR_LON" | grep forecastGridData
# fetch that URL, look for "wetBulbGlobeTemperature" with a non-null value
```

If it's null or absent, the "work outside" answer will fail every run and
stay stale (by design — no Liljegren fallback, see below). The other two
answers are unaffected.

### Check 2 — does the EPA UV endpoint actually return a field named anything with "uv" and "index" in it?

```bash
curl -s "https://data.epa.gov/dmapservice/getEnvirofactsUVDAILY/ZIP/23221/JSON"
```

I confirmed this endpoint is live and requires no API key — I fetched the
default-format version directly. I could **not** confirm the `/JSON`
variant's exact field names (couldn't construct that specific URL under
this tool's fetch restrictions). `_find_uv_value()` in `wbgt_trmnl.py`
scans defensively for any key containing "uv" + "index" rather than
hard-coding a guessed name — run the curl above once, and if the real key
doesn't match that pattern, hard-code it directly in that function.

## Setup

1. **Repo secrets** (Settings → Secrets and variables → Actions → Secrets):
   - `NWS_USER_AGENT` — e.g. `(your-name, you@email.com)`. NWS requires a
     descriptive User-Agent.
2. **Repo variables** (same page, Variables tab):
   - `WBGT_LAT`, `WBGT_LON` — your real coordinates (placeholder ships
     near central Richmond, VA)
   - `WBGT_ZIP` — `23221`
3. **Enable the workflow.** Trigger a manual run (Actions tab →
   workflow_dispatch) to test immediately rather than waiting 6 hours.
4. **TRMNL plugin: Polling, not Webhook.** Strategy = Polling, Polling URL
   = the raw GitHub URL for `data/latest.json`, e.g.
   `https://raw.githubusercontent.com/USER/REPO/main/data/latest.json`,
   refresh interval = 360 (6 hours — matches the cron exactly). Paste
   `templates/full.liquid` into the markup editor.
5. **Phone widget (optional):** see below.

## Phone widget via Scriptable

Same JSON, same URL, no webhook, no NWS/EPA calls on-device:

```javascript
// Scriptable widget script
const url = "https://raw.githubusercontent.com/USER/REPO/main/data/latest.json";
const data = await new Request(url).loadJSON();

const w = new ListWidget();
w.backgroundColor = new Color("#ffffff");

function line(text, size, bold) {
  const t = w.addText(text);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
}

line(data.work?.minutes_per_hour === 60 ? "Work freely"
     : data.work?.minutes_per_hour === 0 ? "Don't work outside"
     : `${data.work?.minutes_per_hour ?? "?"} min/hr`, 20, true);
w.addSpacer(6);
line(data.rain?.bring_umbrella ? "☔ Bring umbrella" : "No umbrella needed", 14, false);
line(data.sun?.label ?? "No data", 14, false);

Script.setWidget(w);
Script.complete();
```

Add a Scriptable widget to your Home Screen, point it at this script.
Refresh cadence is whatever iOS's WidgetKit budget allows — realistically
15–60 minutes, same ceiling every app in this space hits regardless of
which one you pick. That's an OS constraint, not a Scriptable limitation.

## Design decisions worth knowing about

**NWS-only WBGT, no fallback.** An earlier version of this repo computed
WBGT itself (Liljegren et al. 2008, via `pywbgt`) whenever NWS's native
grid layer was missing. That code was cut — NWS made WBGT operational,
nationally, on NDFD, in 2022; the fallback was solving a problem that very
likely didn't exist for this location, at the cost of a C-extension build
and a real numba/pvlib packaging bug that ate an hour of debugging before
anyone had confirmed the fallback was even needed. If Check 1 above comes
back null for your grid cell, that's a real signal to reconsider — not a
reason to silently reintroduce Liljegren.

**Workload assumption.** The work/rest table assumes **moderate workload,
acclimatized**. ACGIH's own tables run roughly 4–8°F more conservative for
unacclimatized workers and light work respectively — the script has no way
to know which applies on a given day. Worth remembering on the first hot
week of the season, or the first day back after time off.

**Umbrella threshold is 30%, not 50%.** Minimax regret, not expected
value: carrying an umbrella you didn't need costs a few ounces; not having
one when it rains costs an afternoon. The asymmetry justifies a threshold
below the naive midpoint.

**Rain/umbrella looks 6 hours ahead, not just the current hour** — matches
the update cadence, so "should I grab an umbrella" reflects the whole gap
until the next refresh, not just this exact moment.

**Precip-type filtering is a soft refinement, explicitly flagged as
unverified.** `_is_frozen_only()` keyword-matches the NDFD `weather`
layer's condition strings to avoid recommending an umbrella for
snow-only forecasts. The exact string vocabulary wasn't confirmed against
a live response — same `robots.txt` constraint as Check 1. If it
misclassifies a real snow day, that's why.

**Every answer fails independently.** A dead EPA endpoint doesn't blank
the umbrella call, and a dead NWS endpoint doesn't blank sun protection.
Each keeps its last committed value until its own next successful fetch.
