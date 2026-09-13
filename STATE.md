# Project State — SEO Utility Site

Updated: 2026-08-30 RTZ

## Phase

Pre-launch / local MVP. Not published and not approved for paid acquisition or broad SEO expansion.

## Primary metric

`successful_organic_calculations_28d = 0` (no verified production deployment).

## Existing artifact

- Repository: `https://github.com/maximalang/mvp-peni-site`
- Inspected commit: `d8e92b57f83d7392b818e58d874723ae06a35bfc`
- Current implementation: four static HTML pages, client-side calculator, key-rate data artifact, refresh tool, sitemap/robots, and tests.
- Demand evidence is screening only: the 2026-08-25 family sum was `319,617`, explicitly not a traffic or CTR forecast.

## Verification snapshot

Command: `node --test tests/*.test.mjs`

- Exit code: `1`
- Calculator fixtures: `10/11` passed.
- Failure: `duplicate_schedule_rejected` expected `source_integrity` but returned a calculation.
- SEO test suite: passed for four HTML pages, sitemap, and robots.

Command: `python -m py_compile tools/refresh_rates.py`

- Exit code: `0`

## Active risks/blockers

1. **Publication blocker:** duplicate/ambiguous source schedule is not currently rejected fail-closed.
2. Independent QA has not accepted the exact corrected commit.
3. Publication gates remain open for source-use/legal wording, privacy boundary, real hostname/canonicals, deployment smoke, rollback, and backup.
4. There is no verified live URL, indexation evidence, organic traffic, completed-calculation metric, or revenue evidence.

## Next action

`tech` fixes the duplicate-schedule source-integrity regression without weakening other fixtures; `qa` then reruns the exact-head calculator, SEO, privacy, and rendered smoke checks. Publication stays blocked until both tasks pass.
