# Деплой на Cloudflare Pages

Репозиторий: https://github.com/maximalang/mvp-peni-site

## Вариант A — GitHub → Cloudflare Pages (рекомендуется)

1. Откройте Cloudflare Dashboard → **Workers & Pages** → **Create application** → **Pages** → **Connect to Git**.
2. Разрешите Cloudflare доступ к GitHub и выберите приватный репозиторий `maximalang/mvp-peni-site`.
3. Выберите production-ветку `main` после её создания; для первого smoke-test можно выбрать ветку `codex/site-v2-seo-deploy`.
4. Оставьте тип проекта static: build command пустая, output directory — `/` (корень репозитория).
5. Нажмите **Save and Deploy** и дождитесь статуса **Success**.
6. Откройте выданный адрес `https://<project>.pages.dev` и передайте его координатору для замены `<PAGES_URL>` в `sitemap.xml` и canonical-тегах.

## Вариант B — Wrangler direct upload (fallback)

7. В локальной сессии владельца задайте `CF_API_TOKEN` с правом Pages Edit и `CLOUDFLARE_ACCOUNT_ID`; токен не записывайте в репозиторий и не присылайте в чат. Если Wrangler ожидает другое имя, передайте тот же секрет через его поддерживаемую переменную окружения.
8. Из корня репозитория создайте Pages-проект один раз (если его ещё нет), затем выполните: `wrangler pages deploy . --project-name=<name>`.
9. Проверьте выданный `https://<name>.pages.dev`, передайте URL координатору и дождитесь отдельного smoke-test всех HTML-страниц, `robots.txt`, `sitemap.xml` и `data/key-rates.json`.

## Ограничения

- До явной команды владельца PR не мержится в `main`.
- Домен не покупается и не настраивается в этом инкременте.
- Режим сайта статический: расчёты остаются в браузере, аналитика и отправка введённых данных не добавляются.
- После первого успешного деплоя `<PAGES_URL>` заменяется только на фактический Pages hostname; после замены повторно проверяются canonical, sitemap и robots.
