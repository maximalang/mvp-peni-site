// First-party consent-safe measurement для метрики successful_organic_calculations_28d.
// Решение владельца (decision:company=go, 2026-09-13): event только на успешный расчёт;
// поля: timestamp, calculator, organic attribution, success, anonymous dedupe id, synthetic flag.
// Жёсткие границы:
//  - allowlist полей: всё, что не в EVENT_FIELDS, отбрасывается (никаких сумм, дат, PII);
//  - referrer классифицируется в категорию, сам URL никогда не сохраняется;
//  - first-party: только localStorage этого сайта + опциональный fire-and-forget beacon
//    на СВОЙ endpoint /api/event (server-side агрегация, t_543102d7). Никаких cookies,
//    третьих сторон и cross-origin запросов;
//  - synthetic-события считаются отдельно и НЕ входят в business-числитель;
//  - отказ measurement не должен ломать калькулятор (вызывающий код оборачивает в try/catch);
//  - отказ beacon-отправки тоже не ломает ничего: localStorage-путь всегда сохраняется.
// Откат: модуль изолирован, интеграция в app.js — одна точка; git revert полностью удаляет
// измерение. Серверный beacon отключается отдельно (transport не передаётся в createMeasure).
'use strict';

export const MEASURE_VERSION = 1;
export const STORAGE_KEY = 'peni.calc_events.v1';
export const DID_KEY = 'peni.device_id.v1';
export const MAX_EVENTS = 500; // ring-buffer cap: защита от бесконечного роста (LLM10-аналог)
export const WINDOW_MS = 28 * 24 * 60 * 60 * 1000;
export const DEFAULT_ENDPOINT = '/api/event'; // same-origin first-party counter

// Разрешённые поля события. Всё остальное вырезается при записи и при чтении.
export const EVENT_FIELDS = ['ts', 'v', 'calc', 'attr', 'ok', 'did', 'syn'];

export const CALCULATORS = ['tax_penalty'];
export const ATTRIBUTIONS = ['organic', 'referral', 'direct', 'unknown'];

// Поисковые системы для organic-классификации (только hostname, URL не хранится).
const SEARCH_HOSTS = [
  'yandex.ru', 'yandex.com', 'yandex.by', 'yandex.kz',
  'google.ru', 'google.com', 'google.com.ua', 'google.by', 'google.kz',
  'bing.com', 'duckduckgo.com', 'yahoo.com', 'rambler.ru',
  'mail.ru', 'go.mail.ru', 'seznam.cz', 'baidu.com', 'qwant.com', 'ecosia.org',
];

/** referrer-строка -> категория. Пустой referrer = direct. Битый URL = unknown. */
export function classifyReferrer(referrer) {
  if (typeof referrer !== 'string' || referrer.trim() === '') return 'direct';
  let host;
  try {
    host = new URL(referrer).hostname.toLowerCase();
  } catch {
    return 'unknown';
  }
  if (host === '') return 'unknown';
  const isSearch = SEARCH_HOSTS.some((s) => host === s || host.endsWith('.' + s));
  return isSearch ? 'organic' : 'referral';
}

/** Прокси над localStorage: в приватном режиме/при блокировке возвращает null-safe заглушку. */
export function safeStorage(globalObj) {
  try {
    const ls = globalObj.localStorage;
    ls.getItem('__probe__');
    return ls;
  } catch {
    const mem = new Map();
    return {
      getItem: (k) => (mem.has(k) ? mem.get(k) : null),
      setItem: (k, v) => { mem.set(k, String(v)); },
      removeItem: (k) => { mem.delete(k); },
    };
  }
}

function sanitizeEvent(raw) {
  const out = {};
  for (const f of EVENT_FIELDS) {
    if (raw[f] !== undefined) out[f] = raw[f];
  }
  return out;
}

function newId(randomId) {
  if (typeof randomId === 'function') return randomId();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

/**
 * beaconTransport(globalObj, endpoint) — fire-and-forget отправка события на
 * СВОЙ first-party endpoint (server-side агрегация). Privacy-границы:
 *  - mode same-origin + credentials omit: ни cookies, ни cross-origin запросов;
 *  - text/plain payload: без CORS-preflight, без лишних заголовков;
 *  - тело — ровно то же allowlist-событие, что и в localStorage (sanitizeEvent);
 *  - любая ошибка глотается: отказ отправки не ломает ни измерение, ни UX.
 * Возвращает функцию transport(event); передаётся в createMeasure({ transport }).
 */
export function beaconTransport(globalObj, endpoint = DEFAULT_ENDPOINT) {
  return function transport(event) {
    try {
      const body = JSON.stringify(event);
      const nav = globalObj && globalObj.navigator;
      if (nav && typeof nav.sendBeacon === 'function' && globalObj.Blob) {
        nav.sendBeacon(endpoint, new globalObj.Blob([body], { type: 'text/plain;charset=UTF-8' }));
        return;
      }
      if (globalObj && typeof globalObj.fetch === 'function') {
        const p = globalObj.fetch(endpoint, {
          method: 'POST',
          body,
          headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
          keepalive: true,
          mode: 'same-origin',
          credentials: 'omit',
        });
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    } catch { /* silent degradation */ }
  };
}

/**
 * createMeasure({ storage, now, randomId, transport }) — фабрика без зависимостей от DOM (тестируемо в node).
 * now: () -> epoch ms (инжектируемые часы для детерминированных тестов окна 28d).
 * transport: (event) -> void — опциональная fire-and-forget отправка на server-side
 * counter (/api/event); localStorage-путь сохраняется всегда и от транспорта не зависит.
 */
export function createMeasure({ storage, now = () => Date.now(), randomId, transport } = {}) {
  if (!storage) throw new Error('measure: storage is required');

  function readLog() {
    let raw;
    try {
      raw = storage.getItem(STORAGE_KEY);
    } catch {
      return [];
    }
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map(sanitizeEvent);
    } catch {
      return []; // повреждённый лог = пустой (fail-safe, не ломаем страницу)
    }
  }

  function writeLog(events) {
    const trimmed = events.slice(-MAX_EVENTS);
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    } catch {
      // переполнение/блокировка записи: measurement деградирует молча, UX не страдает
    }
    return trimmed;
  }

  function ensureId() {
    let did = null;
    try {
      did = storage.getItem(DID_KEY);
    } catch { /* noop */ }
    if (!did) {
      did = newId(randomId);
      try {
        storage.setItem(DID_KEY, did);
      } catch { /* noop */ }
    }
    return did;
  }

  /**
   * record({ calculator, attribution, synthetic }) — единственная точка записи.
   * Входные данные пользователя (суммы/даты) не принимаются сигнатурой в принципе.
   * Возвращает записанное событие (только allowlist-поля).
   */
  function record({ calculator, attribution = 'unknown', synthetic = false } = {}) {
    if (!CALCULATORS.includes(calculator)) {
      throw new Error('measure: unknown calculator ' + JSON.stringify(calculator));
    }
    const attr = ATTRIBUTIONS.includes(attribution) ? attribution : 'unknown';
    const event = sanitizeEvent({
      ts: new Date(now()).toISOString(),
      v: MEASURE_VERSION,
      calc: calculator,
      attr,
      ok: true, // event создаётся только на успешный расчёт (decision:company=go)
      did: ensureId(),
      syn: synthetic === true,
    });
    const log = readLog();
    log.push(event);
    writeLog(log);
    // Server-side агрегация (t_543102d7): тот же allowlist-набор полей уходит
    // fire-and-forget на свой /api/event. Отказ/исключение транспорта не
    // отменяет локальную запись и не всплывает наружу.
    if (typeof transport === 'function') {
      try {
        transport(event);
      } catch { /* beacon degradation is silent */ }
    }
    return event;
  }

  /**
   * Readback за последние 28 дней. Primary numerator успешных organic-расчётов
   * отделён от synthetic и от прочих attribution (referral/direct/unknown).
   * synthetic никогда не входит в business/organic числитель.
   */
  function readback() {
    const cutoff = now() - WINDOW_MS;
    const upper = now();
    const organicBusiness = { count: 0, dids: new Set() };
    const byAttr = { organic: 0, referral: 0, direct: 0, unknown: 0 };
    let businessTotal = 0;
    let synthetic = 0;
    for (const e of readLog()) {
      const t = Date.parse(e.ts);
      if (!Number.isFinite(t) || t < cutoff || t > upper) continue;
      if (e.syn === true) {
        synthetic += 1;
        continue; // synthetic отделён: не влияет ни на один business-числитель
      }
      if (e.ok !== true) continue;
      businessTotal += 1;
      const a = byAttr[e.attr] !== undefined ? e.attr : 'unknown';
      byAttr[a] += 1;
      if (a === 'organic') {
        organicBusiness.count += 1;
        if (typeof e.did === 'string' && e.did) organicBusiness.dids.add(e.did);
      }
    }
    return {
      metric: 'successful_organic_calculations_28d',
      version: MEASURE_VERSION,
      window_days: 28,
      // primary numerator: только успешные organic-расчёты, без synthetic
      successful_organic_calculations_28d: organicBusiness.count,
      unique_organic_visitors_28d: organicBusiness.dids.size,
      // диагностика (не primary): business по всем attribution + synthetic отдельно
      business_all_attributions_28d: businessTotal,
      by_attribution_28d: byAttr,
      synthetic_28d: synthetic,
      stored_events_total: readLog().length,
    };
  }

  function exportJSONL() {
    return readLog().map((e) => JSON.stringify(e)).join('\n');
  }

  function clear() {
    try {
      storage.removeItem(STORAGE_KEY);
    } catch { /* noop */ }
  }

  return { record, readback, exportJSONL, clear, ensureId };
}
