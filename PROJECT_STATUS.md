# Project Status — Outdoor Conditions (WBGT / Rain / UV)

Handoff brief for picking this up in Claude Code or a terminal session.
Everything below was designed and logic-tested in a chat sandbox that
cannot create a real repo, push to GitHub, hit `api.weather.gov` (blocked
by `robots.txt` for automated fetches), or touch a real TRMNL device. This
document is the map of what's already decided vs. what still needs a real
environment to finish.

## Decisions already reviewed — do not silently change these

These were explicitly discussed and approved. If a reason emerges to
revisit one, surface it and ask — don't just change it.

| Decision | Value |
|---|---|
| WBGT source | NWS-native only, no Liljegren/pywbgt fallback |
| Workload assumption | Moderate, acclimatized (ACGIH table) — kept as a Tier-2 supporting detail, not the primary framing |
| Umbrella threshold | 30% PoP (minimax-regret, not 50/50) |
| Rain look-ahead window | 6 hours (matches update cadence) |
| Update cadence | Every 6 hours (`cron: 0 */6 * * *`) |
| UV data source | EPA Envirofacts, by ZIP `23221` |
| Architecture | Shared JSON (`data/latest.json`) via GitHub Actions commit, read by TRMNL Polling + Scriptable — not a webhook |
| Design framework | Decisions-first daily briefing, per `Eos/docs/WEATHER_BRIEFING.md` (2026-07-06). Tier 1 verdict + gated Tier 2 chips, replacing the original 3-card dashboard layout. |
| WBGT flag cutoffs | Military TB MED 507: Green 80–84.9°F, Yellow 85–87.9°F, Red 88–89.9°F, Black ≥90°F. `white` (<80°F) is our own addition below the standard's floor — not official. |
| Windy threshold | 15 mph sustained or 25 mph gust (user-supplied) — drives windbreaker signal and umbrella→raincoat flip |
| UV show gate | `uv_index >= 3` (reuses existing `UV_BANDS` first threshold) |
| Feels-like / air window | Local calendar day, clipped to now (not the 6h rain lookahead) |

## Tested (logic only, in-sandbox)

- ISO8601 duration parsing (`parse_iso_duration`)
- ACGIH work classification (`classify_work`) against the real table
- UV banding (`classify_uv`)
- Frozen-precip filter (`_is_frozen_only`)
- All three failure-isolation paths in `run()`: NWS down, EPA down, both
  up — confirmed stale values survive untouched, nothing goes blank

## Verified live, 2026-07-06 (Claude Code, not sandbox-restricted)

- [x] **Check 1**: `wetBulbGlobeTemperature` is populated for the real
      grid cell (AKQ/46,77, Richmond VA). Confirmed via live curl —
      values in the 23–30°C range for today.
- [x] **Check 2**: EPA UV response field is `UV_INDEX` (e.g.
      `{"ZIP_CODE":"23221",...,"UV_INDEX":"10",...}`). Matches
      `_find_uv_value()`'s defensive scan (`"uv" in key and "index" in
      key`) without any hard-coding needed.
- [x] `weather` layer condition strings use snake_case tokens
      (`rain_showers`, `thunderstorms`) — substring matching in
      `_is_frozen_only()` against `LIQUID_KEYWORDS`/`FROZEN_ONLY_KEYWORDS`
      works correctly against real values. Only liquid-precip tokens seen
      so far (July); frozen-token matching (`snow`, `ice`, `sleet`) not
      yet observed live — revisit on the first winter forecast.
- [x] End-to-end run confirmed: `workflow_dispatch` succeeded, committed
      `data/latest.json` with real, sane values (WBGT 84°F → 50/50
      work-rest, 69% PoP → bring umbrella, UV 10 → minimize exposure).
- [ ] Grayscale severity styling in `templates/full.liquid` — still only
      logic-verified, not seen on real TRMNL hardware.

## 2026-07-06 redesign: daily weather briefing

Repurposed for Eos/Helios's decisions-first briefing design (see
`Eos/docs/WEATHER_BRIEFING.md`). `wbgt_trmnl.py` now also computes wind
(`compute_wind_answer`), feels-like (`compute_feels_like_answer`), air
high/low (`compute_air_answer`), a WBGT flag (`classify_wbgt_flag`), and
a synthesized `verdict` line (`compute_verdict`) — see README.md "Output
schema" and "Design decisions" for the full breakdown and caveats
(TB MED 507 `white` tier, no qualitative feels-like descriptor, no
computed time-of-day in the WBGT clause). All four templates
(`full`/`half_vertical`/`half_horizontal`/`quadrant`) rewritten around
Tier 1 (verdict) + Tier 2 (gated chips), replacing the original 3-card
layout.

Verified locally (venv, `python-liquid`) before pushing: script runs
end-to-end against real NWS/EPA data, all four templates parse and
render against that real output. Not yet verified: live GitHub Actions
run with the new script, or the new layout on real TRMNL hardware.

## Remaining setup steps, in order

1. [x] Real coordinates confirmed as Richmond, VA (23221) — same as the
       placeholder, so `wbgt_trmnl.py` defaults were left as-is. Repo
       variables (below) are the actual source of truth.
2. [x] `git init`, GitHub repo created and pushed:
       https://github.com/mcmphd/trmnl-wbgt (public).
3. [x] Check 1 and Check 2 run for real — see above.
4. [x] Repo secret `NWS_USER_AGENT` set.
5. [x] Repo variables `WBGT_LAT=37.5407`, `WBGT_LON=-77.4360`,
       `WBGT_ZIP=23221` set.
6. [x] Workflow triggered manually, `data/latest.json` committed with
       real values — confirmed above.
7. [ ] TRMNL: create the Private Plugin, Strategy = Polling, URL =
       `https://raw.githubusercontent.com/mcmphd/trmnl-wbgt/main/data/latest.json`,
       `refresh_interval = 360`. Paste `templates/full.liquid` into the
       markup editor. Needs the TRMNL account — not doable from this
       environment.
8. [ ] Optional: set up the Scriptable widget from README.md (URL above),
       test on an actual device — the JS snippet there is unverified
       against a real widget render.

## Files in this project

- `wbgt_trmnl.py` — the compute script (single dependency: `requests`)
- `requirements.txt`
- `.github/workflows/wbgt.yml` — 6-hour cron, commits `data/latest.json`
- `templates/full.liquid` — TRMNL markup
- `README.md` — full rationale, setup guide, design decisions, caveats
