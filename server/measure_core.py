#!/usr/bin/env python3
"""Core first-party measurement logic (stdlib only): allowlist validation,
sqlite aggregation and 28d metric queries. HTTP layer lives in serve.py.

Privacy boundaries (spec t_543102d7):
- ONLY allowlist fields are accepted; any extra field is a hard reject
  (fail closed), so PII injection never reaches storage;
- no amounts, no delay dates, no free-form strings except validated enums;
- dedupe id (did) is stored ONLY as a salted sha256 hash, never in clear,
  and is never tied to IP or User-Agent (those are not persisted at all);
- synthetic events share the counters but carry syn=1 and are excluded from
  every business numerator;
- counters are per UTC-day buckets, so the 28d metric and daily baseline
  maps are pure aggregation - no event-level table is ever exposed.
"""
import hashlib
import json  # noqa: F401  (re-exported for serve.py convenience)
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone, tzinfo

MEASURE_VERSION = 1
SCHEMA_VERSION = 1
WINDOW_DAYS_DEFAULT = 28
WINDOW_DAYS_MAX = 90
CLOCK_SKEW_MS = 5 * 60 * 1000
RETENTION_DAYS = WINDOW_DAYS_MAX + 7  # keep dedupe/visitor state bounded

# Ровно те же allowlist-поля, что и в js/measure.js EVENT_FIELDS.
EVENT_FIELDS = ['ts', 'v', 'calc', 'attr', 'ok', 'did', 'syn']
CALCULATORS = ['tax_penalty']
ATTRIBUTIONS = ['organic', 'referral', 'direct', 'unknown']

_METRIC_KEYS = [
    'metric', 'version', 'window_days',
    'successful_organic_calculations_28d',
    'unique_organic_visitors_28d',
    'business_all_attributions_28d',
    'by_attribution_28d',
    'synthetic_28d',
    'days',
]


class EventRejected(ValueError):
    """Валидация не прошла; code - стабильный машинный код для ответа 400."""

    def __init__(self, code, detail=''):
        super().__init__('%s%s' % (code, (': ' + detail) if detail else ''))
        self.code = code


_DID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_counters(
  day TEXT NOT NULL,
  calc TEXT NOT NULL,
  attr TEXT NOT NULL,
  syn INTEGER NOT NULL,
  events INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, calc, attr, syn));
CREATE TABLE IF NOT EXISTS did_days(
  day TEXT NOT NULL,
  did_hash TEXT NOT NULL,
  organic INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, did_hash));
CREATE TABLE IF NOT EXISTS seen_events(
  dedupe_key TEXT PRIMARY KEY,
  day TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_seen_day ON seen_events(day);
CREATE INDEX IF NOT EXISTS idx_did_days_day ON did_days(day);
"""


# --- Совместимость с Python 3.6 (prod VDS: Ubuntu 18.04, /usr/bin/python3 = 3.6.9) ---
# datetime.fromisoformat появился только в 3.7. Полифилл ТОЧНО повторяет
# грамматику CPython 3.7:
#   YYYY-MM-DD[*HH[:MM[:SS[.fff[fff]]]][±HH[:MM[:SS[.fff[fff]]]]]
# где * - любой одиночный разделитель, дробная часть - ровно 3 или 6 цифр,
# голое 'Z' не принимается (3.7 его тоже не принимает; parse_ts_ms заменяет
# его до парсинга). Любой прочий ввод -> ValueError, как у нативной функции.

def _parse_frac_36(body, value):
    n = 0
    while n < 6 and n < len(body) and body[n].isdigit():
        n += 1
    if n == 3:
        return int(body[0:3]) * 1000, body[3:]
    if n == 6:
        return int(body[0:6]), body[6:]
    raise ValueError('Invalid isoformat string: %r' % (value,))


class _FixedOffset36(tzinfo):
    """Фиксированное UTC-смещение для Python 3.6.

    timezone() в 3.6 принимает только целое число минут; fromisoformat в
    CPython 3.7 принимает ещё и смещения с секундами/микросекундами
    (например '+03:00:30'). Класс используется ТОЛЬКО для sub-minute
    смещений и повторяет граничную проверку 3.7: строго -24h < offset <
    +24h, иначе ValueError (parse_ts_ms превращает его в 'bad_ts').
    Sub-second смещения отклоняются: datetime в 3.6 не может их представить
    (utcoffset() требует целое число секунд), fail-closed -> 'bad_ts' 400.
    """

    def __init__(self, offset):
        if not -timedelta(hours=24) < offset < timedelta(hours=24):
            raise ValueError(
                'offset must be a timedelta strictly between '
                '-timedelta(hours=24) and timedelta(hours=24), not %r'
                % (offset,))
        if offset % timedelta(seconds=1) != timedelta(0):
            raise ValueError(
                'sub-second UTC offset is not representable on Python 3.6: %r'
                % (offset,))
        self._offset = offset

    def utcoffset(self, dt):
        return self._offset

    def dst(self, dt):
        return None

    def tzname(self, dt):
        total_s = int(self._offset.total_seconds())
        sign = '-' if total_s < 0 else '+'
        hours, rest = divmod(abs(total_s), 3600)
        minutes, seconds = divmod(rest, 60)
        name = 'UTC%s%02d:%02d' % (sign, hours, minutes)
        if seconds:
            name += ':%02d' % seconds
        return name

    def __repr__(self):
        return '_FixedOffset36(%r)' % (self._offset,)


def _make_tz_36(offset):
    # Целочисленные минутные смещения идут через нативный timezone()
    # (поведение 3.6 и 3.7 идентично, включая границу +-24h);
    # sub-minute - через _FixedOffset36 (timezone() в 3.6 их отклоняет).
    if offset % timedelta(minutes=1) == timedelta(0):
        return timezone(offset)
    return _FixedOffset36(offset)


def _parse_offset_36(body, value):
    sign = -1 if body[0] == '-' else 1
    body = body[1:]
    if len(body) < 2 or not body[0].isdigit() or not body[1].isdigit():
        raise ValueError('Invalid isoformat string: %r' % (value,))
    hours = int(body[0:2])
    body = body[2:]
    minutes = seconds = micro = 0
    if body[0:1] == ':':
        if len(body) < 3 or not body[1].isdigit() or not body[2].isdigit():
            raise ValueError('Invalid isoformat string: %r' % (value,))
        minutes = int(body[1:3])
        body = body[3:]
        if body[0:1] == ':':
            if len(body) < 3 or not body[1].isdigit() or not body[2].isdigit():
                raise ValueError('Invalid isoformat string: %r' % (value,))
            seconds = int(body[1:3])
            body = body[3:]
            if body[0:1] == '.':
                micro, body = _parse_frac_36(body[1:], value)
    tz = _make_tz_36(sign * timedelta(
        hours=hours, minutes=minutes, seconds=seconds, microseconds=micro))
    return tz, body


def _fromisoformat_36(value):
    if not isinstance(value, str):
        raise TypeError('fromisoformat: argument must be str')
    date_part = value[0:10]
    if len(date_part) != 10 or date_part[4] != '-' or date_part[7] != '-':
        raise ValueError('Invalid isoformat string: %r' % (value,))
    for pos in (0, 1, 2, 3, 5, 6, 8, 9):
        if not date_part[pos].isdigit():
            raise ValueError('Invalid isoformat string: %r' % (value,))
    if len(value) == 10:
        return datetime(int(date_part[0:4]), int(date_part[5:7]),
                        int(date_part[8:10]))
    body = value[11:]  # value[10] - разделитель (любой одиночный символ)
    hour = minute = second = micro = 0
    if len(body) < 2 or not body[0].isdigit() or not body[1].isdigit():
        raise ValueError('Invalid isoformat string: %r' % (value,))
    hour = int(body[0:2])
    body = body[2:]
    if body[0:1] == ':':
        if len(body) < 3 or not body[1].isdigit() or not body[2].isdigit():
            raise ValueError('Invalid isoformat string: %r' % (value,))
        minute = int(body[1:3])
        body = body[3:]
        if body[0:1] == ':':
            if len(body) < 3 or not body[1].isdigit() or not body[2].isdigit():
                raise ValueError('Invalid isoformat string: %r' % (value,))
            second = int(body[1:3])
            body = body[3:]
            if body[0:1] == '.':
                micro, body = _parse_frac_36(body[1:], value)
    tz = None
    if body[0:1] in ('+', '-'):
        tz, body = _parse_offset_36(body, value)
    if body:
        raise ValueError('Invalid isoformat string: %r' % (value,))
    return datetime(int(date_part[0:4]), int(date_part[5:7]),
                    int(date_part[8:10]), hour, minute, second, micro, tz)


try:  # Python >= 3.7
    _fromisoformat = datetime.fromisoformat
except AttributeError:  # Python 3.6: production VDS runtime
    _fromisoformat = _fromisoformat_36


def utc_now_ms():
    return int(time.time() * 1000)


def day_str_of(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d')


def parse_ts_ms(ts):
    if not isinstance(ts, str) or not ts.strip():
        raise EventRejected('bad_ts', 'ts must be a non-empty string')
    raw = ts.strip()
    if raw.endswith(('Z', 'z')):
        raw = raw[:-1] + '+00:00'
    try:
        dt = _fromisoformat(raw)
    except ValueError:
        raise EventRejected('bad_ts', 'unparseable ISO timestamp')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def sanitize_event(obj, now_ms=None):
    """Strict allowlist + enum/bounds validation. Returns normalized event."""
    now_ms = utc_now_ms() if now_ms is None else now_ms
    if not isinstance(obj, dict):
        raise EventRejected('bad_json', 'event must be a JSON object')
    extra = sorted(set(obj) - set(EVENT_FIELDS))
    if extra:
        # PII/суммы/даты инъекция отклоняется целиком, а не вырезается.
        raise EventRejected('unknown_field', ','.join(extra))
    for name in EVENT_FIELDS:
        if name not in obj:
            raise EventRejected('bad_field', name)
    ts_ms = parse_ts_ms(obj['ts'])
    if ts_ms > now_ms + CLOCK_SKEW_MS:
        raise EventRejected('ts_skew', 'timestamp in the future')
    if ts_ms < now_ms - RETENTION_DAYS * 86400000:
        raise EventRejected('too_old', 'beyond retention horizon')
    if type(obj['v']) is not int or obj['v'] != MEASURE_VERSION:
        raise EventRejected('bad_field', 'v')
    if obj['calc'] not in CALCULATORS:
        raise EventRejected('bad_field', 'calc')
    if obj['attr'] not in ATTRIBUTIONS:
        raise EventRejected('bad_field', 'attr')
    if obj['ok'] is not True:
        raise EventRejected('not_success', 'only successful calculations accepted')
    did = obj['did']
    if not isinstance(did, str) or not _DID_RE.match(did.lower()):
        raise EventRejected('bad_field', 'did')
    if type(obj['syn']) is not bool:
        raise EventRejected('bad_field', 'syn')
    return {
        'ts_ms': ts_ms,
        'calc': obj['calc'],
        'attr': obj['attr'],
        'did': did.lower(),
        'syn': obj['syn'],
    }


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def init_db(db_path):
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            'INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)',
            ('schema_version', str(SCHEMA_VERSION)))
        row = conn.execute(
            "SELECT value FROM meta WHERE key='did_salt'").fetchone()
        if not row:
            # Соль только для хэша did; не связана с IP/UA, хранится рядом с БД.
            conn.execute(
                'INSERT INTO meta(key, value) VALUES(?, ?)',
                ('did_salt', secrets.token_hex(16)))
    return db_path


def _did_hash(conn, did):
    row = conn.execute("SELECT value FROM meta WHERE key='did_salt'").fetchone()
    return hashlib.sha256((row[0] + '|' + did).encode('utf-8')).hexdigest()


def _maybe_maintenance(conn, now_ms):
    """Lazy daily purge: дедуп-ключи и visitor-days живут не дольше retention."""
    today = day_str_of(now_ms)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_maintenance_day'").fetchone()
    if row and row[0] == today:
        return
    cutoff = day_str_of(now_ms - RETENTION_DAYS * 86400000)
    conn.execute('DELETE FROM seen_events WHERE day < ?', (cutoff,))
    conn.execute('DELETE FROM did_days WHERE day < ?', (cutoff,))
    # INSERT OR REPLACE == upsert для meta (key - PRIMARY KEY, FK/триггеров
    # нет), но не требует SQLite >= 3.24: prod VDS (Ubuntu 18.04) несёт
    # SQLite 3.22, а UPSERT-синтаксис (conflict-clause DO UPDATE) появился
    # только в 3.24. Литерал намеренно не приводится: модуль охраняет
    # test_no_upsert_syntax_in_module_source.
    conn.execute(
        'INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)',
        ('last_maintenance_day', today))


def ingest_event(db_path, payload, now_ms=None):
    """Validate + idempotent ingest. Returns {'ok': True, 'deduped': bool}."""
    now_ms = utc_now_ms() if now_ms is None else now_ms
    ev = sanitize_event(payload, now_ms)
    day = day_str_of(ev['ts_ms'])
    with closing(connect(db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            dh = _did_hash(conn, ev['did'])
            key = hashlib.sha256(
                ('%s|%d|%s' % (dh, ev['ts_ms'], ev['calc'])).encode('utf-8')
            ).hexdigest()
            cur = conn.execute(
                'INSERT OR IGNORE INTO seen_events(dedupe_key, day) VALUES(?, ?)',
                (key, day))
            deduped = (cur.rowcount == 0)
            if not deduped:
                # Точный аналог upsert без UPSERT-синтаксиса (conflict-clause
                # DO UPDATE требует SQLite >= 3.24; prod Ubuntu 18.04 несёт
                # 3.22): гарантируем строку и увеличиваем счётчик.
                # Атомарность обеспечивает окружающий BEGIN IMMEDIATE.
                syn_flag = 1 if ev['syn'] else 0
                conn.execute(
                    'INSERT OR IGNORE INTO event_counters'
                    '(day, calc, attr, syn, events) VALUES(?, ?, ?, ?, 0)',
                    (day, ev['calc'], ev['attr'], syn_flag))
                conn.execute(
                    'UPDATE event_counters SET events = events + 1 '
                    'WHERE day=? AND calc=? AND attr=? AND syn=?',
                    (day, ev['calc'], ev['attr'], syn_flag))
                if not ev['syn'] and ev['attr'] == 'organic':
                    conn.execute(
                        'INSERT OR IGNORE INTO did_days(day, did_hash, organic) '
                        'VALUES(?, ?, 0)', (day, dh))
                    conn.execute(
                        'UPDATE did_days SET organic = organic + 1 '
                        'WHERE day=? AND did_hash=?', (day, dh))
            _maybe_maintenance(conn, now_ms)
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    return {'ok': True, 'deduped': deduped}


def metric(db_path, window_days=WINDOW_DAYS_DEFAULT, now_ms=None):
    """Aggregate 28d (или меньше/больше в пределах 1..90) метрика из счётчиков."""
    if type(window_days) is not int or not (
            1 <= window_days <= WINDOW_DAYS_MAX):
        raise EventRejected('bad_days', 'window_days must be int 1..90')
    now_ms = utc_now_ms() if now_ms is None else now_ms
    start_day = day_str_of(now_ms - (window_days - 1) * 86400000)
    end_day = day_str_of(now_ms)
    with closing(connect(db_path)) as conn:
        org = conn.execute(
            'SELECT COALESCE(SUM(events), 0) FROM event_counters '
            "WHERE syn = 0 AND attr = 'organic' AND day BETWEEN ? AND ?",
            (start_day, end_day)).fetchone()[0]
        biz = conn.execute(
            'SELECT COALESCE(SUM(events), 0) FROM event_counters '
            'WHERE syn = 0 AND day BETWEEN ? AND ?',
            (start_day, end_day)).fetchone()[0]
        syn = conn.execute(
            'SELECT COALESCE(SUM(events), 0) FROM event_counters '
            'WHERE syn = 1 AND day BETWEEN ? AND ?',
            (start_day, end_day)).fetchone()[0]
        by_attr_rows = conn.execute(
            'SELECT attr, COALESCE(SUM(events), 0) FROM event_counters '
            'WHERE syn = 0 AND day BETWEEN ? AND ? GROUP BY attr',
            (start_day, end_day)).fetchall()
        uniq = conn.execute(
            'SELECT COUNT(DISTINCT did_hash) FROM did_days '
            'WHERE day BETWEEN ? AND ?',
            (start_day, end_day)).fetchone()[0]
        per_day_rows = conn.execute(
            'SELECT day, '
            "  SUM(CASE WHEN syn = 0 THEN events ELSE 0 END) AS business, "
            "  SUM(CASE WHEN syn = 1 THEN events ELSE 0 END) AS synthetic, "
            "  SUM(CASE WHEN syn = 0 AND attr = 'organic' THEN events ELSE 0 END) AS organic "
            'FROM event_counters WHERE day BETWEEN ? AND ? '
            'GROUP BY day',
            (start_day, end_day)).fetchall()
    by_attr = {a: 0 for a in ATTRIBUTIONS}
    for attr, cnt in by_attr_rows:
        by_attr[attr if attr in ATTRIBUTIONS else 'unknown'] = cnt
    per_day = {row[0]: {'business': row[1], 'synthetic': row[2], 'organic': row[3]}
               for row in per_day_rows}
    days = []
    cursor_ms = now_ms - (window_days - 1) * 86400000
    for _ in range(window_days):
        d = day_str_of(cursor_ms)
        bucket = per_day.get(d, {'business': 0, 'synthetic': 0, 'organic': 0})
        days.append({'day': d, 'business': bucket['business'],
                     'synthetic': bucket['synthetic'], 'organic': bucket['organic']})
        cursor_ms += 86400000
    return {
        'metric': 'successful_organic_calculations_%dd' % window_days
                  if window_days != WINDOW_DAYS_DEFAULT
                  else 'successful_organic_calculations_28d',
        'version': MEASURE_VERSION,
        'window_days': window_days,
        'successful_organic_calculations_28d': org,
        'unique_organic_visitors_28d': uniq,
        'business_all_attributions_28d': biz,
        'by_attribution_28d': by_attr,
        'synthetic_28d': syn,
        'days': days,
    }
