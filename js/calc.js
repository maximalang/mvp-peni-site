// Калькулятор пеней по ст. 75 НК РФ (ФЛ/ИП), 1/300 ключевой ставки ЦБ за каждый день.
// Порт penalty_model.py (readiness-пакет MVP-1, fixtures 10/10).
// Точная арифметика на BigInt: копейки входа, ставка в сотых долях %,
// аккумулятор в масштабе 1e-6 копейки. Округление только на выводе, ROUND_HALF_UP.
'use strict';

const SCALE = 1000000n; // 1e-6 копейки на единицу аккумулятора

export class CalcError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

/** ISO-строка -> Date (UTC-полночь); бросает invalid_input на мусоре. */
export function parseISO(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new CalcError('invalid_input', 'invalid ISO date: ' + JSON.stringify(value));
  }
  const d = new Date(value + 'T00:00:00Z');
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== value) {
    throw new CalcError('invalid_input', 'invalid ISO date: ' + value);
  }
  return d;
}

function dayDiff(a, b) { // b - a в днях (оба UTC)
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

/**
 * rates: [{d:'YYYY-MM-DD', p:'14.00'}, ...] — как в data/key-rates.json.
 * Возвращает результат или бросает CalcError (fail-closed вне диапазона снапшота).
 */
export function calculateTaxPenalty(rates, arrearsInput, firstDelay, lastDelay, payerType = 'individual_or_ip') {
  if (payerType !== 'individual_or_ip') {
    throw new CalcError(
      'unsupported_scope',
      'MVP-1 supports only individuals and individual entrepreneurs under Article 75(4)(1)'
    );
  }
  if (!Array.isArray(rates) || rates.length === 0) {
    throw new CalcError('source_integrity', 'key-rate schedule is empty');
  }
  // arrears: строка "1234.56" | число; конвертация в копейки без float
  let kop;
  if (typeof arrearsInput === 'string') {
    const m = /^(\d+)(?:[.,](\d{1,2}))?$/.exec(arrearsInput.trim());
    if (!m) throw new CalcError('invalid_input', 'arrears must be a decimal amount');
    const rub = BigInt(m[1]);
    const frac = (m[2] || '').padEnd(2, '0');
    kop = rub * 100n + BigInt(frac || '0');
  } else if (typeof arrearsInput === 'number' && Number.isFinite(arrearsInput)) {
    kop = BigInt(Math.round(arrearsInput * 100));
  } else {
    throw new CalcError('invalid_input', 'arrears must be a decimal amount');
  }
  if (kop <= 0n) throw new CalcError('invalid_input', 'arrears must be greater than zero');

  const start = parseISO(firstDelay);
  const end = parseISO(lastDelay);
  if (start > end) {
    throw new CalcError('invalid_input', 'first_delay_date must not be after last_delay_date');
  }

  const sorted = [...rates].sort((a, b) => (a.d < b.d ? -1 : a.d > b.d ? 1 : 0));
  // Инвариант дубликатов: источник отклоняет повторы при парсинге, но функция
  // принимает любой массив строк, поэтому проверяем и здесь (fail closed, не гадать).
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].d === sorted[i - 1].d) {
      throw new CalcError('source_integrity', 'duplicate effective dates in key-rate schedule');
    }
  }
  const firstDay = parseISO(sorted[0].d);
  const lastDay = parseISO(sorted[sorted.length - 1].d);
  if (start < firstDay || end > lastDay) {
    throw new CalcError(
      'source_range',
      'requested dates must be within ' + sorted[0].d + '..' + sorted[sorted.length - 1].d +
        '; refresh the key-rate snapshot instead of extrapolation'
    );
  }

  // День d использует последнюю ставку с effective_date <= d.
  let idx = 0;
  let num = 0n; // числитель: Σ kop * rate100 * SCALE
  const days = dayDiff(start, end);
  const rateDays = new Map();
  let cursor = new Date(start);
  for (let i = 0; i <= days; i++) {
    const iso = cursor.toISOString().slice(0, 10);
    while (idx + 1 < sorted.length && sorted[idx + 1].d <= iso) idx++;
    const r = sorted[idx];
    if (!r || r.d > iso) throw new CalcError('source_range', 'no CBR rate for ' + iso);
    const rate100 = BigInt(Math.round(parseFloat(r.p) * 100)); // 14.00% -> 1400
    // penalty_day_kop = kop * (rate100/10000) / 300 = kop * rate100 / 3_000_000
    num += kop * rate100 * SCALE;
    rateDays.set(r.p, (rateDays.get(r.p) || 0) + 1);
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }

  // total_kop = num / (3_000_000 * SCALE), округление ROUND_HALF_UP для положительных
  const denom = 3000000n * SCALE;
  const totalKopRounded = (num * 2n + denom) / (2n * denom); // floor((num/denom)+0.5)
  const exactKop = num / denom; // усечённое точное значение для справки
  const displayRub = totalKopRounded / 100n;
  const displayKopRem = totalKopRounded % 100n;

  return {
    scope: 'tax_arrears_individual_or_ip',
    days_inclusive: days + 1,
    arrears_rub: (kop / 100n).toString() + ',' + (kop % 100n).toString().padStart(2, '0'),
    first_delay_date: firstDelay,
    last_delay_date: lastDelay,
    rate_days: Object.fromEntries([...rateDays.entries()]),
    exact_total_rub: formatRub(exactKop),
    display_total_rub: formatRub(totalKopRounded),
    rounding: 'ROUND_HALF_UP to kopecks for display',
  };
}

function formatRub(kop) {
  const sign = kop < 0n ? '-' : '';
  const abs = kop < 0n ? -kop : kop;
  return sign + (abs / 100n).toString() + ',' + (abs % 100n).toString().padStart(2, '0');
}
