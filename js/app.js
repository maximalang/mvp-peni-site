// UI-обвязка калькулятора пеней. Только клиент: суммы и даты пользователя никуда не уходят (W1).
// Измерение successful_organic_calculations_28d — first-party, consent-safe: событие
// пишется только на успешный расчёт, поля ограничены allowlist (js/measure.js),
// суммы/даты/идентификаторы пользователя не собираются. Анонимный счётчик события
// дополнительно уходит fire-and-forget на СВОЙ endpoint /api/event (server-side
// агрегация, t_543102d7) — без cookies и сторонних сервисов; отказ отправки ничего не ломает.
'use strict';

import { calculateTaxPenalty } from './calc.js';
import { createMeasure, classifyReferrer, safeStorage, beaconTransport } from './measure.js';

const $ = (sel) => document.querySelector(sel);

// Attribution фиксируется один раз на загрузку страницы (landing), а не на каждое
// событие: referrer относится к органическому приземлению на страницу.
const measure = createMeasure({
  storage: safeStorage(globalThis),
  transport: beaconTransport(globalThis),
});
const PAGE_ATTRIBUTION = classifyReferrer(globalThis.document?.referrer);

const ERROR_TEXT = {
  invalid_input: 'Проверьте поля: сумма больше нуля, дата начала не позже даты окончания.',
  source_range: 'Период вне диапазона данных ключевой ставки. Расчёт заблокирован до обновления данных.',
  unsupported_scope: 'MVP считает пени только для физлиц и ИП (ст. 75 НК РФ). Для организаций расчёт иной.',
  source_integrity: 'Данные ставки повреждены или пусты. Расчёт недоступен.',
};

async function boot() {
  // Данные ставки грузятся с того же origin, из статического файла — это не отправка данных.
  const resp = await fetch('data/key-rates.json');
  if (!resp.ok) throw new Error('rates unavailable');
  const ratesDoc = await resp.json();
  const rates = ratesDoc.rows;

  // Provenance-блок: источник, дата снимка, TTL-статус (7 дней операционно).
  const captured = new Date(ratesDoc.captured_at);
  const ageDays = Math.floor((Date.now() - captured.getTime()) / 86400000);
  $('#prov-source').textContent = ratesDoc.source.replace(/^https?:\/\//, '');
  $('#prov-source').href = ratesDoc.source;
  $('#prov-date').textContent = captured.toLocaleDateString('ru-RU');
  $('#prov-ttl').textContent = ageDays <= 7 ? 'актуально' : 'требует обновления (' + ageDays + ' дн.)';
  $('#prov-ttl').className = ageDays <= 7 ? 'ttl-ok' : 'ttl-stale';

  // Предзаполнение из URL (?a=<сумма>&d1=<ISO>&d2=<ISO> или тот же формат после #).
  const raw = (location.hash || '').replace(/^#/, '') + '&' + location.search.replace(/^[?]/, '');
  const q = new URLSearchParams(raw);
  const hasQuery = !!(q.get('a') && q.get('d1') && q.get('d2'));
  if (q.get('a')) $('#arrears').value = q.get('a');
  if (q.get('d1')) $('#date-from').value = q.get('d1');
  if (q.get('d2')) $('#date-to').value = q.get('d2');

  $('#calc-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const arrears = $('#arrears').value;
    const d1 = $('#date-from').value;
    const d2 = $('#date-to').value;
    const out = $('#result');
    try {
      const res = calculateTaxPenalty(rates, arrears, d1, d2);
      $('#res-total').textContent = res.display_total_rub + ' ₽';
      $('#res-days').textContent = res.days_inclusive;
      const parts = Object.entries(res.rate_days)
        .map(([rate, days]) => `<tr><td>${rate}%</td><td>${days}</td></tr>`).join('');
      $('#res-rates').innerHTML = parts;
      $('#res-period').textContent =
        formatDate(d1) + ' — ' + formatDate(d2);
      out.hidden = false;
      out.className = 'result';
      // Измерение: event только на успешный расчёт; не содержит данных ввода (allowlist).
      // Изолированный try/catch: отказ измерения не должен красить успешный результат ошибкой.
      try {
        measure.record({ calculator: 'tax_penalty', attribution: PAGE_ATTRIBUTION });
      } catch { /* measurement degradation is silent */ }
      navigator.clipboard?.writeText(
        `Пени по ст. 75 НК РФ: ${res.display_total_rub} руб. ` +
        `(задолженность ${res.arrears_rub} руб., период ${d1}..${d2}, ${res.days_inclusive} дн.)`
      ).catch(() => {});
    } catch (e) {
      out.hidden = false;
      out.className = 'result result-error';
      $('#res-total').textContent = '—';
      $('#res-error').textContent = ERROR_TEXT[e.code] || 'Расчёт недоступен.';
      return;
    }
    $('#res-error').textContent = '';
  });

  if (hasQuery) $('#calc-form').requestSubmit();
}

function formatDate(iso) {
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y}`;
}

boot().catch((e) => {
  document.body.classList.add('fatal');
  $('#fatal-msg').hidden = false;
});
