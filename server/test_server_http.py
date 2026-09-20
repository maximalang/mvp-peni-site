#!/usr/bin/env python3
"""HTTP round-trip тесты сервера (server/serve.py): реальный сокет, реальные
заголовки. Запуск как и unit-тесты: python3 -m unittest discover -s server.

Проверяют контракт endpoint'ов:
- POST /api/event: 200 + deduped-флаг, идемпотентность повторного beacon;
- allowlist-отказ: 400 unknown_field, тело не сохранено;
- большой/битый body: 413/400;
- GET /api/metric: агрегаты и per-day buckets; days-параметр и его валидация;
- GET /api/healthz: 200;
- метод/путь: 405/404;
- приват-границы transport-слоя: нет CORS-заголовков, Content-Length есть,
  no-store, IP клиента не логируется в stderr.
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import measure_core as core  # noqa: E402
import serve  # noqa: E402

# сервер валидирует ts относительно РЕАЛЬНОГО now, поэтому T0 берём от него
T0_MS = core.utc_now_ms()


def iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def valid_event(ts_ms=T0_MS, syn=False, did=None, seq=0):
    return {
        'ts': iso(ts_ms + seq),
        'v': core.MEASURE_VERSION,
        'calc': 'tax_penalty',
        'attr': 'organic',
        'ok': True,
        'did': did or '0f1e2d3c-4b5a-6789-8abc-def012345678',
        'syn': syn,
    }


class HttpServerTest(unittest.TestCase):
    """Каждый тест получает свежий сервер и пустую БД: счётчики не зависят
    от порядка выполнения методов."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='measure-http-test-')
        self.db = os.path.join(self.tmp, 'measure.sqlite3')
        core.init_db(self.db)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.MeasureHandler)
        self.server.db_path = self.db
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.port, path)

    def post(self, path, body, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            self.url(path), data=data, method='POST',
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8')), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode('utf-8')), dict(err.headers)

    def get(self, path):
        req = urllib.request.Request(self.url(path), method='GET')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8')), dict(resp.headers)
        except urllib.error.HTTPError as err:
            try:
                body = json.loads(err.read().decode('utf-8'))
            except ValueError:
                body = None
            return err.code, body, dict(err.headers)

    # -- POST /api/event ------------------------------------------------------
    def test_event_ingest_and_dedupe_roundtrip(self):
        status, body, hdrs = self.post('/api/event', valid_event())
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertFalse(body['deduped'])
        # повторный beacon идемпотентен
        status2, body2, _ = self.post('/api/event', valid_event())
        self.assertEqual(status2, 200)
        self.assertTrue(body2['deduped'])
        m_status, m, _ = self.get('/api/metric')
        self.assertEqual(m_status, 200)
        self.assertEqual(m['successful_organic_calculations_28d'], 1)
        self.assertEqual(hdrs.get('Cache-Control'), 'no-store')
        self.assertEqual(hdrs.get('X-Content-Type-Options'), 'nosniff')

    def test_pii_injection_400_and_not_stored(self):
        payload = valid_event(seq=100)
        payload.update({'arrears_rub': '50000', 'phone': '+79991234567'})
        status, body, _ = self.post('/api/event', payload)
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'unknown_field')
        _, m, _ = self.get('/api/metric')
        # отклонённое событие ничего не записало
        self.assertEqual(m['business_all_attributions_28d'], 0)

    def test_bad_json_and_bad_field(self):
        status, body, _ = self.post('/api/event', None, raw=b'{not json')
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'bad_json')
        status, body, _ = self.post('/api/event', valid_event(seq=101) | {'ok': False})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'not_success')

    def test_oversized_body_413(self):
        big = json.dumps({'pad': 'x' * (serve.MAX_BODY_BYTES + 10)}).encode('utf-8')
        status, body, _ = self.post('/api/event', None, raw=big)
        self.assertEqual(status, 413)
        self.assertEqual(body['error']['code'], 'too_large')

    def test_empty_body_400(self):
        req = urllib.request.Request(
            self.url('/api/event'), data=b'', method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            status, body = err.code, json.loads(err.read())
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'empty_body')

    # -- GET /api/metric -------------------------------------------------------
    def test_metric_days_param_and_validation(self):
        self.post('/api/event', valid_event(seq=200))
        status, m, _ = self.get('/api/metric?days=1')
        self.assertEqual(status, 200)
        self.assertEqual(m['window_days'], 1)
        self.assertEqual(len(m['days']), 1)
        status, body, _ = self.get('/api/metric?days=abc')
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'bad_days')
        status, body, _ = self.get('/api/metric?days=91')
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], 'bad_days')

    def test_metric_fields_for_baseline_card(self):
        # контракт для t_0af547dc: поля, которые читает baseline-цикл
        status, m, _ = self.get('/api/metric')
        self.assertEqual(status, 200)
        for key in ('metric', 'version', 'window_days',
                    'successful_organic_calculations_28d',
                    'unique_organic_visitors_28d',
                    'business_all_attributions_28d',
                    'by_attribution_28d', 'synthetic_28d', 'days'):
            self.assertIn(key, m)
        self.assertEqual(m['metric'], 'successful_organic_calculations_28d')

    # -- routing / methods -----------------------------------------------------
    def test_healthz_and_routing(self):
        status, body, _ = self.get('/api/healthz')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        status, _, _ = self.get('/api/nope')
        self.assertEqual(status, 404)
        status, _, hdrs = self.post('/api/metric', valid_event(seq=201))
        self.assertEqual(status, 405)
        self.assertEqual(hdrs.get('Allow'), 'GET')
        status, body, _ = self.get('/api/event')
        self.assertEqual(status, 405)
        self.assertEqual(body['error']['code'], 'method_not_allowed')

    def test_options_405_no_cors_headers(self):
        req = urllib.request.Request(self.url('/api/event'), method='OPTIONS')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, hdrs = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as err:
            status, hdrs = err.code, dict(err.headers)
        self.assertEqual(status, 405)
        # same-origin by design: никаких CORS-заголовков ни на одном ответе
        self.assertNotIn('Access-Control-Allow-Origin', hdrs)
        _, _, get_hdrs = self.get('/api/metric')
        self.assertNotIn('Access-Control-Allow-Origin', get_hdrs)

    def test_client_ip_not_logged(self):
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.post('/api/event', valid_event(seq=300))
            self.get('/api/metric')
        logs = captured.getvalue()
        self.assertNotIn('127.0.0.1', logs)
        self.assertNotIn('Client', logs)

    def test_raw_query_string_never_logged(self):
        # QA F-1: чувствительные значения вне allowlist в query string
        # не должны попадать в stderr-журнал; маршрут нормализуется.
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.get('/api/metric?days=7&ssn=123-45-6789&phone=***')
            self.get('/api/event?token=***')
            self.get('/api/nope?password=***')
            self.post('/api/nope?apikey=***', valid_event(seq=301))
        logs = captured.getvalue()
        for secret in ('123-45-6789', '+799****4567', 'abc123secret',
                       'hunter2', 'sekret-key-xyz'):
            self.assertNotIn(secret, logs)
        # нормализованные allowlisted-маршруты присутствуют без query
        self.assertIn('"GET /api/metric" 200', logs)
        self.assertIn('"GET /api/event" 405', logs)
        # вне allowlist — нейтральный placeholder
        self.assertIn(f'"GET {serve._REDACTED_ROUTE}" 404', logs)
        self.assertIn(f'"POST {serve._REDACTED_ROUTE}" 404', logs)

    def test_log_control_chars_escaped_like_cpython(self):
        # QA F-1: control characters экранируются как в CPython log_message
        handler = object.__new__(serve.MeasureHandler)
        captured = io.StringIO()
        with redirect_stderr(captured):
            serve.MeasureHandler.log_message(
                handler, '"GET %s" 200 -', '/api/metric?x=\x00\x01y\x7f')
        logs = captured.getvalue()
        self.assertNotIn('\x00', logs)
        self.assertNotIn('\x01', logs)
        self.assertNotIn('\x7f', logs)
        self.assertIn(r'\x00\x01y\x7f', logs)

    def test_log_request_uses_method_and_route_only(self):
        # QA F-1: requestline (с raw query) не используется в access-логе
        status, _, _ = self.get('/api/healthz?trace=deadbeef')
        self.assertEqual(status, 200)
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.get('/api/healthz?trace=deadbeef')
        logs = captured.getvalue()
        self.assertNotIn('deadbeef', logs)
        self.assertIn('"GET /api/healthz" 200', logs)

    def test_malformed_requestline_not_logged_and_400(self):
        # QA F-1 (error-path): битый request-line CPython раньше включал
        # verbatim в message send_error (log_error → stderr). Теперь raw
        # текст в журнал не попадает, а клиент всё равно получает 400 —
        # без падения логгера на не установленном self.path.
        captured = io.StringIO()
        response = b''
        with redirect_stderr(captured):
            with socket.create_connection(
                    ('127.0.0.1', self.port), timeout=10) as sock:
                sock.sendall(b'/api/healthz?marker_b=2\r\n\r\n')
                sock.settimeout(5)
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                except socket.timeout:
                    pass
        logs = captured.getvalue()
        self.assertNotIn('marker_b', logs)
        self.assertNotIn('/api/healthz?marker_b', logs)
        status_line = response.split(b'\r\n', 1)[0].decode('utf-8', 'replace')
        self.assertIn('400', status_line)
        # сервер жив: следующий корректный запрос обслуживается
        status, _, _ = self.get('/api/healthz')
        self.assertEqual(status, 200)


if __name__ == '__main__':
    unittest.main()
