// Синтетический edge-case корпус (Q2 quality-program): кандидаты от модели,
// ожидания вычислены детерминированным оракулом tools/build_calc_edge_fixtures.py
// (независимая реимплементация формулы ст. 75 НК РФ, Fraction-арифметика).
// Запуск: node --test (CI), вручную: node tests/calc-edge-cases.test.mjs
'use strict';

import { calculateTaxPenalty, CalcError } from '../js/calc.js';
import { readFileSync } from 'node:fs';

const rates = JSON.parse(readFileSync(new URL('../data/key-rates.json', import.meta.url), 'utf-8')).rows;
const corpus = JSON.parse(readFileSync(new URL('./fixtures/calc-edge-cases.json', import.meta.url), 'utf-8'));

let pass = 0, fail = 0;
const digests = new Set();
for (const f of corpus.fixtures) {
  if (digests.has(f.input_digest)) {
    console.log('FAIL', f.id, '- duplicate input_digest in corpus');
    fail++;
    continue;
  }
  digests.add(f.input_digest);

  const args = [rates, f.arrears_rub, f.first_delay_date, f.last_delay_date];
  if (f.payer_type) args.push(f.payer_type);
  try {
    const res = calculateTaxPenalty(...args);
    if (f.kind === 'negative') {
      console.log('FAIL', f.id, '- expected error', f.expected_error_code, 'got result', res.display_total_rub);
      fail++;
      continue;
    }
    const expected = f.expected_display_total_rub.replace('.', ',');
    const problems = [];
    if (res.display_total_rub !== expected) problems.push(`display ${res.display_total_rub} != ${expected}`);
    if (res.exact_total_rub !== f.expected_exact_total_rub.replace('.', ',')) problems.push('exact mismatch');
    if (res.days_inclusive !== f.expected_days_inclusive) problems.push('days_inclusive mismatch');
    if (JSON.stringify(res.rate_days) !== JSON.stringify(f.expected_rate_days)) problems.push('rate_days mismatch');
    if (problems.length === 0) {
      pass++;
      console.log('PASS', f.id, '=', res.display_total_rub);
    } else {
      fail++;
      console.log('FAIL', f.id, '-', problems.join('; '));
    }
  } catch (e) {
    if (f.kind === 'negative' && e instanceof CalcError && e.code === f.expected_error_code) {
      pass++;
      console.log('PASS', f.id, '(error as expected:', e.code + ')');
    } else if (f.kind === 'positive') {
      console.log('FAIL', f.id, '- unexpected error:', e.code || e.message);
      fail++;
    } else {
      console.log('FAIL', f.id, '- wrong error code:', e.code, 'expected', f.expected_error_code);
      fail++;
    }
  }
}
console.log('\ncorpus ' + corpus.corpus_version + ': ' + pass + '/' + (pass + fail) + ' passed');
process.exit(fail ? 1 : 0);
