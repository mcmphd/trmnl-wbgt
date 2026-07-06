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

## NOT tested — needs a real environment

- [ ] **Check 1**: Is `wetBulbGlobeTemperature` actually populated for
      your real grid cell? (`api.weather.gov` blocked automated fetch
      from the sandbox.) Run the curl in README.md "Check 1" for your
      *real* lat/lon, not the Richmond-center placeholder still in the
      code.
- [ ] **Check 2**: Does `_find_uv_value()`'s defensive key-scan actually
      match the EPA UV JSON response's real field name? Run the curl in
      README.md "Check 2" for ZIP 23221. If it returns `None` every run
      in practice, hard-code the real key.
- [ ] Does the `weather` layer's condition strings actually match the
      `FROZEN_ONLY_KEYWORDS`/`LIQUID_KEYWORDS` sets in
      `_is_frozen_only()`? Spot-check against a real winter forecast.
- [ ] Grayscale severity styling in `templates/full.liquid` — verified
      correct *logic* (which color maps to which `minutes_per_hour`), not
      verified visually on real TRMNL hardware.

## Remaining setup steps, in order

1. [ ] Replace the placeholder `WBGT_LAT`/`WBGT_LON` in `wbgt_trmnl.py`
       (or better, only set them as repo variables, never hard-code) with
       real coordinates for the location this should track.
2. [ ] `git init`, create the GitHub repo, first commit, push. Public
       repo recommended — nothing sensitive in code, and it gets
       unlimited Actions minutes vs. a private repo's monthly cap.
3. [ ] Run Check 1 and Check 2 above for real, before wiring anything
       else together.
4. [ ] Repo secret: `NWS_USER_AGENT`.
5. [ ] Repo variables: `WBGT_LAT`, `WBGT_LON`, `WBGT_ZIP=23221`.
6. [ ] Trigger the workflow manually (`workflow_dispatch`), confirm
       `data/latest.json` gets committed with real, sane values.
7. [ ] TRMNL: create the Private Plugin, Strategy = Polling, URL = the
       raw GitHub URL for `data/latest.json`, `refresh_interval = 360`.
       Paste `templates/full.liquid` into the markup editor.
8. [ ] Optional: set up the Scriptable widget from README.md, test on an
       actual device — the JS snippet there is unverified against a real
       widget render.

## Files in this project

- `wbgt_trmnl.py` — the compute script (single dependency: `requests`)
- `requirements.txt`
- `.github/workflows/wbgt.yml` — 6-hour cron, commits `data/latest.json`
- `templates/full.liquid` — TRMNL markup
- `README.md` — full rationale, setup guide, design decisions, caveats
