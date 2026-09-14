#!/usr/bin/env node
// Readback-харнес измерения successful_organic_calculations_28d.
// Запуск: node tools/readback_measure.mjs [--synthetic N] [--business N] [--days-ago D]
//
// Что делает (acceptance из decision:company=go):
//  1. создаёт изолированное in-memory хранилище (ничего не трогает на диске и в браузере);
//  2. пишет synthetic-события (syn=true) и, опционально, business-события;
//  3. выполняет readback и печатает JSON;
//  4. ASSERT: synthetic полностью отделён от business numerator
//     (successful_organic_calculations_28d не включает syn-события),
//     события старше 28 дней не считаются.
// Exit 0 = readback-контракт выполнен; exit 1 = нарушение разделения.
'use strict';

import { createMeasure, safeStorage, MEASURE_VERSION } from '../js/measure.js';

const args = process.argv.slice(2);
function argVal(name, def) {
  const i = args.indexOf('--' + name);
  return i >= 0 && args[i + 1] !== undefined ? Number(args[i + 1]) : def;
}
const SYN = argVal('synthetic', 1);
const BIZ = argVal('business', 0);
const DAYS_AGO = argVal('days-ago', 0); // сдвиг synthetic-событий в прошлое (тест окна)

const DAY = 24 * 60 * 60 * 1000;
const T0 = Date.now();
let clock = T0 - DAYS_AGO * DAY;
const storage = safeStorage({});
const m = createMeasure({ storage, now: () => clock });

// synthetic readback-события (как при post-deploy проверке)
for (let i = 0; i < SYN; i++) {
  m.record({ calculator: 'tax_penalty', attribution: 'organic', synthetic: true });
}
// business-события (в «сейчас»), если запрошены
clock = T0;
const mNow = createMeasure({ storage, now: () => clock });
for (let i = 0; i < BIZ; i++) {
  mNow.record({ calculator: 'tax_penalty', attribution: 'organic' });
}

const rb = mNow.readback();
const report = {
  harness: 'readback_measure',
  measure_version: MEASURE_VERSION,
  input: { synthetic: SYN, business: BIZ, synthetic_days_ago: DAYS_AGO },
  readback: rb,
  assertions: {},
};

let ok = true;
function check(name, cond) {
  report.assertions[name] = cond ? 'PASS' : 'FAIL';
  if (!cond) ok = false;
}

// Synthetic отделён от business numerator:
const syntheticExpectedInWindow = DAYS_AGO <= 28 ? SYN : 0;
check('synthetic_not_in_business_numerator', rb.successful_organic_calculations_28d === BIZ);
check('synthetic_counted_separately', rb.synthetic_28d === syntheticExpectedInWindow);
check('business_total_excludes_synthetic', rb.business_all_attributions_28d === BIZ);
check('stored_total_consistent', rb.stored_events_total === SYN + BIZ);
// Export round-trip: JSONL содержит ровно те же события, allowlist-поля
const lines = mNow.exportJSONL().split('\n').filter(Boolean);
check('jsonl_roundtrip', lines.length === Math.min(SYN + BIZ, 500));

console.log(JSON.stringify(report, null, 2));
process.exit(ok ? 0 : 1);
