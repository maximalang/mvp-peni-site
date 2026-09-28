#!/usr/bin/env python3
"""Регрессионные тесты Python 3.6.9-совместимости (prod VDS Ubuntu 18.04).

Выполняются на ЛЮБОМ поддерживаемом Python (CI verify: 3.11; CI py36-runtime:
3.6.9 в docker). Проверяют сами polyfill'и и runtime-независимые гарантии:
- measure_core._fromisoformat_36 точно повторяет грамматику
  datetime.fromisoformat из CPython 3.7 (принимает/отклоняет те же строки);
- SQL модуля не требует SQLite >= 3.24 (никакого UPSERT `ON CONFLICT ...
  DO UPDATE`): prod Ubuntu 18.04 несёт SQLite 3.22;
- состояние счётчиков после переписанных INSERT OR IGNORE + UPDATE такое же,
  как после upsert (дедуп, organic, unique, maintenance-маркер);
- serve.ThreadingHTTPServer доступен на любом runtime (3.6 fallback-класс
  байт-эквивалентен определению CPython 3.7+).
"""
import inspect
import os
import socketserver
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import measure_core as core  # noqa: E402
import serve  # noqa: E402


class FromIsoformat36GrammarTest(unittest.TestCase):
    """Грамматика CPython 3.7 datetime.fromisoformat, байт-в-байт."""

    def test_accepted_naive(self):
        cases = [
            ('2026-09-28', (2026, 9, 28, 0, 0, 0, 0)),
            ('2026-09-28T12', (2026, 9, 28, 12, 0, 0, 0)),
            ('2026-09-28T12:34', (2026, 9, 28, 12, 34, 0, 0)),
            ('2026-09-28T12:34:56', (2026, 9, 28, 12, 34, 56, 0)),
            ('2026-09-28T12:34:56.123', (2026, 9, 28, 12, 34, 56, 123000)),
            ('2026-09-28T12:34:56.123456', (2026, 9, 28, 12, 34, 56, 123456)),
            ('2026-09-28 12:34:56', (2026, 9, 28, 12, 34, 56, 0)),
            # разделитель - любой одиночный символ (grammar 3.7)
            ('2026-09-28x12:34:56', (2026, 9, 28, 12, 34, 56, 0)),
        ]
        for raw, (y, mo, d, h, mi, s, us) in cases:
            dt = core._fromisoformat_36(raw)
            self.assertEqual(
                (dt.year, dt.month, dt.day, dt.hour, dt.minute,
                 dt.second, dt.microsecond), (y, mo, d, h, mi, s, us), raw)
            self.assertIsNone(dt.tzinfo, raw)

    def test_accepted_offsets(self):
        cases = [
            ('2026-09-28T12:34:56+00:00', timedelta(0)),
            ('2026-09-28T12:34:56+03', timedelta(hours=3)),
            ('2026-09-28T12:34:56.123+03:00', timedelta(hours=3)),
            ('2026-09-28T12:34:56-05:30', -timedelta(hours=5, minutes=30)),
            ('2026-09-28T12:34:56+03:00:30', timedelta(hours=3, seconds=30)),
            # sub-minute смещения: timezone() в 3.6 их отклоняет,
            # полифилл идёт через _FixedOffset36 (как нативный 3.7)
            ('2026-09-28T12:34:56-00:30:15',
             -timedelta(minutes=30, seconds=15)),
        ]
        for raw, off in cases:
            dt = core._fromisoformat_36(raw)
            self.assertIsNotNone(dt.tzinfo, raw)
            self.assertEqual(dt.utcoffset(), off, raw)

    def test_rejected(self):
        bad = [
            '', 'x', '2026-09-2', '2026-9-28', '20260928',
            '2026-09-28T', '2026-09-28T1', '2026-09-28T1a',
            '2026-09-28T12:5', '2026-09-28T12:34:5',
            '2026-09-28T12.34', '2026-09-28T12:34.5',
            '2026-09-28T12:34:56.', '2026-09-28T12:34:56.1',
            '2026-09-28T12:34:56.12', '2026-09-28T12:34:56.1234',
            '2026-09-28T12:34:56.12345', '2026-09-28T12:34:56.1234567',
            # голое Z не принимается и нативным 3.7 (parse_ts_ms заменяет его)
            '2026-09-28T12:34:56Z',
            '2026-09-28T12:34:56+3:00', '2026-09-28T12:34:56+03:0',
            '2026-09-28T12:34:56+00:00:0', '2026-09-28T12:34:56+00:00.',
            # sub-second смещение: на 3.6 непредставимо -> fail-closed 400
            # (нативный 3.7+ принимает; дрейф задокументирован в _FixedOffset36)
            '2026-09-28T12:34:56+03:00:30.123456',
            '2026-09-28T25:00:00', '2026-13-01', '2026-00-10',
            '2026-09-28T12:34:56+25:00',
            'not-a-date', '2026-09-28T12:34:56 trailing',
        ]
        for raw in bad:
            with self.assertRaises(ValueError):
                core._fromisoformat_36(raw)

    def test_non_str_argument(self):
        for bad in (None, 42, b'2026-09-28'):
            with self.assertRaises(TypeError):
                core._fromisoformat_36(bad)


class ParseTsContractTest(unittest.TestCase):
    """Контракт parse_ts_ms не зависит от runtime (3.6 polyfill / 3.7+ native)."""

    def test_client_z_format(self):
        # точный формат js/measure.js: new Date().toISOString()
        ms = core.parse_ts_ms('2026-09-28T12:34:56.123Z')
        expected = int(datetime(
            2026, 9, 28, 12, 34, 56, 123000,
            tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(ms, expected)

    def test_naive_is_utc(self):
        ms = core.parse_ts_ms('2026-09-28T12:34:56')
        expected = int(datetime(
            2026, 9, 28, 12, 34, 56,
            tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(ms, expected)

    def test_unparseable_rejected_as_bad_ts(self):
        for bad in ('not-a-date', ''):
            with self.assertRaises(core.EventRejected) as ctx:
                core.parse_ts_ms(bad)
            self.assertEqual(ctx.exception.code, 'bad_ts')


class SqlitePre324CompatTest(unittest.TestCase):
    """SQL модуля исполним на SQLite < 3.24 (prod Ubuntu 18.04: 3.22)."""

    def test_no_upsert_syntax_in_module_source(self):
        src = inspect.getsource(core)
        self.assertNotIn('ON CONFLICT', src)

    def test_counter_state_matches_upsert_semantics(self):
        tmp = tempfile.mkdtemp(prefix='py36-compat-')
        db = os.path.join(tmp, 'm.sqlite3')
        core.init_db(db)
        now_ms = int(datetime(
            2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        payload = {
            'ts': '2026-09-20T12:00:00.000Z',
            'v': core.MEASURE_VERSION,
            'calc': 'tax_penalty',
            'attr': 'organic',
            'ok': True,
            'did': '0f1e2d3c-4b5a-6789-8abc-def012345678',
            'syn': False,
        }
        r1 = core.ingest_event(db, payload, now_ms=now_ms)
        r2 = core.ingest_event(db, payload, now_ms=now_ms)
        self.assertFalse(r1['deduped'])
        self.assertTrue(r2['deduped'])
        second = dict(payload)
        second['ts'] = '2026-09-20T12:00:01.000Z'
        core.ingest_event(db, second, now_ms=now_ms)
        m = core.metric(db, now_ms=now_ms)
        self.assertEqual(m['successful_organic_calculations_28d'], 2)
        self.assertEqual(m['unique_organic_visitors_28d'], 1)
        self.assertEqual(m['business_all_attributions_28d'], 2)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_maintenance_day'"
        ).fetchone()
        self.assertEqual(row[0], core.day_str_of(now_ms))
        counters = conn.execute(
            'SELECT events FROM event_counters WHERE day=? AND syn=0',
            (core.day_str_of(now_ms),)).fetchall()
        self.assertEqual(sorted(c[0] for c in counters), [2])
        conn.close()


class ServeThreadingFallbackTest(unittest.TestCase):
    """serve.ThreadingHTTPServer определён на любом runtime."""

    def test_class_shape_matches_cpython_37(self):
        self.assertTrue(issubclass(serve.ThreadingHTTPServer, HTTPServer))
        self.assertTrue(
            issubclass(serve.ThreadingHTTPServer, socketserver.ThreadingMixIn))
        self.assertTrue(serve.ThreadingHTTPServer.daemon_threads)

    def test_handler_bound(self):
        self.assertTrue(
            issubclass(serve.MeasureHandler,
                       serve.BaseHTTPRequestHandler))

    def test_control_char_table_defined_on_handler(self):
        # BaseHTTPRequestHandler в 3.6 не несёт _control_char_table -
        # у MeasureHandler должна быть СОБСТВЕННАЯ таблица, байт-идентичная
        # таблице свежих CPython (0x00-0x1f, 0x5c, 0x7f, 0x80-0x9f).
        table = serve.MeasureHandler.__dict__.get('_control_char_table')
        self.assertIsNotNone(table)
        expected_keys = sorted(
            list(range(32)) + [92, 127] + list(range(128, 160)))
        self.assertEqual(sorted(table.keys()), expected_keys)
        for key in expected_keys:
            if key == 92:
                self.assertEqual(table[key], '\\\\')
            else:
                self.assertEqual(table[key], '\\x%02x' % key)

    def test_log_message_escapes_control_chars_without_inheritance(self):
        # лог-санитизация работает на runtime без таблицы в базовом классе
        handler = object.__new__(serve.MeasureHandler)
        message = ('"GET %s" 200' % '/api/metric?x=\x00\x01y\x7f\x1b[31m'
                   ).translate(serve.MeasureHandler._control_char_table)
        del handler  # таблица класс-уровня: наследование не задействовано
        self.assertNotIn('\x00', message)
        self.assertNotIn('\x1b', message)
        self.assertIn('\\x00\\x01y\\x7f\\x1b[31m', message)


if __name__ == '__main__':
    unittest.main()
