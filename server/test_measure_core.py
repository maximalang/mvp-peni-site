#!/usr/bin/env python3
"""Unit-тесты серверного ядра first-party measurement (server/measure_core.py).

Запуск: python3 -m unittest discover -s server -p 'test_*.py'  (из корня репо)
или:    cd server && python3 -m unittest test_measure_core.py -v

Проверяют acceptance из спеки t_543102d7:
- allowlist-фильтрация: инжект PII-полей отклоняется целиком (fail closed);
- synthetic-разделение: syn=1 не входит в business/organic числители;
- окно 28 дней: события старше cutoff не считаются;
- dedupe по did: повторный beacon (тот же did+ts+calc) не дублирует счётчик;
- did хранится только как соль-хэш: clear-значение отсутствует в БД;
- схема не содержит IP/UA-колонок; счётчики только агрегированные.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import measure_core as core  # noqa: E402

T0_MS = int(datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
DAY_MS = 24 * 60 * 60 * 1000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def valid_event(ts_ms=T0_MS, attr='organic', syn=False, did=None, seq=0):
    return {
        'ts': iso(ts_ms + seq),
        'v': core.MEASURE_VERSION,
        'calc': 'tax_penalty',
        'attr': attr,
        'ok': True,
        'did': did or '0f1e2d3c-4b5a-6789-8abc-def012345678',
        'syn': syn,
    }


class MeasureCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='measure-test-')
        self.db = os.path.join(self.tmp, 'measure.sqlite3')
        core.init_db(self.db)

    def ingest(self, payload, now_ms=T0_MS):
        return core.ingest_event(self.db, payload, now_ms=now_ms)

    def metric(self, days=28, now_ms=T0_MS):
        return core.metric(self.db, window_days=days, now_ms=now_ms)

    # -- allowlist / privacy ------------------------------------------------
    def test_pii_injection_rejected_entirely(self):
        payload = valid_event()
        payload.update({
            'arrears_rub': '100000',
            'first_delay_date': '2026-01-01',
            'ip': '1.2.3.4',
            'userAgent': 'Mozilla/5.0',
            'url': 'https://site/?a=100000&d1=2026-01-01',
        })
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(payload)
        self.assertEqual(ctx.exception.code, 'unknown_field')
        self.assertIn('arrears_rub', str(ctx.exception))
        self.assertEqual(self.metric()['successful_organic_calculations_28d'], 0)

    def test_partial_pii_field_rejected_and_nothing_stored(self):
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(valid_event(ts_ms=T0_MS - 27 * DAY_MS, attr='organic')
                        | {'note': 'hello'})
        self.assertEqual(ctx.exception.code, 'unknown_field')
        # даже валидная часть не записана: событие отклонено целиком
        self.assertEqual(self.metric()['business_all_attributions_28d'], 0)

    def test_non_dict_payload_rejected(self):
        for bad in ([valid_event()], 'str', 42, None):
            with self.assertRaises(core.EventRejected) as ctx:
                self.ingest(bad)
            self.assertEqual(ctx.exception.code, 'bad_json')

    def test_missing_field_rejected(self):
        for drop in core.EVENT_FIELDS:
            payload = valid_event()
            del payload[drop]
            with self.assertRaises(core.EventRejected) as ctx:
                self.ingest(payload)
            self.assertEqual(ctx.exception.code, 'bad_field')

    def test_only_successful_calculations_accepted(self):
        payload = valid_event()
        payload['ok'] = False
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(payload)
        self.assertEqual(ctx.exception.code, 'not_success')

    def test_enum_and_version_bounds(self):
        for mutate, code in (
            (lambda p: p.update(calc='mortgage'), 'bad_field'),
            (lambda p: p.update(attr='paid-ads'), 'bad_field'),
            (lambda p: p.update(v=2), 'bad_field'),
            (lambda p: p.update(v='1'), 'bad_field'),
            (lambda p: p.update(syn=1), 'bad_field'),
            (lambda p: p.update(did='not-a-uuid'), 'bad_field'),
            (lambda p: p.update(did=42), 'bad_field'),
        ):
            payload = valid_event()
            mutate(payload)
            with self.assertRaises(core.EventRejected) as ctx:
                self.ingest(payload)
            self.assertEqual(ctx.exception.code, code)

    def test_ts_validation(self):
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(valid_event() | {'ts': 'not-a-date'})
        self.assertEqual(ctx.exception.code, 'bad_ts')
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(valid_event() | {'ts': ''})
        self.assertEqual(ctx.exception.code, 'bad_ts')
        # будущее за пределом clock skew
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(valid_event(ts_ms=T0_MS + 6 * 60 * 1000))
        self.assertEqual(ctx.exception.code, 'ts_skew')
        # слишком старое (за retention)
        with self.assertRaises(core.EventRejected) as ctx:
            self.ingest(valid_event(ts_ms=T0_MS - (core.RETENTION_DAYS + 2) * DAY_MS))
        self.assertEqual(ctx.exception.code, 'too_old')
        # наивный ts трактуется как UTC и принимается
        naive = valid_event() | {'ts': '2026-09-20T12:00:00'}
        self.ingest(naive)
        self.assertEqual(self.metric()['successful_organic_calculations_28d'], 1)

    def test_did_never_stored_in_clear(self):
        did = 'aabbccdd-eeff-1122-3344-556677889900'
        self.ingest(valid_event(did=did))
        with open(self.db, 'rb') as fh:
            raw = fh.read()
        self.assertNotIn(did.encode('ascii'), raw)
        # хэш при этом присутствует и участвует в unique-подсчёте
        salt_row = sqlite3.connect(self.db).execute(
            "SELECT value FROM meta WHERE key='did_salt'").fetchone()
        expected = hashlib.sha256(
            (salt_row[0] + '|' + did).encode('utf-8')).hexdigest()
        self.assertIn(expected.encode('ascii'), raw)

    def test_schema_has_no_ip_or_agent_columns(self):
        conn = sqlite3.connect(self.db)
        for table in ('event_counters', 'did_days', 'seen_events', 'meta'):
            cols = [r[1] for r in conn.execute(
                'PRAGMA table_info(%s)' % table).fetchall()]
            joined = ','.join(cols).lower()
            self.assertNotIn('ip', joined.split(','), table)
            self.assertNotIn('user_agent', joined, table)
            self.assertNotIn('ua', joined.split(','), table)
            self.assertNotIn('referrer', joined, table)

    # -- dedupe / idempotency ----------------------------------------------
    def test_duplicate_beacon_does_not_double_count(self):
        ev = valid_event()
        r1 = self.ingest(ev)
        r2 = self.ingest(ev)  # повторный beacon: тот же did+ts+calc
        self.assertFalse(r1['deduped'])
        self.assertTrue(r2['deduped'])
        m = self.metric()
        self.assertEqual(m['successful_organic_calculations_28d'], 1)
        self.assertEqual(m['unique_organic_visitors_28d'], 1)
        self.assertEqual(m['business_all_attributions_28d'], 1)

    def test_same_did_new_timestamp_counts(self):
        self.ingest(valid_event())
        self.ingest(valid_event(ts_ms=T0_MS + 1000))
        self.assertEqual(self.metric()['successful_organic_calculations_28d'], 2)
        self.assertEqual(self.metric()['unique_organic_visitors_28d'], 1)

    def test_same_ts_other_did_counts(self):
        self.ingest(valid_event())
        self.ingest(valid_event(did='11111111-2222-4333-8444-555555555555'))
        self.assertEqual(self.metric()['successful_organic_calculations_28d'], 2)
        self.assertEqual(self.metric()['unique_organic_visitors_28d'], 2)

    # -- synthetic separation -----------------------------------------------
    def test_synthetic_separated_from_business_numerators(self):
        self.ingest(valid_event())
        self.ingest(valid_event(syn=True, seq=1))
        self.ingest(valid_event(syn=True, seq=2))
        m = self.metric()
        self.assertEqual(m['successful_organic_calculations_28d'], 1)
        self.assertEqual(m['business_all_attributions_28d'], 1)
        self.assertEqual(m['synthetic_28d'], 2)
        self.assertEqual(m['by_attribution_28d']['organic'], 1)
        # synthetic не влияет и на unique business-посетителей
        self.assertEqual(m['unique_organic_visitors_28d'], 1)

    def test_synthetic_dedupe_also_idempotent(self):
        self.ingest(valid_event(syn=True))
        self.ingest(valid_event(syn=True))
        self.assertEqual(self.metric()['synthetic_28d'], 1)

    # -- attribution ----------------------------------------------------------
    def test_only_organic_in_primary_numerator(self):
        self.ingest(valid_event())
        self.ingest(valid_event(attr='referral', ts_ms=T0_MS + 1000))
        self.ingest(valid_event(attr='direct', ts_ms=T0_MS + 2000))
        self.ingest(valid_event(attr='unknown', ts_ms=T0_MS + 3000))
        m = self.metric()
        self.assertEqual(m['successful_organic_calculations_28d'], 1)
        self.assertEqual(m['business_all_attributions_28d'], 4)
        self.assertEqual(
            m['by_attribution_28d'],
            {'organic': 1, 'referral': 1, 'direct': 1, 'unknown': 1})

    # -- 28d window -----------------------------------------------------------
    def test_window_28_days_boundary(self):
        self.ingest(valid_event(ts_ms=T0_MS - 27 * DAY_MS))       # в окне
        self.ingest(valid_event(ts_ms=T0_MS - 27 * DAY_MS + 1000,
                                did='22222222-2222-4222-8222-222222222222'))
        # событие за границей 28d-окна (но внутри retention) принимается,
        # однако в primary-числитель окна не попадает
        self.ingest(valid_event(ts_ms=T0_MS - 29 * DAY_MS,
                                did='33333333-3333-4333-8333-333333333333'))
        m = self.metric(now_ms=T0_MS)
        self.assertEqual(m['successful_organic_calculations_28d'], 2)
        self.assertEqual(len(m['days']), 28)
        self.assertEqual(m['days'][-1]['day'], '2026-09-20')
        self.assertEqual(m['days'][0]['day'], '2026-08-24')

    def test_window_days_param(self):
        self.ingest(valid_event(ts_ms=T0_MS - 10 * DAY_MS))
        self.ingest(valid_event(ts_ms=T0_MS - 20 * DAY_MS,
                                did='44444444-4444-4444-8444-444444444444'))
        m7 = self.metric(days=7)
        self.assertEqual(m7['successful_organic_calculations_28d'], 0)
        self.assertEqual(len(m7['days']), 7)
        m14 = self.metric(days=14)
        self.assertEqual(m14['successful_organic_calculations_28d'], 1)
        with self.assertRaises(core.EventRejected):
            self.metric(days=0)
        with self.assertRaises(core.EventRejected):
            self.metric(days=91)
        with self.assertRaises(core.EventRejected):
            self.metric(days='28')

    def test_day_bucket_uses_utc(self):
        # 23:30 UTC попадает в свой UTC-день независимо от локальной таймзоны
        day_start = T0_MS - (T0_MS % DAY_MS)
        late = day_start + (23 * 60 + 30) * 60 * 1000
        self.ingest(valid_event(ts_ms=late), now_ms=late)
        m = self.metric(days=1, now_ms=late)
        self.assertEqual(m['days'][-1]['organic'], 1)

    # -- retention maintenance ------------------------------------------------
    def test_maintenance_purges_old_dedupe_and_visitor_rows(self):
        # строки старше retention появляются напрямую в БД (ingest их бы
        # отклонил как too_old), maintenance выметает их при следующей записи
        cutoff_day = core.day_str_of(T0_MS - (core.RETENTION_DAYS + 1) * DAY_MS)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO seen_events(dedupe_key, day) VALUES('old-key', ?)",
            (cutoff_day,))
        conn.execute(
            "INSERT INTO did_days(day, did_hash, organic) VALUES('2020-01-01', 'x', 1)")
        conn.execute(
            "INSERT INTO did_days(day, did_hash, organic) VALUES(?, 'y', 1)",
            (cutoff_day,))
        conn.commit()
        conn.close()
        # следующая запись запускает lazy maintenance и выметает старые строки
        self.ingest(valid_event())
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM seen_events WHERE day < ?",
            (core.day_str_of(T0_MS - core.RETENTION_DAYS * DAY_MS),)
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM did_days WHERE day='2020-01-01'").fetchone()[0], 0)
        # свежие строки текущей записи сохранены
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM seen_events').fetchone()[0], 1)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM did_days').fetchone()[0], 1)
        conn.close()

    # -- empty storage ----------------------------------------------------------
    def test_empty_db_metric_shape(self):
        m = self.metric()
        self.assertEqual(m['metric'], 'successful_organic_calculations_28d')
        self.assertEqual(m['successful_organic_calculations_28d'], 0)
        self.assertEqual(m['unique_organic_visitors_28d'], 0)
        self.assertEqual(m['synthetic_28d'], 0)
        self.assertEqual(len(m['days']), 28)
        self.assertTrue(all(d['business'] == 0 for d in m['days']))


if __name__ == '__main__':
    unittest.main()
