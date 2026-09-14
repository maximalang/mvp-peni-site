# Project State — SEO Utility Site

Updated: 2026-09-13 RTZ

## Phase

Pre-launch / local MVP. Not published and not approved for paid acquisition or broad SEO expansion.

## Primary metric

`successful_organic_calculations_28d = 0` (no verified production deployment).

Measurement is now implemented first-party and consent-safe (decision 2026-09-13, see
`decisions/2026-09-13-first-party-organic-measurement.md`): event on successful calculation
only; fields `ts/v/calc/attr/ok/did/syn`; anonymous dedupe id in first-party localStorage;
no amounts, dates, PII, cookies, third parties, or server sends. Synthetic readback events
are separated from the business numerator. Readback harness: `node tools/readback_measure.mjs`.
The metric stays 0 until a verified live deployment produces real organic events.

## Existing artifact

- Repository: `https://github.com/maximalang/mvp-peni-site`
- Inspected commit: `d8e92b57f83d7392b818e58d874723ae06a35bfc`
- Current implementation: four static HTML pages, client-side calculator, key-rate data artifact, refresh tool, sitemap/robots, and tests.
- Demand evidence is screening only: the 2026-08-25 family sum was `319,617`, explicitly not a traffic or CTR forecast.

## Verification snapshot

Command: `node --test tests/*.test.mjs`

- Exit code: `0`
- Calculator fixtures: `11/11` passed (duplicate-schedule fail-closed restored in `dbcde68`).
- Measurement suite: `19/19` passed (privacy allowlist, dedupe id, organic attribution, 28d window, synthetic separation, storage cap, hostile-storage degradation).
- SEO test suite: passed for four HTML pages, sitemap, and robots.

Command: `node tools/readback_measure.mjs --synthetic 1`

- Exit code: `0`; synthetic/business separation assertions PASS.

Command: `python -m py_compile tools/refresh_rates.py`

- Exit code: `0`

## Active risks/blockers

1. Publication gates remain open for source-use/legal wording, real hostname/canonicals, deployment smoke, rollback, and backup.
2. Independent QA has not yet accepted the measurement commit (child card t_1146fd0b).
3. There is no verified live URL, indexation evidence, organic traffic, or non-zero metric evidence yet.

## Next action

`qa` accepts the measurement commit on its exact head (tests + readback harness + privacy boundary); then publication gates proceed per `docs/cloudflare-pages-deploy.md`. After a verified live deployment, run the readback check with a synthetic event to confirm separation in production.
