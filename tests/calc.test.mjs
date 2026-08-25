// Тесты: порт formula_fixtures.json (10 кейсов) на данные data/key-rates.json.
// Запуск: node tests/calc.test.mjs  (exit 0 = все зелёные)
'use strict';

import { calculateTaxPenalty, CalcError } from '../js/calc.js';
import { readFileSync } from 'node:fs';

const rates = JSON.parse(readFileSync(new URL('../data/key-rates.json', import.meta.url), 'utf-8')).rows;
const fixtures = JSON.parse(readFileSync(
  new URL('../../mvp-1-readiness-2026-08-25/formula_fixtures.json', import.meta.url), 'utf-8')).fixtures;

let pass = 0, fail = 0;
for (const f of fixtures) {
  const args = [rates, f.arrears_rub, f.first_delay_date, f.last_delay_date];
  if (f.payer_type) args.push(f.payer_type);
  try {
    const res = calculateTaxPenalty(...args);
    if (f.kind === 'negative') {
      console.log('FAIL', f.id, '- expected error', f.expected_error_code, 'got', res.display_total_rub);
      fail++;
      continue;
    }
    const expected = (f.expected_display_total_rub || '').replace('.', ',');
    if (res.display_total_rub === expected) {
      pass++;
      console.log('PASS', f.id, '=', res.display_total_rub);
    } else {
      console.log('FAIL', f.id, '- expected', expected, 'got', res.display_total_rub);
      fail++;
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
console.log('\n' + pass + '/' + (pass + fail) + ' passed');
process.exit(fail ? 1 : 0);
