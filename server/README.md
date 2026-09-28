# server/ — first-party measurement endpoint (t_543102d7)

Минимальный серверный counter для метрики `successful_organic_calculations_28d`.
Только python3 stdlib (`http.server` + `sqlite3`) — никаких зависимостей и сборок,
по условию целевого хоста (1 GB RAM VDS).

## Что это

- `POST /api/event` — приём ОДНОГО allowlist-события из `js/measure.js`
  (поля `ts/v/calc/attr/ok/did/syn`). Любое лишнее поле (PII, суммы, даты) —
  отказ 400 `unknown_field`, событие не сохраняется целиком (fail closed).
  Повторный beacon (`did+ts+calc`) идемпотентен — счётчик не дублируется.
- `GET /api/metric[?days=N]` — агрегаты: `successful_organic_calculations_28d`
  (syn исключён), `unique_organic_visitors_28d`, `by_attribution_28d`,
  `synthetic_28d`, per-day buckets. Контракт читает baseline-карта t_0af547dc.
- `GET /api/healthz` — liveness.

## Privacy-границы (на уровне схемы)

- событийной таблицы нет вообще: только per-day счётчики и `did_hash`;
- `did` хранится ТОЛЬКО как sha256(salt|did); clear-значение не записывается
  ни в одну таблицу (проверяется тестом `test_did_never_stored_in_clear`);
- IP и User-Agent не логируются и не хранятся (нет колонок + логи без адреса);
- CORS-заголовков нет: same-origin за reverse proxy, cross-origin reads
  блокируются браузером; `OPTIONS` → 405;
- retention: dedupe-ключи и visitor-days выметаются lazy-чисткой
  (RETENTION_DAYS = 97); body запроса ограничен 4096 байт (413);
- synthetic (`syn=true`) считается отдельно и не входит в business-числители.

## Локальный запуск / smoke

```bash
cd server
python3 serve.py --bind 127.0.0.1 --port 8101 --db /tmp/measure.sqlite3
# в другом терминале:
curl -s http://127.0.0.1:8101/api/healthz
curl -s -X POST http://127.0.0.1:8101/api/event \
  -H 'Content-Type: text/plain;charset=UTF-8' \
  -d "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"v\":1,\"calc\":\"tax_penalty\",\"attr\":\"organic\",\"ok\":true,\"did\":\"0f1e2d3c-4b5a-6789-8abc-def012345678\",\"syn\":true}"
curl -s 'http://127.0.0.1:8101/api/metric?days=1'
```

## Тесты

```bash
# из корня репо (то, что гоняет CI):
python3 -m py_compile server/measure_core.py server/serve.py
python3 -m unittest discover -s server -p 'test_*.py'
```

- `test_measure_core.py` (20 тестов) — allowlist/PII-инжект, dedupe, synthetic,
  окно 28d, retention, отсутствие IP/UA-колонок, did только хэшем.
- `test_server_http.py` (10 тестов) — реальный HTTP round-trip: 200/400/405/
  413/404, days-валидация, no-CORS, no-store, IP не в логах.

## Прод (VDS peni.recruiter-radar.ru) — ops-карта

1. Скопировать `server/measure_core.py`, `server/serve.py` в
   `/opt/peni-measure/`; данные — `/var/lib/peni-measure/measure.sqlite3`
   (каталог создать с владельцем сервиса).
2. systemd unit `/etc/systemd/system/peni-measure.service`:

```ini
[Unit]
Description=peni.recruiter-radar.ru first-party measurement counter
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/peni-measure/serve.py --bind 127.0.0.1 --port 8101 --db /var/lib/peni-measure/measure.sqlite3
Restart=on-failure
RestartSec=5
DynamicUser=yes
StateDirectory=peni-measure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/peni-measure

[Install]
WantedBy=multi-user.target
```

   (при `DynamicUser` + `StateDirectory` данные живут в
   `/var/lib/private/peni-measure`; если нужен простой путь — заменить на
   `User=peni-measure` + ручной каталог.)

3. Аддитивный блок Caddy (к существующему сайту peni.recruiter-radar.ru):

```
peni.recruiter-radar.ru {
    # ... существующие директивы сайта ...
    handle /api/* {
        reverse_proxy 127.0.0.1:8101
    }
}
```

4. Smoke после деплоя: `curl https://peni.recruiter-radar.ru/api/healthz`;
   затем synthetic-событие (`syn:true`) через POST /api/event и проверка, что
   `/api/metric` показывает `synthetic_28d >= 1` и
   `successful_organic_calculations_28d` не изменился.

## Rollback (изолированный, по частям)

- Клиент: одна строка `transport: beaconTransport(globalThis)` в `js/app.js` —
  revert коммита этой ветки убирает beacon; localStorage-измерение остаётся.
- Proxy: удалить `handle /api/*` блок Caddy + `caddy reload` — endpoint
  исчезает с публичного origin, сайт продолжает работать (статика не задета).
- Сервис: `systemctl disable --now peni-measure` — процесс и данные изолированы
  в одном unit + одном каталоге; удаление каталога стирает всю накопленную
  статистику (других потребителей нет).
- Миграций БД нет: схема создаётся на старте (`init_db`), даунгрейд не нужен.
