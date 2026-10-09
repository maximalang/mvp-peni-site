#!/usr/bin/env python3
"""Build the synthetic edge-case corpus for the peni calculator (js/calc.js).

Pipeline (Q2 quality-program, variant-2):
  1. A model (ordinary rail) generates CANDIDATE INPUTS only — arrears amounts,
     delay dates, payer types, category tags. Model output is never trusted for
     expected values.
  2. This script validates every candidate against the input contract, dedupes
     by sha256 of the canonical input tuple, then computes the expected result
     with a DETERMINISTIC ORACLE: an independent reimplementation of the
     Article 75(4)(1) NK RF penalty formula (arrears x key-rate/300 per day,
     inclusive day count, HALF_UP kopeck rounding) using exact Fraction
     arithmetic, plus a mirror of js/calc.js's validation contract and error
     precedence (unsupported_scope -> invalid_input -> source_range).
  3. Emits tests/fixtures/calc-edge-cases.json consumed by
     tests/calc-edge-cases.test.mjs.

Usage:
  python tools/build_calc_edge_fixtures.py CANDIDATES_JSON OUT_JSON \
      [--rates PATH] [--provenance PATH]

Exit codes: 0 = corpus written (per-candidate skips are reported, not fatal);
1 = bad invocation/inputs; 2 = zero valid candidates (refuse empty corpus).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

ARREARS_RE = re.compile(r"^(\d+)(?:[.,](\d{1,2}))?$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_KINDS = {"positive", "negative"}
DEFAULT_PAYER = "individual_or_ip"


def js_round(value):
    """JS Math.round: half toward +infinity on the float64 value."""
    return math.floor(value + 0.5)


# --- deterministic oracle: mirrors js/calc.js validation contract ----------

def _days_in_month(year, month):
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def days_from_civil(y, m, d):
    """Serial day number (Hinnant's algorithm); exact integers, year 0 safe."""
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_iso_or_none(value):
    """Strict YYYY-MM-DD + real calendar date -> serial day; mirrors calc.js
    parseISO (`new Date(v)` round-trip accepts year 0000, datetime.date does
    not, so bounds are checked manually and arithmetic stays integer)."""
    if not isinstance(value, str) or not ISO_RE.match(value):
        return None
    y, m, d = (int(part) for part in value.split("-"))
    if not (1 <= m <= 12 and 1 <= d <= _days_in_month(y, m)):
        return None
    return days_from_civil(y, m, d)


def arrears_to_kopecks(value):
    """Mirror calc.js arrears parsing. Returns (kop_int, error_code_or_none)."""
    if isinstance(value, bool):  # JSON true/false is not a number contractually
        return None, "invalid_input"
    if isinstance(value, str):
        match = ARREARS_RE.match(value.strip())
        if not match:
            return None, "invalid_input"
        rub = int(match.group(1))
        frac = (match.group(2) or "").ljust(2, "0")
        kop = rub * 100 + (int(frac) if frac else 0)
    elif isinstance(value, (int, float)) and math.isfinite(value):
        # JS: BigInt(Math.round(x * 100)) — float64 product, half toward +inf.
        kop = math.floor(value * 100 + 0.5)
    else:
        return None, "invalid_input"
    if kop <= 0:
        return None, "invalid_input"
    return kop, None


def oracle_outcome(candidate, rates_sorted):
    """Compute the expected outcome for one candidate.

    rates_sorted: list of {d: 'YYYY-MM-DD', p: '14.00'} sorted by d.
    Returns dict with either {"error_code": ...} or the positive expectation.
    Error precedence mirrors calc.js top-to-bottom order.
    """
    payer = candidate.get("payer_type", DEFAULT_PAYER)
    if payer != DEFAULT_PAYER:
        return {"error_code": "unsupported_scope",
                "note": "MVP-1 scope is individuals/IP under Art. 75(4)(1) only"}

    if not rates_sorted:
        return {"error_code": "source_integrity", "note": "empty rate schedule"}

    kop, err = arrears_to_kopecks(candidate.get("arrears_rub"))
    if err:
        return {"error_code": err, "note": "arrears violates the decimal-amount contract"}

    start_raw, end_raw = candidate.get("first_delay_date"), candidate.get("last_delay_date")
    start = parse_iso_or_none(start_raw)
    if start is None:
        return {"error_code": "invalid_input", "note": "first_delay_date is not a strict ISO calendar date"}
    end = parse_iso_or_none(end_raw)
    if end is None:
        return {"error_code": "invalid_input", "note": "last_delay_date is not a strict ISO calendar date"}
    if start > end:
        return {"error_code": "invalid_input", "note": "first_delay_date after last_delay_date"}

    rate_points = [(parse_iso_or_none(row["d"]), row["p"]) for row in rates_sorted]
    first_day, last_day = rate_points[0][0], rate_points[-1][0]
    if start < first_day or end > last_day:
        return {"error_code": "source_range",
                "note": "outside snapshot %s..%s (fail-closed, no extrapolation)" % (rates_sorted[0]["d"], rates_sorted[-1]["d"])}

    # Positive path: exact Fraction arithmetic, day uses last rate with d <= day.
    total = Fraction(0, 1)  # exact sum in kopecks
    rate_days = {}
    idx = 0
    for day in range(start, end + 1):
        while idx + 1 < len(rate_points) and rate_points[idx + 1][0] <= day:
            idx += 1
        if rate_points[idx][0] > day:
            return {"error_code": "source_range", "note": "gap in the CBR schedule inside the period"}
        rate100 = js_round(float(rate_points[idx][1]) * 100)  # 14.00 -> 1400
        total += Fraction(kop * rate100, 3_000_000)
        rate_days[rate_points[idx][1]] = rate_days.get(rate_points[idx][1], 0) + 1

    days_inclusive = end - start + 1
    # ROUND_HALF_UP to kopecks (floor(x + 1/2) on the exact value, positives only).
    display_kop = math.floor(total + Fraction(1, 2))
    exact_kop = total.numerator // total.denominator  # trunc toward zero (positives)
    return {
        "expected_display_total_rub": "%d.%02d" % divmod(display_kop, 100),
        "expected_exact_total_rub": "%d.%02d" % divmod(exact_kop, 100),
        "expected_days_inclusive": days_inclusive,
        "expected_rate_days": rate_days,
    }


# --- candidate intake: schema validation + digest dedupe -------------------

def canonical_input_digest(candidate):
    payload = {
        "kind": candidate["kind"],
        "arrears_rub": candidate.get("arrears_rub"),
        "first_delay_date": candidate.get("first_delay_date"),
        "last_delay_date": candidate.get("last_delay_date"),
        "payer_type": candidate.get("payer_type", DEFAULT_PAYER),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_candidate(raw, index):
    """Returns (candidate, None) or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        return None, "kind must be positive|negative"
    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        return None, "category missing"
    arrears_present = "arrears_rub" in raw
    if not arrears_present:
        return None, "arrears_rub missing (type-fuzz needs an explicit null)"
    for field in ("first_delay_date", "last_delay_date"):
        if field in raw and raw[field] is not None and not isinstance(raw[field], str):
            return None, "%s must be a string" % field
    payer = raw.get("payer_type")
    if payer is not None and not isinstance(payer, str):
        return None, "payer_type must be a string"
    return {
        "kind": kind,
        "category": category.strip(),
        "arrears_rub": raw.get("arrears_rub"),
        "first_delay_date": raw.get("first_delay_date"),
        "last_delay_date": raw.get("last_delay_date"),
        **({"payer_type": payer} if payer is not None else {}),
        "rationale": raw.get("rationale") if isinstance(raw.get("rationale"), str) else "",
    }, None


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidates", help="model-generated candidate inputs (JSON array)")
    parser.add_argument("out", help="output corpus JSON path")
    parser.add_argument("--rates", default=str(Path(__file__).resolve().parent.parent / "data" / "key-rates.json"))
    parser.add_argument("--provenance", default=None, help="JSON with generator rail/session evidence")
    args = parser.parse_args()

    candidates_path = Path(args.candidates)
    if not candidates_path.is_file():
        print("candidates file not found: %s" % candidates_path, file=sys.stderr)
        return 1
    raw_candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    if not isinstance(raw_candidates, list):
        print("candidates root must be a JSON array", file=sys.stderr)
        return 1

    rates_path = Path(args.rates)
    rates_doc = json.loads(rates_path.read_text(encoding="utf-8"))
    rates_sorted = sorted(rates_doc["rows"], key=lambda row: row["d"])

    fixtures, seen_digests = [], set()
    stats = {"received": len(raw_candidates), "schema_skipped": 0, "duplicate_skipped": 0}
    skip_log = []
    for index, raw in enumerate(raw_candidates):
        candidate, reason = validate_candidate(raw, index)
        if candidate is None:
            stats["schema_skipped"] += 1
            skip_log.append({"index": index, "reason": reason})
            continue
        digest = canonical_input_digest(candidate)
        if digest in seen_digests:
            stats["duplicate_skipped"] += 1
            skip_log.append({"index": index, "reason": "duplicate input digest", "id_hint": candidate["category"]})
            continue
        seen_digests.add(digest)

        outcome = oracle_outcome(candidate, rates_sorted)
        if "error_code" in outcome:
            if candidate["kind"] != "negative":
                stats["schema_skipped"] += 1
                skip_log.append({"index": index, "reason": "declared positive but oracle rejects: %s" % outcome["error_code"]})
                continue
            expected = {"expected_error_code": outcome["error_code"]}
        else:
            if candidate["kind"] != "positive":
                stats["schema_skipped"] += 1
                skip_log.append({"index": index, "reason": "declared negative but oracle computes a result"})
                continue
            expected = outcome

        fixtures.append({
            "id": "%s-%02d" % (re.sub(r"[^a-z0-9]+", "_", candidate["category"].lower()).strip("_"), len(fixtures) + 1),
            "kind": candidate["kind"],
            "category": candidate["category"],
            "arrears_rub": candidate["arrears_rub"],
            "first_delay_date": candidate["first_delay_date"],
            "last_delay_date": candidate["last_delay_date"],
            **({"payer_type": candidate["payer_type"]} if "payer_type" in candidate else {}),
            **expected,
            "oracle_note": (outcome.get("note") or candidate["rationale"] or ""),
            "input_digest": digest,
        })

    stats["written"] = len(fixtures)
    if not fixtures:
        print("refusing to write an empty corpus", file=sys.stderr)
        print(json.dumps({"stats": stats, "skips": skip_log}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    provenance = json.loads(Path(args.provenance).read_text(encoding="utf-8")) if args.provenance else {}
    corpus = {
        "corpus_version": "calc-edge-2026-10-09-v1",
        "scope": "synthetic edge cases for the Art. 75(4)(1) NK RF peni calculator (js/calc.js)",
        "generated_by": {
            "method": "model proposes candidate INPUTS only; all expected values are computed by the deterministic oracle script",
            **provenance,
        },
        "oracle": {
            "script": "tools/build_calc_edge_fixtures.py",
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "arithmetic": "exact Fraction arithmetic, independent reimplementation; HALF_UP kopeck rounding; validation contract mirrored from js/calc.js",
        },
        "rates_snapshot": {
            "path": "data/key-rates.json",
            "rows": len(rates_sorted),
            "first_date": rates_sorted[0]["d"],
            "last_date": rates_sorted[-1]["d"],
            "sha256": sha256_file(rates_path),
        },
        "stats": stats,
        "fixtures": fixtures,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(json.dumps({"stats": stats, "skips": skip_log}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
