#!/usr/bin/env python3
"""Thin HTTP layer for the first-party measurement endpoint (stdlib only).

Endpoints (same-origin behind the existing reverse proxy):
  POST /api/event    - ingest one allowlisted event (idempotent, fail-closed)
  GET  /api/metric   - aggregated counters: successful_organic_calculations_28d,
                       unique_organic_visitors_28d, per-day buckets
  GET  /api/healthz  - liveness probe

Privacy/hardening notes:
- no CORS headers on purpose: the site is same-origin through the proxy, and
  without ACAO browsers block cross-origin reads; OPTIONS is answered 405;
- request logs contain method + normalized allowlisted route + status only:
  the raw query string is never logged (values outside the allowlist must not
  leak into stderr), control characters are escaped as in CPython, and client
  IPs are never written by this service (the event store has no IP/UA columns);
- request body capped at MAX_BODY_BYTES (413), malformed JSON is 400;
- every response is JSON with explicit Content-Length (keep-alive safe).
Run example (see README.md): python3 serve.py --bind 127.0.0.1 --port 8101
"""
import argparse
import json
import os
import signal
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import measure_core as core

MAX_BODY_BYTES = 4096

# Единственные маршруты, которые могут появиться в access-логе. Всё прочее
# (включая любой raw query string) нормализуется в нейтральный placeholder:
# чувствительные значения вне allowlist не должны попадать в журнал (QA F-1).
_LOGGED_ROUTES = frozenset(("/api/event", "/api/metric", "/api/healthz"))
_REDACTED_ROUTE = "/api/__redacted__"


class MeasureHandler(BaseHTTPRequestHandler):
    server_version = "peni-measure/1"
    protocol_version = "HTTP/1.1"

    def _safe_route(self):
        """Нормализованный маршрут для лога: только allowlist, без query string.

        Любой путь вне _LOGGED_ROUTES (включая raw query с чувствительными
        значениями) заменяется нейтральным placeholder — приват-граница журнала.
        """
        # При сбое parse_request self.path ещё не установлен (CPython шлёт 400
        # через send_error до его присвоения) — берём через getattr, иначе
        # log_request падает с AttributeError и клиент не получает ответ.
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        return path if path in _LOGGED_ROUTES else _REDACTED_ROUTE

    def log_request(self, code="-", size="-"):
        # QA F-1: не логируем self.requestline (в нём raw query string).
        # Только метод + нормализованный allowlisted route + status/size.
        if isinstance(code, HTTPStatus):
            code = code.value
        method = self.command or "-"
        self.log_message('"%s %s" %s %s',
                         method, self._safe_route(), str(code), str(size))

    def log_message(self, fmt, *args):
        # Client address deliberately omitted (privacy boundary, not a bug).
        # Control characters are escaped exactly как в CPython log_message.
        message = (fmt % args).translate(self._control_char_table)
        sys.stderr.write("[measure] " + message + "\n")

    def send_error(self, code, message=None, explain=None):
        # QA F-1 (error-path): вызовы CPython send_error передают в message
        # raw requestline ("Bad request syntax (%r)" % requestline), который
        # через log_error попадает в журнал и в тело ответа. Отбрасываем
        # message/explain: код + штатный shortmsg достаточны для диагностики,
        # приват-граница журнала закрыта и для битых запросов.
        super().send_error(code)

    # -- helpers ----------------------------------------------------------
    def _send_json(self, status, obj, allow_header=None):
        body = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow_header:
            self.send_header("Allow", allow_header)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # fire-and-forget клиент мог уже закрыть соединение

    def _not_allowed(self, allow):
        self._send_json(
            405, {"error": {"code": "method_not_allowed"}}, allow_header=allow)

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):
        self._not_allowed("GET, POST")

    def do_GET(self):
        split = self.path.split("?", 1)
        path, query = split[0], (split[1] if len(split) > 1 else "")
        if path == "/api/healthz":
            self._send_json(200, {"ok": True})
            return
        if path == "/api/metric":
            days = core.WINDOW_DAYS_DEFAULT
            if query:
                raw = urllib.parse.parse_qs(query).get("days", [None])[0]
                if raw is not None:
                    try:
                        days = int(raw)
                    except ValueError:
                        self._send_json(400, {"error": {"code": "bad_days"}})
                        return
            try:
                data = core.metric(self.server.db_path, window_days=days)
            except core.EventRejected as exc:
                self._send_json(400, {"error": {"code": exc.code}})
            except Exception:
                self.log_message("metric query failed")
                self._send_json(500, {"error": {"code": "internal"}})
            else:
                self._send_json(200, data)
            return
        if path == "/api/event":
            self._not_allowed("POST")
            return
        self._send_json(404, {"error": {"code": "not_found"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/event":
            if path in ("/api/metric", "/api/healthz"):
                self._not_allowed("GET")
            else:
                self._send_json(404, {"error": {"code": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(400, {"error": {"code": "bad_request"}})
            return
        if length <= 0:
            self._send_json(400, {"error": {"code": "empty_body"}})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"code": "too_large"}})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"code": "bad_json"}})
            return
        try:
            result = core.ingest_event(self.server.db_path, payload)
        except core.EventRejected as exc:
            self._send_json(400, {"error": {"code": exc.code}})
        except Exception:
            self.log_message("ingest failed")
            self._send_json(500, {"error": {"code": "internal"}})
        else:
            self._send_json(200, {"ok": True, "deduped": result.get("deduped", False)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="first-party measurement endpoint")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument(
        "--db",
        default=os.environ.get("MEASURE_DB", "data/measure.sqlite3"),
        help="sqlite file path (created on start)")
    args = parser.parse_args(argv)

    core.init_db(args.db)
    server = ThreadingHTTPServer((args.bind, args.port), MeasureHandler)
    server.db_path = args.db

    def _graceful(_sig, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    sys.stderr.write(
        "[measure] listening on %s:%d db=%s\n" % (args.bind, args.port, args.db))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
