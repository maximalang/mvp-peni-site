// Тесты first-party measurement (js/measure.js) для successful_organic_calculations_28d.
// Запуск: node --test tests/measure.test.mjs
// Проверяют: allowlist полей (privacy), anonymous dedupe id, organic attribution,
// окно 28 дней, отделение synthetic от business numerator, cap буфера, readback.
'use strict';

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createMeasure,
  classifyReferrer,
  safeStorage,
  beaconTransport,
  EVENT_FIELDS,
  STORAGE_KEY,
  DID_KEY,
  MAX_EVENTS,
  MEASURE_VERSION,
  DEFAULT_ENDPOINT,
} from '../js/measure.js';

const T0 = Date.parse('2026-09-13T12:00:00Z');
const DAY = 24 * 60 * 60 * 1000;

function makeNow(start = T0) {
  let t = start;
  return { now: () => t, advance: (ms) => { t += ms; }, set: (ms) => { t = ms; } };
}

function freshMeasure(nowFn) {
  const storage = safeStorage({});
  return { m: createMeasure({ storage, now: nowFn }), storage };
}

test('classifyReferrer: пустой referrer = direct', () => {
  assert.equal(classifyReferrer(''), 'direct');
  assert.equal(classifyReferrer(undefined), 'direct');
  assert.equal(classifyReferrer('   '), 'direct');
});

test('classifyReferrer: поисковые hostname = organic (yandex/google и поддомены)', () => {
  assert.equal(classifyReferrer('https://yandex.ru/search/?text=пени'), 'organic');
  assert.equal(classifyReferrer('https://www.google.com/search?q=пени'), 'organic');
  assert.equal(classifyReferrer('https://go.mail.ru/search?q=x'), 'organic');
  assert.equal(classifyReferrer('https://duckduckgo.com/?q=x'), 'organic');
});

test('classifyReferrer: не-поисковый hostname = referral; битый URL = unknown', () => {
  assert.equal(classifyReferrer('https://habr.com/ru/articles/1'), 'referral');
  assert.equal(classifyReferrer('not a url'), 'unknown');
  // домен-lookalike не должен матчиться как поиск
  assert.equal(classifyReferrer('https://notyandex.ru/search'), 'referral');
  assert.equal(classifyReferrer('https://yandex.ru.evil.example/q'), 'referral');
});

test('record: событие содержит ровно allowlist-поля (privacy boundary)', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  const ev = m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  assert.deepEqual(Object.keys(ev).sort(), [...EVENT_FIELDS].sort());
  assert.equal(ev.v, MEASURE_VERSION);
  assert.equal(ev.ok, true);
  assert.equal(ev.syn, false);
  assert.equal(ev.calc, 'tax_penalty');
  assert.equal(ev.attr, 'organic');
  // timestamp — ISO, парсится
  assert.ok(Number.isFinite(Date.parse(ev.ts)));
});

test('record: данные пользователя не попадают в событие даже при попытке прокинуть их', () => {
  const { now } = makeNow();
  const { m, storage } = freshMeasure(now);
  // hostile caller пытается протащить PII/суммы/даты через extra-поля
  const ev = m.record({
    calculator: 'tax_penalty',
    attribution: 'organic',
    arrears_rub: '100000',
    first_delay_date: '2026-01-01',
    ip: '1.2.3.4',
    userAgent: 'x',
    url: 'https://site/?a=100000&d1=2026-01-01',
  });
  assert.deepEqual(Object.keys(ev).sort(), [...EVENT_FIELDS].sort());
  const persisted = JSON.parse(storage.getItem(STORAGE_KEY));
  const blob = JSON.stringify(persisted);
  assert.ok(!blob.includes('100000'));
  assert.ok(!blob.includes('2026-01-01'));
  assert.ok(!blob.includes('1.2.3.4'));
  assert.ok(!blob.includes('userAgent'));
});

test('record: referrer URL не сохраняется — только категория attribution', () => {
  const { now } = makeNow();
  const { m, storage } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  const blob = storage.getItem(STORAGE_KEY);
  assert.ok(!blob.includes('yandex'));
  assert.ok(!blob.includes('google'));
  assert.ok(!blob.includes('http'));
});

test('record: неизвестный calculator отклоняется (fail closed)', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  assert.throws(() => m.record({ calculator: 'mortgage' }), /unknown calculator/);
  assert.throws(() => m.record({}), /unknown calculator/);
});

test('record: неизвестная attribution нормализуется в unknown', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  const ev = m.record({ calculator: 'tax_penalty', attribution: 'paid-ads' });
  assert.equal(ev.attr, 'unknown');
});

test('dedupe id: один и тот же did на всех событиях хранилища, stable через перезапуск', () => {
  const { now } = makeNow();
  const storage = safeStorage({});
  const m1 = createMeasure({ storage, now });
  const a = m1.record({ calculator: 'tax_penalty', attribution: 'organic' });
  const b = m1.record({ calculator: 'tax_penalty', attribution: 'direct' });
  assert.equal(a.did, b.did);
  assert.match(a.did, /^[0-9a-f-]{36}$/);
  // «перезапуск» страницы: новый инстанс поверх того же storage — тот же did
  const m2 = createMeasure({ storage, now });
  const c = m2.record({ calculator: 'tax_penalty', attribution: 'organic' });
  assert.equal(c.did, a.did);
});

test('readback: synthetic отделён от business numerator', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  m.record({ calculator: 'tax_penalty', attribution: 'organic', synthetic: true });
  m.record({ calculator: 'tax_penalty', attribution: 'organic', synthetic: true });
  const rb = m.readback();
  assert.equal(rb.successful_organic_calculations_28d, 1, 'synthetic не входит в organic numerator');
  assert.equal(rb.synthetic_28d, 2);
  assert.equal(rb.business_all_attributions_28d, 1);
});

test('readback: только organic идёт в primary numerator; referral/direct — в диагностику', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  m.record({ calculator: 'tax_penalty', attribution: 'referral' });
  m.record({ calculator: 'tax_penalty', attribution: 'direct' });
  m.record({ calculator: 'tax_penalty', attribution: 'unknown' });
  const rb = m.readback();
  assert.equal(rb.successful_organic_calculations_28d, 2);
  assert.equal(rb.business_all_attributions_28d, 5);
  assert.deepEqual(rb.by_attribution_28d, { organic: 2, referral: 1, direct: 1, unknown: 1 });
});

test('readback: окно ровно 28 дней — события старше cutoff не считаются', () => {
  const clock = makeNow();
  const { m } = freshMeasure(clock.now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' }); // сейчас
  clock.set(T0 - 27 * DAY);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' }); // 27 дней назад — в окне
  clock.set(T0 - 29 * DAY);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' }); // 29 дней назад — вне окна
  clock.set(T0);
  const rb = m.readback();
  assert.equal(rb.successful_organic_calculations_28d, 2);
  assert.equal(rb.stored_events_total, 3);
});

test('readback: пустое хранилище = нули, структура метрики стабильна', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  const rb = m.readback();
  assert.equal(rb.metric, 'successful_organic_calculations_28d');
  assert.equal(rb.successful_organic_calculations_28d, 0);
  assert.equal(rb.synthetic_28d, 0);
  assert.equal(rb.stored_events_total, 0);
});

test('readback: повреждённый лог читается как пустой (fail-safe)', () => {
  const { now } = makeNow();
  const storage = safeStorage({});
  storage.setItem(STORAGE_KEY, '{not json');
  const m = createMeasure({ storage, now });
  const rb = m.readback();
  assert.equal(rb.successful_organic_calculations_28d, 0);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  assert.equal(m.readback().successful_organic_calculations_28d, 1);
});

test('cap: буфер ограничен MAX_EVENTS (старые события вытесняются)', () => {
  const { now } = makeNow();
  const { m, storage } = freshMeasure(now);
  for (let i = 0; i < MAX_EVENTS + 50; i++) {
    m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  }
  const persisted = JSON.parse(storage.getItem(STORAGE_KEY));
  assert.equal(persisted.length, MAX_EVENTS);
  assert.equal(m.readback().successful_organic_calculations_28d, MAX_EVENTS);
});

test('storage: блокированный localStorage (SecurityError) деградирует молча', () => {
  const { now } = makeNow();
  const hostile = {
    getItem() { throw new Error('SecurityError'); },
    setItem() { throw new Error('SecurityError'); },
    removeItem() { throw new Error('SecurityError'); },
  };
  const m = createMeasure({ storage: hostile, now });
  const ev = m.record({ calculator: 'tax_penalty', attribution: 'organic' }); // не бросает
  assert.equal(ev.calc, 'tax_penalty');
  assert.equal(m.readback().successful_organic_calculations_28d, 0); // читать нечего, но без crash
});

test('readback synthetic event: отдельный синтетический прогон с проверкой JSONL-экспорта', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  // synthetic-событие как при readback-проверке деплоя (acceptance из decision:company=go)
  const syn = m.record({ calculator: 'tax_penalty', attribution: 'organic', synthetic: true });
  assert.equal(syn.syn, true);
  const jsonl = m.exportJSONL().split('\n').filter(Boolean);
  assert.equal(jsonl.length, 1);
  const parsed = JSON.parse(jsonl[0]);
  assert.equal(parsed.syn, true);
  assert.equal(parsed.attr, 'organic');
  const rb = m.readback();
  assert.equal(rb.synthetic_28d, 1);
  assert.equal(rb.successful_organic_calculations_28d, 0);
});

test('exportJSONL: каждая строка — валидный JSON только с allowlist-полями', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  m.record({ calculator: 'tax_penalty', attribution: 'direct', synthetic: true });
  for (const line of m.exportJSONL().split('\n').filter(Boolean)) {
    const obj = JSON.parse(line);
    assert.deepEqual(Object.keys(obj).sort(), [...EVENT_FIELDS].sort());
  }
});

test('DID_KEY и STORAGE_KEY различаются; clear() удаляет только лог событий', () => {
  const { now } = makeNow();
  const { m, storage } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  const didBefore = m.ensureId();
  m.clear();
  assert.equal(storage.getItem(STORAGE_KEY), null);
  assert.equal(storage.getItem(DID_KEY), didBefore); // dedupe id переживает очистку лога
  assert.equal(m.readback().successful_organic_calculations_28d, 0);
});

// --- server-side beacon (t_543102d7) ---------------------------------------

function captureTransport() {
  const sent = [];
  return { sent, transport: (event) => sent.push(structuredClone(event)) };
}

test('beacon: record отправляет ровно allowlist-событие в transport', () => {
  const { now } = makeNow();
  const { sent, transport } = captureTransport();
  const m = createMeasure({ storage: safeStorage({}), now, transport });
  const ev = m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0], ev);
  assert.deepEqual(Object.keys(sent[0]).sort(), [...EVENT_FIELDS].sort());
});

test('beacon: PII/суммы не уходят в transport даже при hostile вызове', () => {
  const { now } = makeNow();
  const { sent, transport } = captureTransport();
  const m = createMeasure({ storage: safeStorage({}), now, transport });
  m.record({
    calculator: 'tax_penalty',
    attribution: 'organic',
    arrears_rub: '100000',
    first_delay_date: '2026-01-01',
    ip: '1.2.3.4',
    userAgent: 'x',
  });
  const blob = JSON.stringify(sent);
  assert.ok(!blob.includes('100000'));
  assert.ok(!blob.includes('2026-01-01'));
  assert.ok(!blob.includes('1.2.3.4'));
  assert.ok(!blob.includes('userAgent'));
});

test('beacon: localStorage-путь сохраняется при отказе transport (fire-and-forget)', () => {
  const { now } = makeNow();
  const storage = safeStorage({});
  const m = createMeasure({
    storage,
    now,
    transport: () => { throw new Error('network down'); },
  });
  const ev = m.record({ calculator: 'tax_penalty', attribution: 'organic' }); // не бросает
  assert.equal(ev.calc, 'tax_penalty');
  assert.equal(m.readback().successful_organic_calculations_28d, 1);
  assert.equal(JSON.parse(storage.getItem(STORAGE_KEY)).length, 1);
});

test('beacon: без transport модуль ведёт себя как раньше (обратная совместимость)', () => {
  const { now } = makeNow();
  const { m } = freshMeasure(now);
  m.record({ calculator: 'tax_penalty', attribution: 'organic' });
  assert.equal(m.readback().successful_organic_calculations_28d, 1);
});

test('beaconTransport: sendBeacon вызывается с same-origin endpoint и text/plain', async () => {
  const calls = [];
  class FakeBlob {
    constructor(parts, opts) { this.parts = parts; this.opts = opts; }
  }
  const fakeGlobal = {
    Blob: FakeBlob,
    navigator: {
      sendBeacon: (url, blob) => { calls.push({ url, blob }); return true; },
    },
  };
  const transport = beaconTransport(fakeGlobal);
  const event = { ts: '2026-09-20T12:00:00.000Z', v: 1, calc: 'tax_penalty', attr: 'organic', ok: true, did: 'd', syn: false };
  transport(event);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, DEFAULT_ENDPOINT);
  assert.ok(!calls[0].url.includes('://'), 'endpoint same-origin (без scheme/host)');
  assert.equal(calls[0].blob.opts.type, 'text/plain;charset=UTF-8');
  assert.deepEqual(JSON.parse(calls[0].blob.parts[0]), event);
});

test('beaconTransport: fallback fetch keepalive + credentials omit + mode same-origin', async () => {
  const calls = [];
  const fakeGlobal = {
    fetch: (url, init) => { calls.push({ url, init }); return Promise.resolve({ ok: true }); },
    navigator: {}, // sendBeacon отсутствует — fallback на fetch
  };
  beaconTransport(fakeGlobal)({ ts: 'x', v: 1, calc: 'tax_penalty', attr: 'direct', ok: true, did: 'd', syn: false });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, DEFAULT_ENDPOINT);
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.keepalive, true);
  assert.equal(calls[0].init.mode, 'same-origin');
  assert.equal(calls[0].init.credentials, 'omit');
});

test('beaconTransport: rejected fetch проглатывается (unhandled rejection нет)', async () => {
  const fakeGlobal = {
    fetch: () => Promise.reject(new Error('offline')),
    navigator: {},
  };
  beaconTransport(fakeGlobal)({ ts: 'x', v: 1, calc: 'tax_penalty', attr: 'direct', ok: true, did: 'd', syn: false });
  await new Promise((r) => setTimeout(r, 10)); // даём микрозадачам отработать
  assert.ok(true); // если rejection не обработан, node --test упадёт
});

test('beaconTransport: hostile global (нет fetch/sendBeacon) деградирует молча', () => {
  assert.doesNotThrow(() => beaconTransport({})({ ts: 'x' }));
  assert.doesNotThrow(() => beaconTransport(undefined)({ ts: 'x' }));
  assert.doesNotThrow(() =>
    beaconTransport({ navigator: { sendBeacon: () => { throw new Error('boom'); } } })({ ts: 'x' }));
});
