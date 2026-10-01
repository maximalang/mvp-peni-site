# Techstack pages — batch summary (task t_97447243, 30.09.2026)

- Доменов в наборе (domains.txt): 149 (начальные 100 = 50 RU + 50 EN, замены-спэры: RU 22, EN 27)
- Уникальных доменов live-просканировано: 149 (+1 smoke example.com; бюджет spec <=150 не превышен)
- Записей сканов всего (включая дубли/повторы): 149
- Успешно (tool_count>=1): 109 — RU 43, EN 66
- Скипнуто/неуспешно (skips.json): 54
- Суммарное время сканов: 641.1 с (elapsed_ms всех записей)
- Сгенерировано страниц доменов: 109 + индекс /techstack/

## Топ-10 технологий (по числу доменов, где обнаружены)

| # | Технология | Доменов |
|---|-----------|---------|
| 1 | HSTS | 76 |
| 2 | HTTP/3 | 28 |
| 3 | Nginx | 23 |
| 4 | Envoy | 15 |
| 5 | Google Tag Manager | 14 |
| 6 | Cloudflare | 13 |
| 7 | Liveinternet | 12 |
| 8 | Akamai Bot Manager | 11 |
| 9 | Varnish | 9 |
| 10 | Akamai | 9 |

## Причины неуспеха (агрегированно)

- TLS handshake reset from our IP: 23
- not scanned: 8
- DNS resolution blocked from our IP (regional blocking): 7
- HTTP 200 but zero server-side markers detected (JS-only response): 5
- SSL certificate verify failed from our IP (national CA chain not in curl trust store): 4
- not present in Tranco top-1m snapshot used; not scanned: 3
- HTTP 403 but zero server-side markers detected (JS-only response): 2
- HTTP 401 but zero server-side markers detected (JS-only response): 1
- fetch error: 1

Движок: capability webstack-scanner (enthec/webappanalyzer@local, curl-cffi impersonate chrome124).
Данные — только публичный уровень (домен + технологии), PII нет. Без фабрикаций: неуспешные домены перечислены в skips.json с причинами.
