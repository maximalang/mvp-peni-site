# -*- coding: utf-8 -*-
"""Refresh-harness ставки ЦБ для MVP-1 (требование W4: обновление данных с верификацией).

Логика fail-closed:
1. Тянет https://www.cbr.ru/hd_base/KeyRate/
2. Парсит тем же алгоритмом, что readiness-пакет (table.data -> Дата/Ставка)
3. Сравнивает с текущим data/key-rates.json:
   - без изменений -> выход 0, файл не трогается
   - изменения ТОЛЬКО добавлением новых дат (история неизменяема) -> запись нового файла
   - изменение/удаление исторических строк -> ОТКАЗ (выход 3), файл не трогается
4. После любого обновления гоняет node tests/calc.test.mjs: падение -> откат и выход 2

Коды выхода: 0 = ok/no-change | 2 = fixture fail (откачено) | 3 = history drift | 4 = сеть/схема.
"""
import hashlib, json, subprocess, sys, urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
DATA = SITE / "data" / "key-rates.json"
SNAP_DIR = SITE.parent / "mvp-1-readiness-2026-08-25" / "source_snapshots"
SOURCE_URL = "https://www.cbr.ru/hd_base/KeyRate/"


class KeyRateTableParser(HTMLParser):
    """Порт _KeyRateTableParser из penalty_model.py."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.depth = 0
        self.row = None
        self.cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        amap = dict(attrs)
        if tag == "table" and amap.get("class") == "data" and not self.in_table:
            self.in_table, self.depth = True, 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self.depth += 1
        elif tag == "tr":
            self.row = []
        elif tag in {"th", "td"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        import re as _re
        if tag in {"th", "td"} and self.cell is not None and self.row is not None:
            self.row.append(_re.sub(r"\s+", "", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_table = False


def parse_rows(html_bytes: bytes):
    p = KeyRateTableParser()
    p.feed(html_bytes.decode("utf-8", "replace"))
    if not p.rows or p.rows[0] != ["Дата", "Ставка"]:
        raise ValueError("source schema drift: header != Дата/Ставка")
    out = []
    for r in p.rows[1:]:
        if len(r) != 2:
            raise ValueError("source schema drift: row %r" % (r,))
        d = datetime.strptime(r[0], "%d.%m.%Y").date().isoformat()
        pct = "%.2f" % float(r[1].replace(",", "."))
        if not 0 < float(pct) < 100:
            raise ValueError("source value drift: %s" % pct)
        out.append({"d": d, "p": pct})
    out.sort(key=lambda x: x["d"])
    ds = [x["d"] for x in out]
    if len(set(ds)) != len(ds):
        raise ValueError("duplicate dates in source")
    return out


def fetch():
    # Полный ряд ЦБ отдаёт только при явном диапазоне UniDbQuery (см. source_probe.py).
    try:
        first = min(r["d"] for r in json.loads(
            DATA.read_text(encoding="utf-8"))["rows"]).split("-")
    except Exception:
        first = ["2013", "09", "17"]
    from_d = "%s.%s.%s" % tuple(first[::-1])
    to = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%d.%m.%Y")
    url = SOURCE_URL + "?UniDbQuery.Posted=True&UniDbQuery.From=%s&UniDbQuery.To=%s" % (from_d, to)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def main():
    old_doc = json.loads(DATA.read_text(encoding="utf-8"))
    old_map = {r["d"]: r["p"] for r in old_doc["rows"]}

    try:
        html_bytes = fetch()
    except Exception as exc:  # сеть упала -> старые данные остаются в силе
        print("FETCH_FAIL %s" % exc)
        return 4
    try:
        fresh = parse_rows(html_bytes)
    except ValueError as exc:
        print("SCHEMA_DRIFT %s" % exc)
        return 4

    new_map = {r["d"]: r["p"] for r in fresh}
    changed = {d for d in new_map if d in old_map and old_map[d] != new_map[d]}
    removed = set(old_map) - set(new_map)
    added = sorted(set(new_map) - set(old_map))

    if changed or removed:
        print("HISTORY_DRIFT changed=%s removed=%s" % (sorted(changed), sorted(removed)))
        return 3
    if not added:
        print("NO_CHANGE rows=%d" % len(fresh))
        return 0

    # Сохраняем сырой снимок как evidence
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    snap = SNAP_DIR / ("cbr-keyrate-refresh-%s.html" % stamp)
    snap.write_bytes(html_bytes)

    tmp = DATA.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "source": SOURCE_URL,
        "captured_at": stamp,
        "source_sha256": hashlib.sha256(html_bytes).hexdigest(),
        "rows": fresh,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # Гейт фикстур на НОВЫХ данных до подмены рабочего файла
    DATA.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
    tmp.unlink()
    run = subprocess.run(["node", str(SITE / "tests" / "calc.test.mjs")],
                         capture_output=True, text=True)
    if run.returncode != 0:
        old_data = json.dumps(old_doc, ensure_ascii=False, indent=1)
        DATA.write_text(old_data, encoding="utf-8")
        print("FIXTURES_FAILED_ROLLBACK added=%s\n%s" % (added, run.stdout[-2000:]))
        return 2

    print("UPDATED +%d rows -> %d total, snapshot=%s, fixtures PASS" %
          (len(added), len(fresh), snap.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
