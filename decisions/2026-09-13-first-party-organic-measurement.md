# Decision — first-party consent-safe measurement для successful_organic_calculations_28d

```text
decision:company=go
Objective: реализовать first-party consent-safe event только для успешного расчёта, чтобы метрика successful_organic_calculations_28d стала измеримой после публикации.
Decision date: 2026-09-13
Evidence: parent card t_ddb78947 (post-merge verification PASS на main 7caee4d2); карточка t_d78e12d6; owner-директива в комментарии карты (operator message 2026-09-13).
Expected value: валидация гипотезы органического спроса (CHARTER.md) без сбора PII и без новых платных capability.
Cost: 0 RUB; paid costs = 0 RUB, commitments = 0 RUB; revenue = null, refunds = null, usage = null до metering (финансовый контур 28d из owner-директивы).
Downside cap: новый paid capability запрещён; аналитика/куки только в рамках существующего согласия; никаких third-party скриптов.
Kill criteria: если измерение начнёт собирать PII или искажать privacy-границу сайта — немедленный revert модуля.
Rollback: git revert коммита модуля; интеграция в app.js — единственная точка; серверных данных, миграций и состояния нет (deploy-safe rollback).
Owner: company
Squad: tech (implementation) + qa (независимая приёмка, карта t_1146fd0b)
```

## Дизайн

### Границы данных (privacy)

- Событие пишется **только на успешный расчёт** (не на ошибки и не на загрузку страницы).
- Поля события — строгий allowlist (`EVENT_FIELDS`): `ts` (ISO timestamp), `v` (версия схемы),
  `calc` (имя калькулятора), `attr` (категория attribution), `ok` (success), `did` (anonymous
  dedupe id), `syn` (synthetic flag). Всё, что не в allowlist, вырезается и при записи, и при чтении.
- **Не собираются**: суммы, даты просрочки, IP, user-agent, URL с параметрами, любые PII.
- Referrer классифицируется в категорию (`organic|referral|direct|unknown`) — сам referrer URL
  никогда не сохраняется. Search-hosts матчатся по hostname с границей домена (lookalike
  `notyandex.ru` и `yandex.ru.evil.example` не дают organic).
- Хранилище: только first-party `localStorage` (без cookies, без third-party, без сетевой
  отправки). Согласие на cookies не требуется: данные не покидают браузер пользователя.

### Attribution

- Organic = приземление с поисковой системы (referrer hostname из списка поисковиков).
- Attribution фиксируется один раз на загрузку страницы (landing), referrer события —
  страница приземления.
- Primary numerator `successful_organic_calculations_28d` считает **только** `attr=organic`
  и `ok=true` и `syn=false`. Остальные attribution идут в диагностику
  (`by_attribution_28d`), synthetic — в отдельный счётчик `synthetic_28d`.

### Synthetic vs business

- Synthetic-события (readback-проверки деплоя) пишутся с `syn=true` и **никогда** не попадают
  в business/organic numerator. Разделение проверяется юнит-тестами и readback-харнесом
  `tools/readback_measure.mjs` (assert-контракт, exit code).

### Отказоустойчивость и эксплуатация

- `safeStorage`: приватный режим/блокировка localStorage → in-memory fallback, measurement
  деградирует молча, калькулятор не ломается (интеграция в `app.js` обёрнута в try/catch).
- Ring-buffer cap 500 событий: защита от бесконечного роста хранилища.
- Повреждённый JSON лога читается как пустой (fail-safe), не красит страницу.
- Readback: `node tools/readback_measure.mjs --synthetic N --business M --days-ago D`
  печатает JSON-отчёт с assertions; exit 0 = контракт разделения synthetic/business выполнен.

## Отклонённые варианты

- **Third-party analytics (GA/Yandex.Metrica)**: требует cookie-согласия, ломает privacy-обещание
  сайта («данные не отправляются на сервер»), новый внешний dependency — запрещено owner-директивой.
- **Server-side счётчик**: серверной части нет (статический сайт); добавление backend — выход за
  gate статичности (CHARTER.md: out until separate evidence gate).
- **Session-based dedupe (sessionStorage)**: не даёт 28-дневного окна уникальных визитов;
  выбран persistent first-party id с cap.

## Verification (этот коммит)

- `node --test tests/measure.test.mjs` — 19/19 pass (allowlist/privacy, dedupe id stability,
  organic attribution, окно 28d, synthetic separation, cap, hostile storage).
- `node --test tests/*.test.mjs` — 21/21 pass, baseline calc (11/11) и SEO-сьют не сломаны.
- `node tools/readback_measure.mjs --synthetic 1` — exit 0, все assertions PASS.
