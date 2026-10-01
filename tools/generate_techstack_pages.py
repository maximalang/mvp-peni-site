#!/usr/bin/env python3
"""Генератор статических страниц «техстек-чекер» для каталога /techstack/.

Вход (без сети, только локальные данные):
  data/techstack/domains.txt  — список доменов (по одному на строку, '#' — комментарий);
  data/techstack/scans.json   — сырой вывод батча webstack-scanner (JSON-массив
                                или JSONL из объектов-записей скана);
  data/techstack/skips.json   — [{"domain": ..., "reason": ...}] — домены,
                                исключённые/не просканированные, с причиной.

Выход:
  techstack/<slug>/index.html — страница домена (RU-копия, таблица инструментов);
  techstack/index.html        — индексная страница каталога;
  sitemap.xml                 — секция /techstack/ обновляется идемпотентно.

Страницы создаются только для успешных сканов (tool_count >= 1, без fetch_error).
В HTML рендерятся исключительно поля белого списка (name, categories, confidence,
version + домен/final-url/статус/дата/число инструментов) — PII не проходит.

Fail-closed рендеринг недоверенных данных скана (hardening PR9, 2026-10-01):
  * JSON-LD сериализуется для контекста HTML <script>: символы < > & и
    разделители строк U+2028/U+2029 заменяются на \\uXXXX-эквиваленты, поэтому
    значение поля не может закрыть <script> и открыть новый; текст остаётся
    валидным JSON с идентичным семантическим содержимым (jsonld()).
  * URL из записи скана проходит валидацию схемы: в href допускаются только
    http/https с непустым host и без управляющих символов/пробелов; любой иной
    или malformed URL детерминированно рендерится как экранированный текст без
    ссылки (safe_href() + render_domain_page()).

Python 3.11+, только stdlib. Детерминированный вывод (сортировка по домену).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

BASE_URL_DEFAULT = "https://peni.recruiter-radar.ru"

# Белый список полей записи скана, которые могут попасть в HTML.
PAGE_FIELDS = ("domain", "url", "http_status", "scanned_at", "tool_count")
TOOL_FIELDS = ("name", "categories", "confidence", "version")

DISCLAIMER = (
    "Данные получены автоматическим анализом открытой главной страницы {domain} "
    "и могут быть неполными: клиентские JavaScript-приложения без серверных "
    "маркеров определяются частично. Не является официальным заявлением владельца сайта."
)


def slugify(domain: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", domain.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot build slug for domain {domain!r}")
    return slug


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def load_domains(path: Path) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        d = line.lower()
        if d not in seen:
            seen.add(d)
            domains.append(d)
    return domains


def load_scans(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("scans.json: top-level JSON value is not an array")
        return data
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def load_skips(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("skips.json: top-level JSON value is not an array")
    return data


def pick_success_records(scans: list[dict], domains: list[str]) -> dict[str, dict]:
    """Лучшая успешная запись на домен: max tool_count, затем свежайшая scanned_at."""
    allowed = set(domains)
    best: dict[str, dict] = {}
    unknown = 0
    for rec in scans:
        domain = str(rec.get("domain", "")).strip().lower()
        if domain not in allowed:
            unknown += 1
            continue
        if rec.get("fetch_error") or int(rec.get("tool_count") or 0) < 1:
            continue
        prev = best.get(domain)
        key = (int(rec.get("tool_count") or 0), str(rec.get("scanned_at") or ""))
        if prev is None or key > (
            int(prev.get("tool_count") or 0),
            str(prev.get("scanned_at") or ""),
        ):
            best[domain] = rec
    if unknown:
        print(f"warn: {unknown} scan record(s) for domains outside domains.txt ignored",
              file=sys.stderr)
    return best


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(iso)[:10]


def fmt_confidence(value: object) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "—"


def meta_head(*, title: str, description: str, canonical: str, stylesheet: str) -> str:
    t, d, c = esc(title), esc(description), esc(canonical)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{t}</title>\n"
        f'<link rel="canonical" href="{c}">\n'
        f'<meta name="description" content="{d}">\n'
        "<!-- OpenGraph -->\n"
        f'<meta property="og:title" content="{t}">\n'
        f'<meta property="og:description" content="{d}">\n'
        '<meta property="og:type" content="website">\n'
        f'<meta property="og:url" content="{c}">\n'
        '<meta property="og:locale" content="ru_RU">\n'
        "<!-- Twitter card -->\n"
        '<meta name="twitter:card" content="summary">\n'
        f'<meta name="twitter:title" content="{t}">\n'
        f'<meta name="twitter:description" content="{d}">\n'
        f'<link rel="stylesheet" href="{esc(stylesheet)}">\n'
    )


# Символы, недопустимые в URL, который мы выпускаем в href (управляющие,
# пробельные и DEL): их наличие — признак обфускации схемы (java\tscript: и т.п.).
_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")

# Fail-closed белый список схем для исходящих ссылок.
SAFE_URL_SCHEMES = ("http", "https")

# Экранирование JSON-текста для безопасной вставки внутрь HTML <script>:
# ни один из этих символов не может появиться в JSON вне строковых значений,
# поэтому замена сохраняет валидность JSON и его семантику — JSON-парсер
# возвращает исходный символ из escape-последовательности.
def _unicode_escape(ch: str) -> str:
    return chr(92) + "u%04x" % ord(ch)


_JSONLD_HTML_ESCAPES = tuple(
    (ch, _unicode_escape(ch)) for ch in ("&", "<", ">", "\u2028", "\u2029")
)


def safe_href(url: object) -> str | None:
    """URL, безопасный для href, либо None (fail-closed).

    Допускаются только схемы http/https с непустым host. Отклоняются:
    javascript:/data:/vbscript: и любые иные схемы, scheme-relative //host,
    пустые и относительные значения, управляющие символы и пробелы внутри
    (обфускация схемы вида java\\tscript:), URL без host.
    """
    candidate = str(url).strip()
    if not candidate or _UNSAFE_URL_CHARS.search(candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in SAFE_URL_SCHEMES:
        return None
    if not parts.hostname:
        return None
    return candidate


def jsonld(obj: object) -> str:
    """JSON-LD блок, безопасный для контекста HTML <script>.

    Сериализованный JSON экранируется (&, <, >, U+2028, U+2029 -> \\uXXXX),
    поэтому attacker-controlled значение (например, имя инструмента, содержащее
    </script>) не может завершить блок и породить исполняемые script-элементы.
    Результат остаётся валидным JSON с идентичным содержимым.
    """
    payload = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    for char, escaped in _JSONLD_HTML_ESCAPES:
        payload = payload.replace(char, escaped)
    return (
        '<script type="application/ld+json">\n'
        + payload
        + "\n</script>\n"
    )


def clean_tools(rec: dict) -> list[dict]:
    tools = []
    for t in rec.get("tools") or []:
        if not isinstance(t, dict):
            continue
        item = {}
        for field in TOOL_FIELDS:
            if field in t:
                item[field] = t[field]
        if item.get("name"):
            tools.append(item)
    tools.sort(key=lambda x: (-float(x.get("confidence") or 0), str(x.get("name"))))
    return tools


def render_domain_page(rec: dict, base_url: str) -> str:
    domain = str(rec["domain"]).strip().lower()
    slug = slugify(domain)
    canonical = f"{base_url}/techstack/{slug}/"
    tools = clean_tools(rec)
    date = fmt_date(rec.get("scanned_at"))
    names = ", ".join(str(t["name"]) for t in tools[:4])
    title = f"Технологии сайта {domain} — что используется"
    description = (
        f"Какие технологии использует сайт {domain}: {names}"
        f" — всего инструментов: {len(tools)}. Данные скана открытой главной страницы от {date}."
    )
    if len(description) > 200:
        description = description[:197] + "..."
    rows = []
    for t in tools:
        cats = ", ".join(str(c) for c in (t.get("categories") or [])) or "—"
        ver = esc(t.get("version") or "—")
        rows.append(
            "      <tr>"
            f"<td>{esc(t['name'])}</td>"
            f"<td>{esc(cats)}</td>"
            f"<td>{fmt_confidence(t.get('confidence'))}</td>"
            f"<td>{ver}</td>"
            "</tr>"
        )
    final_url = str(rec.get("url") or f"https://{domain}")
    safe_url = safe_href(final_url)
    if safe_url is not None:
        final_url_html = (
            f'<a rel="nofollow noopener" href="{esc(safe_url)}">{esc(safe_url)}</a>'
        )
    else:
        # Fail-closed: опасная/malformed схема URL — экранированный текст без ссылки.
        final_url_html = f"<span>{esc(final_url)}</span>"
    status = rec.get("http_status")
    status_txt = esc(status) if status is not None else "—"
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "name": title,
                "description": description,
                "url": canonical,
                "inLanguage": "ru",
            },
            {
                "@type": "ItemList",
                "name": f"Инструменты сайта {domain}",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "name": str(t["name"]),
                    }
                    for i, t in enumerate(tools)
                ],
            },
        ],
    }
    return (
        meta_head(title=title, description=description, canonical=canonical,
                  stylesheet="../../styles.css")
        + jsonld(ld)
        + "</head>\n<body>\n<main class=\"page\">\n"
        + f'  <p><a href="../">&larr; Все техстеки</a> · <a href="../../">Калькулятор пеней</a></p>\n'
        + f"  <h1>Технологии сайта {esc(domain)} — что используется</h1>\n"
        + f'  <p class="lead">Найдено инструментов: <strong>{len(tools)}</strong>. '
        + f"Дата скана: {esc(date)}. HTTP-статус главной: {status_txt}.</p>\n"
        + '  <table class="breakdown">\n'
        + "    <caption>Стек сайта " + esc(domain) + "</caption>\n"
        + "    <thead><tr><th>Инструмент</th><th>Категории</th><th>Уверенность</th><th>Версия</th></tr></thead>\n"
        + "    <tbody>\n" + "\n".join(rows) + "\n    </tbody>\n  </table>\n"
        + f'  <p class="disclaimer">{esc(DISCLAIMER.format(domain=domain))} '
        + f'Проверенный адрес: {final_url_html}.</p>\n'
        + '  <nav class="crosslinks" aria-label="Смежные материалы">\n'
        + '    <a href="../">Каталог техстеков</a>\n'
        + '    <a href="../../">Калькулятор пеней по налогам</a>\n'
        + "  </nav>\n"
        + "</main>\n</body>\n</html>\n"
    )


def render_index_page(records: dict[str, dict], skips: list[dict], base_url: str) -> str:
    canonical = f"{base_url}/techstack/"
    domains = sorted(records.keys())
    title = f"Технологии сайтов — каталог проверенных доменов ({len(domains)} сайтов)"
    description = (
        f"Каталог из {len(domains)} популярных сайтов: какие технологии и инструменты "
        "использует каждый домен, по данным автоматического скана открытой главной страницы."
    )
    ld = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "inLanguage": "ru",
    }
    rows = []
    for d in domains:
        rec = records[d]
        slug = slugify(d)
        rows.append(
            "      <tr>"
            f'<td><a href="./{esc(slug)}/">{esc(d)}</a></td>'
            f"<td>{int(rec.get('tool_count') or 0)}</td>"
            f"<td>{esc(fmt_date(rec.get('scanned_at')))}</td>"
            "</tr>"
        )
    skip_note = ""
    if skips:
        items = "; ".join(
            f"{esc(s.get('domain', '?'))} — {esc(s.get('reason', 'причина не указана'))}"
            for s in skips
        )
        skip_note = (
            '  <p class="disclaimer">Некоторые домены из публичных топ-листов не удалось '
            "просканировать с нашего IP и они исключены из каталога: " + items + ".</p>\n"
        )
    return (
        meta_head(title=title, description=description, canonical=canonical,
                  stylesheet="../styles.css")
        + jsonld(ld)
        + "</head>\n<body>\n<main class=\"page\">\n"
        + '  <p><a href="../">&larr; Калькулятор пеней</a></p>\n'
        + "  <h1>Технологии сайтов — каталог проверенных доменов</h1>\n"
        + f'  <p class="lead">{len(domains)} популярных сайтов России и мира: стек технологий '
        + "по данным автоматического скана открытой главной страницы.</p>\n"
        + '  <table class="breakdown">\n'
        + "    <caption>Домены каталога</caption>\n"
        + "    <thead><tr><th>Сайт</th><th>Инструментов</th><th>Дата скана</th></tr></thead>\n"
        + "    <tbody>\n" + "\n".join(rows) + "\n    </tbody>\n  </table>\n"
        + skip_note
        + '  <nav class="crosslinks" aria-label="Смежные материалы">\n'
        + '    <a href="../">Калькулятор пеней по налогам</a>\n'
        + '    <a href="../faq.html">Частые вопросы</a>\n'
        + "  </nav>\n"
        + "</main>\n</body>\n</html>\n"
    )


TECHSTACK_URL_BLOCK = re.compile(
    r"[ \t]*<url>\s*<loc>[^<]*/techstack/[^<]*</loc>.*?</url>\s*",
    re.DOTALL,
)


def update_sitemap(path: Path, base_url: str, slugs: list[str]) -> int:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text.strip():
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "</urlset>\n"
        )
    text = TECHSTACK_URL_BLOCK.sub("", text)
    blocks = [f"  <url>\n    <loc>{base_url}/techstack/</loc>\n    <priority>0.7</priority>\n  </url>\n"]
    for slug in slugs:
        blocks.append(
            f"  <url>\n    <loc>{base_url}/techstack/{slug}/</loc>\n    <priority>0.5</priority>\n  </url>\n"
        )
    section = "".join(blocks)
    idx = text.rfind("</urlset>")
    if idx < 0:
        raise ValueError("sitemap.xml: closing </urlset> not found")
    new_text = text[:idx] + section + text[idx:]
    new_text = new_text.replace("\r\n", "\n")
    path.write_text(new_text, encoding="utf-8", newline="\n")
    return len(blocks)


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data/techstack", help="каталог входных данных")
    ap.add_argument("--out", default=".", help="корень сайта (куда писать techstack/)")
    ap.add_argument("--sitemap", default=None, help="путь к sitemap.xml (default: <out>/sitemap.xml)")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    ap.add_argument("--no-sitemap", action="store_true", help="не обновлять sitemap.xml")
    args = ap.parse_args(argv)

    data = Path(args.data)
    out = Path(args.out)
    base_url = args.base_url.rstrip("/")

    domains = load_domains(data / "domains.txt")
    scans = load_scans(data / "scans.json")
    skips = load_skips(data / "skips.json")
    records = pick_success_records(scans, domains)
    if not records:
        print("error: no successful scans (tool_count >= 1) for listed domains", file=sys.stderr)
        return 1

    pages = 0
    for domain in sorted(records):
        slug = slugify(domain)
        write_lf(out / "techstack" / slug / "index.html", render_domain_page(records[domain], base_url))
        pages += 1
    write_lf(out / "techstack" / "index.html", render_index_page(records, skips, base_url))

    sitemap_urls = 0
    if not args.no_sitemap:
        sitemap_path = Path(args.sitemap) if args.sitemap else out / "sitemap.xml"
        sitemap_urls = update_sitemap(
            sitemap_path, base_url, [slugify(d) for d in sorted(records)]
        )

    print(json.dumps({
        "domains_listed": len(domains),
        "scan_records": len(scans),
        "pages_generated": pages,
        "index_generated": True,
        "sitemap_urls": sitemap_urls,
        "skipped": len(skips),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
