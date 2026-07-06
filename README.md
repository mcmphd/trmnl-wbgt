# Outdoor Conditions: TRMNL + phone widget

A daily weather briefing: a synthesized verdict line, not a dashboard.
Design follows `Eos/docs/WEATHER_BRIEFING.md` (the Eos/Helios weather
briefing draft) — organized around the **decisions** it answers, gated so
a metric only appears once it crosses a line:

| Decision | Inputs |
|---|---|
| Hydrate / limit exertion | WBGT (flag + value), NWS-native only |
| Umbrella **or** raincoat | rain probability + type + wind |
| Windbreaker / layers | wind gusts |
| Sunscreen / hat | UV Index (EPA), gated to high+ only |
| What to wear | feels-like range (NWS `apparentTemperature`) |

Runs on GitHub Actions every 6 hours, writes one JSON file, committed back
to the repo. TRMNL's Polling strategy and a Scriptable phone widget both
read that same file directly — one computed value, two displays, no drift
between them, no webhook, no secret UUID to manage.

## Output schema

```jsonc
{
  "verdict": "Bring an umbrella (5pm–11pm); Minimize exposure 10am-4pm.",
  "work": { "wbgt_f": 85.0, "flag": "yellow", "label": "25% work / 75% rest",
            "minutes_per_hour": 15 },
  "rain": { "max_pop_pct": 68, "action": "umbrella", "window_label": "5pm–11pm",
            "frozen_only": false, "window_hours": 6 },
  "wind": { "sustained_mph": 3, "gust_mph": 8, "windy": false, "window_hours": 6 },
  "feels_like": { "low_f": 83, "high_f": 100 },
  "air": { "high_f": 94, "low_f": 72 },
  "sun": { "uv_index": 10.0, "label": "Minimize exposure 10am-4pm", "show": true },
  "generated_at": "2026-07-06T20:23:12+00:00"
}
```

`verdict` is Tier 1 — the imperative headline, synthesized once in Python
(single source of truth, not duplicated Liquid logic across four
templates). Everything else is Tier 2 — supporting detail, each gated on
its own threshold. `rain.action` is `"none"`, `"umbrella"`, or
`"raincoat"` (raincoat when rain is likely **and** windy). `work.flag` is
`white`/`green`/`yellow`/`red`/`black`; see "WBGT flag cutoffs" below for
an important caveat on the `white` tier. `sun.show` and `wind.windy` gate
whether their Tier-2 chip renders at all.

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
   refresh interval = 360 (6 hours — matches the cron exactly). The markup
   editor has a separate tab per layout size — paste the matching file
   into each:
   - Full (800x480) → `templates/full.liquid`
   - Half horizontal (800x240) → `templates/half_horizontal.liquid`
   - Half vertical (400x480) → `templates/half_vertical.liquid`
   - Quadrant (400x240) → `templates/quadrant.liquid`

   All four use the same severity logic (grayscale fill on the
   work-outside card, keyed to `minutes_per_hour`) and the same JSON
   fields — only the layout and font sizes change per size.
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

line(data.verdict ?? "No data", 16, true);
w.addSpacer(6);
if (data.feels_like) {
  line(`Feels ${Math.round(data.feels_like.low_f)}–${Math.round(data.feels_like.high_f)}°F`, 13, false);
}

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
The verdict line is re-synthesized every run from whatever's currently in
the result — a mix of fresh and stale answers — rather than being gated
on every input succeeding this run.

**WBGT flag cutoffs — Military TB MED 507, with one caveat.** Green
80–84.9°F, Yellow 85–87.9°F, Red 88–89.9°F, Black ≥90°F is the real
published standard. The `white` tier below 80°F is **not** part of the
standard — I added it myself so there's a "nothing to report" bucket
below Green, which is a common practical extension but shouldn't be
read as official. `work.flag == "white"` is the only flag value that
suppresses the WBGT Tier-2 chip.

**"Windy" is 15 mph sustained or 25 mph gust, user-supplied.** Feeds two
decisions: the windbreaker/layers signal, and flipping the rain call from
umbrella to raincoat. Both use the same wind reading over the same
6-hour lookahead window as the rain answer, rather than two separate
wind windows.

**Feels-like and air high/low span "the rest of today," not the 6-hour
lookahead.** NWS already publishes `apparentTemperature` (feels-like,
folds in humidity and wind chill — confirmed live, no derivation needed)
and daily `maxTemperature`/`minTemperature` layers. Both are windowed to
the local calendar day (via the timezone NWS returns for the gridpoint),
clipped to now, so the range narrows as the day goes on rather than
re-showing hours that have already passed.

**Verdict synthesis is a simple, capped rule, not NLG.** Priority order:
WBGT flag (red/black only) > rain action > windy > sun. At most two
clauses render, joined with "; ". Two things it deliberately does *not*
do, for lack of a supplied threshold: it never asserts a qualitative
"cool"/"hot" descriptor (the design doc's example verdict says "windy and
cool" — only "windy" is asserted here, since only that threshold was
given), and it doesn't compute a time-of-day like "midday" for the WBGT
clause (the doc's other example says "WBGT red midday" — this only
reports the flag itself). If you want either of those, they need their
own threshold decisions first, same as the flag cutoffs did.
