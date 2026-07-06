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
| Workload assumption | Moderate, acclimatized (ACGIH table) |
| Umbrella threshold | 30% PoP (minimax-regret, not 50/50) |
| Rain look-ahead window | 6 hours (matches update cadence) |
| Update cadence | Every 6 hours (`cron: 0 */6 * * *`) |
| UV data source | EPA Envirofacts, by ZIP `23221` |
| Architecture | Shared JSON (`data/latest.json`) via GitHub Actions commit, read by TRMNL Polling + Scriptable — not a webhook |

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
