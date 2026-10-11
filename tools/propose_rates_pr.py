# -*- coding: utf-8 -*-
"""Идемпотентный operator-handoff для PR с обновлением ставок ЦБ.

Использует уже выданную аутентификацию gh (GH_TOKEN / gh auth), без новых
прав, токенов и аккаунтов. Порядок:
1. Ищет ровно открытый PR с точным head/base. Найден один -> возвращает его.
   Найдено несколько -> fail closed (неоднозначность).
2. Не найден -> создаёт ровно один PR и делает readback: number/URL/head/base/
   headRefOid. Любое расхождение с ожидаемым head SHA -> fail closed.
3. Если GitHub Actions не имеет права создавать PR (известный отказ
   "GitHub Actions is not permitted to create or approve pull requests") ->
   пишет устойчивую review-квитанцию (outcome=NEEDS_REVIEW) и выходит с кодом 0.
   Публикация PR НЕ заявляется как выполненная.
4. Любая иная ошибка API -> выход 1, квитанция с outcome=FAILED.

В квитанции нет токенов, переменных окружения и персональных данных:
только репозиторий, ветки, SHA, дата снимка и путь к артефакту.

Коды выхода: 0 = PR существует/создан ИЛИ зафиксирована NEEDS_REVIEW
квитанция по отказу прав | 1 = ошибка, drift или неоднозначность.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ACTIONS_PR_DENIED = "not permitted to create or approve pull requests"
RECEIPT_KEYS = (
    "outcome", "reason", "repo", "base", "head", "head_sha",
    "source_captured_at", "source_max_date", "artifact_path",
    "pr_number", "pr_url", "generated_at_utc",
)


def run_gh(args):
    """Запускает gh и возвращает (rc, stdout, stderr). Вынесено для тестов."""
    proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


class HandoffError(Exception):
    """Fail-closed ошибка: drift, неоднозначность, неожиданный сбой API."""


def find_open_prs(gh, repo, base, head):
    rc, out, err = gh(["pr", "list", "--repo", repo, "--state", "open",
                       "--base", base, "--head", head, "--limit", "100",
                       "--json", "number,url,headRefName,baseRefName,headRefOid"])
    if rc != 0:
        raise HandoffError("pr list failed rc=%d: %s" % (rc, err.strip()[:300]))
    prs = json.loads(out or "[]")
    return [p for p in prs
            if p.get("headRefName") == head and p.get("baseRefName") == base]


def readback(gh, repo, pr_url):
    rc, out, err = gh(["pr", "view", pr_url, "--repo", repo, "--json",
                       "number,url,headRefName,baseRefName,headRefOid,state"])
    if rc != 0:
        raise HandoffError("pr view failed rc=%d: %s" % (rc, err.strip()[:300]))
    return json.loads(out)


def build_receipt(**kw):
    rec = {k: kw.get(k) for k in RECEIPT_KEYS}
    rec["generated_at_utc"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H-%M-%SZ")
    return rec


def write_receipt(path, rec):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")


def propose(gh, *, repo, base, head, head_sha, title, body, source_captured_at,
            source_max_date, artifact_path, receipt_path):
    """Возвращает квитанцию. Бросает HandoffError для fail-closed случаев."""
    common = dict(repo=repo, base=base, head=head, head_sha=head_sha,
                  source_captured_at=source_captured_at,
                  source_max_date=source_max_date, artifact_path=artifact_path)

    existing = find_open_prs(gh, repo, base, head)
    if len(existing) > 1:
        raise HandoffError("ambiguous: %d open PRs for %s->%s" %
                           (len(existing), head, base))
    if len(existing) == 1:
        pr = existing[0]
        rb = readback(gh, repo, pr["url"])
        _check_readback(rb, base, head, head_sha)
        rec = build_receipt(outcome="EXISTING", reason="exact_open_pr",
                            pr_number=rb["number"], pr_url=rb["url"], **common)
        write_receipt(receipt_path, rec)
        return rec

    rc, out, err = gh(["pr", "create", "--repo", repo, "--base", base,
                       "--head", head, "--title", title, "--body", body])
    if rc != 0:
        if ACTIONS_PR_DENIED in err:
            rec = build_receipt(outcome="NEEDS_REVIEW",
                                reason="actions_pr_permission_denied",
                                pr_number=None, pr_url=None, **common)
            write_receipt(receipt_path, rec)
            return rec
        raise HandoffError("pr create failed rc=%d: %s" % (rc, err.strip()[:300]))

    pr_url = out.strip().splitlines()[-1].strip()
    rb = readback(gh, repo, pr_url)
    _check_readback(rb, base, head, head_sha)
    rec = build_receipt(outcome="CREATED", reason="created_and_read_back",
                        pr_number=rb["number"], pr_url=rb["url"], **common)
    write_receipt(receipt_path, rec)
    return rec


def _check_readback(rb, base, head, head_sha):
    if rb.get("headRefName") != head or rb.get("baseRefName") != base:
        raise HandoffError("readback branch mismatch: %s->%s" %
                           (rb.get("headRefName"), rb.get("baseRefName")))
    if head_sha and rb.get("headRefOid") != head_sha:
        raise HandoffError("head drift: expected %s got %s" %
                           (head_sha, rb.get("headRefOid")))


def _source_meta(data_path):
    doc = json.loads(Path(data_path).read_text(encoding="utf-8"))
    rows = doc.get("rows") or []
    max_date = max((r["d"] for r in rows), default=None)
    return doc.get("captured_at"), max_date


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--head", required=True)
    ap.add_argument("--head-sha", default="",
                    help="ожидаемый SHA head-ветки (drift-guard)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--data", default="data/key-rates.json")
    ap.add_argument("--artifact", default="data/key-rates.json")
    ap.add_argument("--receipt", required=True)
    a = ap.parse_args(argv)

    captured, max_date = _source_meta(a.data)
    try:
        rec = propose(run_gh, repo=a.repo, base=a.base, head=a.head,
                      head_sha=a.head_sha, title=a.title, body=a.body,
                      source_captured_at=captured, source_max_date=max_date,
                      artifact_path=a.artifact, receipt_path=a.receipt)
    except HandoffError as exc:
        rec = build_receipt(outcome="FAILED", reason=str(exc)[:300], repo=a.repo,
                            base=a.base, head=a.head, head_sha=a.head_sha,
                            source_captured_at=captured, source_max_date=max_date,
                            artifact_path=a.artifact, pr_number=None, pr_url=None)
        write_receipt(a.receipt, rec)
        print("PR_HANDOFF_FAILED %s" % exc, file=sys.stderr)
        return 1
    print("PR_HANDOFF %s %s" % (rec["outcome"], rec.get("pr_url") or rec["reason"]))
    if rec["outcome"] == "NEEDS_REVIEW":
        print("::warning::PR не создан: Actions не имеет права создавать PR. "
              "Квитанция: %s" % a.receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
