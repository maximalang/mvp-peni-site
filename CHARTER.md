# Project Charter — SEO Utility Site

## Problem

Russian individuals and individual entrepreneurs need narrow calculation utilities whose formula, source dates, applicability limits, and provenance are visible rather than hidden behind an unverifiable answer.

## Initial customer and job

- Customer: an individual or individual entrepreneur checking tax-penalty accrual for a known debt and delay period.
- Job: obtain a reproducible client-side calculation based on the applicable Central Bank key-rate history, with explicit limitations and no collection of personal data.

## Product hypothesis

An evidence-first calculator can win useful organic demand by being narrower and more transparent than generic penalty calculators. The first validation slice is tax penalties plus historical CBR key rates; court fees and simplified-tax utilities remain gated expansions.

## Primary metric

`successful_organic_calculations_28d` — completed valid calculations during the latest 28-day window from organic landings. Instrumentation must not capture entered amounts, dates, identifiers, or other PII.

Current baseline: `0` because there is no verified production deployment.

## Phase

Pre-launch / local MVP. Registration is a GO for controlled validation, not a GO for publication, paid acquisition, or broad SEO expansion.

## Scope

### In

- one tax-penalty calculator for individuals/IP;
- CBR key-rate provenance and fail-closed source validation;
- a small set of distinct supporting intent pages;
- tests for formula, source integrity, privacy boundary, SEO structure, accessibility, and rendered behavior;
- an organic pilot after publication gates pass.

### Out until separate evidence gate

- legal-entity formulas;
- court fees, USN, mortgage, deposits, and other calculator families;
- accounts, payments, personal-data storage, server-side calculation, mass pages, paid ads, or legal guarantees.

## Current gate

Restore the failing duplicate-source-schedule integrity check, obtain independent QA on the exact commit, then close legal/source-use, canonical-host, deployment, backup, rollback, and runtime smoke gates before publication.

## Kill criteria

Pause publication or expansion if a verified formula/source defect remains unresolved, source reuse is not permitted, two comparable demand snapshots materially invalidate the tax-specific cluster, or the rendered SERP already satisfies the same narrow job without a click. Archive rather than continue activity without metric evidence.

## Owner

Accountable owner: `company`. Initial delivery squad: `tech` + independent `qa`; add `research`, `operations`, or `finance` only when their evidence gate is active.
