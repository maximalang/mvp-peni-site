#!/usr/bin/env python3
"""Startup-проба заявленного production-runtime (Python 3.6.9, только stdlib).

Запускает реальный server/serve.py subprocess'ом на ТЕКУЩЕМ интерпретаторе и
проверяет контракт endpoint'ов end-to-end:
  1. GET  /api/healthz            -> 200 {"ok": true};
  2. POST /api/event (валидное organic-событие) -> 200, deduped=false;
  3. повторный POST того же события -> 200, deduped=true (идемпотентность);
  4. POST c PII-полем вне allowlist -> 400 unknown_field (fail-closed);
  5. GET  /api/metric             -> 200, organic=1, unique=1, synthetic=0;
  6. access-лог без клиентского IP (privacy-граница журнала);
  7. SIGTERM -> graceful shutdown, exit code 0.

Запуск:  python3 tools/py36_startup_probe.py [--port N]
Exit 0 = все проверки PASS; 1 = хотя бы одна FAIL.

Назначен для запуска ВНУТРИ контейнера python:3.6.9 (CI job py36-runtime)
и для ручной проверки на prod VDS. На Windows проверка 7 вырождается
(TerminateProcess вместо SIGTERM) - проба предназначена для POSIX.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SERVER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server')


def now_iso_z():
    """Формат ts клиента js/measure.js: new Date().toISOString()."""
    ms = int(time.time() * 1000)
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (ms % 1000)


def valid_event():
    return {
        'ts': now_iso_z(),
        'v': 1,
        'calc': 'tax_penalty',
        'attr': 'organic',
        'ok': True,
        'did': '0f1e2d3c-4b5a-6789-8abc-def012345678',
        'syn': False,
    }


def request(url, body=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        method='POST' if data is not None else 'GET',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode('utf-8'))


def main(argv):
    port = 8199
    if '--port' in argv:
        port = int(argv[argv.index('--port') + 1])
    print('probe runtime: python %s, sqlite %s' % (
        sys.version.split()[0], sqlite3.sqlite_version))
    tmp = tempfile.mkdtemp(prefix='py36-probe-')
    db = os.path.join(tmp, 'measure.sqlite3')
    err_path = os.path.join(tmp, 'server.stderr.log')
    err_file = open(err_path, 'wb')
    proc = subprocess.Popen(
        [sys.executable, 'serve.py', '--bind', '127.0.0.1',
         '--port', str(port), '--db', db],
        cwd=SERVER_DIR, stdout=subprocess.DEVNULL, stderr=err_file)

    checks = []

    def check(name, cond):
        checks.append(bool(cond))
        print('%s %s' % ('PASS' if cond else 'FAIL', name))

    base = 'http://127.0.0.1:%d' % port
    try:
        status, body = None, None
        last_err = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                print('server exited early, code %s' % proc.returncode)
                break
            try:
                status, body = request(base + '/api/healthz')
                break
            except Exception as exc:
                last_err = exc
                time.sleep(0.25)
        if status is None and last_err is not None:
            print('healthz poll error: %r' % (last_err,))
        elif status is not None and status != 200:
            print('healthz returned %s: %r' % (status, body))
        check('healthz_200', status == 200 and body == {'ok': True})
        if status == 200:
            ev = valid_event()
            s1, b1 = request(base + '/api/event', ev)
            check('event_ingest_200',
                  s1 == 200 and b1.get('ok') is True
                  and b1.get('deduped') is False)
            s2, b2 = request(base + '/api/event', ev)
            check('event_idempotent_dedupe',
                  s2 == 200 and b2.get('deduped') is True)
            pii = valid_event()
            pii['phone'] = '+799****4567'
            s3, b3 = request(base + '/api/event', pii)
            check('pii_rejected_400_unknown_field',
                  s3 == 400 and b3.get('error', {}).get('code') == 'unknown_field')
            s4, m = request(base + '/api/metric')
            check('metric_readback_counts',
                  s4 == 200
                  and m.get('successful_organic_calculations_28d') == 1
                  and m.get('unique_organic_visitors_28d') == 1
                  and m.get('synthetic_28d') == 0)
    finally:
        if proc.poll() is None:
            proc.terminate()  # POSIX: SIGTERM -> graceful shutdown serve.py
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        else:
            rc = proc.returncode
        err_file.close()
    with open(err_path, 'r', encoding='utf-8', errors='replace') as fh:
        logs = fh.read()
    access_lines = [ln for ln in logs.splitlines()
                    if '"GET' in ln or '"POST' in ln]
    check('access_log_present', len(access_lines) >= 5)
    check('access_log_has_no_client_ip',
          not any('127.0.0.1' in ln for ln in access_lines))
    check('sigterm_graceful_exit_0', rc == 0)

    failed = checks.count(False)
    if failed:
        print('--- server stderr (diagnostics) ---')
        print(logs.rstrip())
        print('--- end of server stderr ---')
    print('probe finished: %d checks, %d failed' % (len(checks), failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
