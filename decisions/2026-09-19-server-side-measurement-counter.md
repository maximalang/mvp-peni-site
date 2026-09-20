# Decision — server-side first-party counter для successful_organic_calculations_28d

```text
decision:company=go
Objective: сделать метрику successful_organic_calculations_28d агрегируемой флотом (server-side), сохранив приват-копию сайта «без сторонних сервисов».
Decision date: 2026-09-19
Evidence: spec Documents/seo-refresh-20260919/SPEC-server-side-measurement.md; owner-одобрение t_e104dd21 (17.09.2026): "server-side Yandex Metrica and/or first-party events"; карта t_543102d7.
Expected value: baseline-карта t_0af547dc и daily-цикл получают реальный агрегат вместо нулей localStorage-only измерения.
Cost: 0 RUB incremental (существующий Timeweb VDS peni.recruiter-radar.ru, python3 stdlib, без новых зависимостей).
Downside cap: no PII, data minimization, consent-safe, synthetic events separated, rollback required (ограничения владельца t_e104dd21).
Kill criteria: если endpoint начнёт принимать/хранить что-либо вне allowlist или приват-тексты разойдутся с фактическим поведением — немедленный rollback (см. server/README.md).
Rollback: изолирован — один systemd unit + аддитивный Caddy-блок + одна строка transport в js/app.js; revert ветки удаляет клиентскую часть; инструкция в server/README.md.
Owner: company
Squad: tech (implementation, карта t_543102d7) + независимый qa/ops до публикации (publication-гейты отдельной ops-картой).
```

## Выбор варианта

Вариант A (выбран): **first-party counter endpoint** на существующем VDS —
python3 stdlib `http.server` + `sqlite3`, reverse-proxy через существующий Caddy.

Вариант B (отклонён): Yandex Metrica server-side events. Причины: third-party
сервис ломает приват-копию сайта «без сторонних сервисов» (CHARTER), добавляет
юридическую поверхность (cookie-баннер третьей стороны) и зависимость от
внешнего квота. Metrica остаётся fallback только через новое решение company.

Вариант C (отклонён): оставить localStorage-only. Причина: флот не может
агрегировать метрику, baseline 7-day цикл (t_0af547dc) записал бы нули.

## Дизайн-границы

1. **Allowlist fail closed**: сервер принимает ровно поля `ts/v/calc/attr/ok/did/syn`
   (тот же набор, что `EVENT_FIELDS` в js/measure.js). Любое лишнее поле —
   400 `unknown_field`, событие отклоняется ЦЕЛИКОМ (частичная запись запрещена).
2. **Data minimization на схеме**: событийной таблицы нет; только per-day
   счётчики (`event_counters`) и visitor-days (`did_days`). IP/User-Agent
   отсутствуют как колонки и не пишутся в логи.
3. **did — только соль-хэш**: sha256(salt|did), соль в meta-таблице; clear-did
   не появляется в БД (байтовый тест).
4. **Idempotency**: повторный beacon (тот же did+ts+calc) не дублирует счётчик
   (`seen_events` dedupe-key, INSERT OR IGNORE).
5. **Synthetic separation**: `syn=true` — отдельные счётчики, вне всех
   business/organic числителей; readback-харнес и post-deploy smoke это проверяют.
6. **Retention**: lazy-чистка dedupe/visitor строк старше 97 дней (28d-окно +
   69d запас); body ≤ 4096 байт; ts вне retention/clock-skew — отказ.
7. **Клиент fire-and-forget**: `navigator.sendBeacon` → fallback
   `fetch keepalive, same-origin, credentials omit`; отказ отправки не ломает
   ни калькулятор, ни localStorage-путь (transport изолирован try/catch).
8. **No CORS**: same-origin через Caddy; без ACAO браузеры блокируют
   cross-origin reads; OPTIONS → 405.

## Контракт GET /api/metric

`successful_organic_calculations_28d`, `unique_organic_visitors_28d`,
`business_all_attributions_28d`, `by_attribution_28d`, `synthetic_28d`,
`days[]` (business/synthetic/organic по дням, UTC), `window_days`, `metric`,
`version`. Читатели: baseline t_0af547dc, daily-цикл, ops-smoke.

## Publication-гейты

Публикация на прод — отдельной ops-картой после независимого QA:
gate:review + gate:qa + gate:backup + gate:rollback + gate:scope
(acceptance спеки t_543102d7). Эта карта поставляет только PR в
maximalang/mvp-peni-site с зелёным CI.
