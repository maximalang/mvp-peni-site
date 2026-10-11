# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import propose_rates_pr as P  # noqa: E402

REPO, BASE, HEAD, SHA = "o/r", "main", "ops/auto-refresh-key-rates-20261011", "a" * 40
PR_URL = "https://github.com/o/r/pull/77"


class FakeGh:
    """Имитирует gh по сценарию; фиксирует вызовы для проверки идемпотентности."""

    def __init__(self, list_out="[]", create=(0, PR_URL + "\n", ""),
                 view=None):
        self.list_out = list_out
        self.create = create
        self.view = view or {"number": 77, "url": PR_URL, "headRefName": HEAD,
                             "baseRefName": BASE, "headRefOid": SHA,
                             "state": "OPEN"}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            if isinstance(self.list_out, Exception):
                raise self.list_out
            return 0, self.list_out, ""
        if args[:2] == ["pr", "create"]:
            return self.create
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps(self.view), ""
        raise AssertionError("unexpected gh call %r" % (args,))

    def creates(self):
        return [c for c in self.calls if c[:2] == ["pr", "create"]]


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prh-")
        self.receipt = os.path.join(self.tmp, "receipt.json")

    def _propose(self, gh, head_sha=SHA):
        return P.propose(gh, repo=REPO, base=BASE, head=HEAD, head_sha=head_sha,
                         title="t", body="b", source_captured_at="2026-10-10T14-15-25Z",
                         source_max_date="2026-10-09", artifact_path="data/key-rates.json",
                         receipt_path=self.receipt)

    def test_existing_exact_pr_is_reused_without_create(self):
        gh = FakeGh(list_out=json.dumps([{"number": 77, "url": PR_URL, "headRefName": HEAD,
                                          "baseRefName": BASE, "headRefOid": SHA}]))
        rec = self._propose(gh)
        self.assertEqual(rec["outcome"], "EXISTING")
        self.assertEqual(rec["pr_number"], 77)
        self.assertEqual(gh.creates(), [], "idempotency: no duplicate PR")

    def test_ambiguous_open_prs_fail_closed(self):
        two = json.dumps([{"number": 1, "url": "u1", "headRefName": HEAD, "baseRefName": BASE},
                          {"number": 2, "url": "u2", "headRefName": HEAD, "baseRefName": BASE}])
        gh = FakeGh(list_out=two)
        with self.assertRaises(P.HandoffError):
            self._propose(gh)
        self.assertEqual(gh.creates(), [])

    def test_create_then_readback_exact_head(self):
        gh = FakeGh(list_out="[]")
        rec = self._propose(gh)
        self.assertEqual(rec["outcome"], "CREATED")
        self.assertEqual(len(gh.creates()), 1)
        self.assertEqual(rec["pr_url"], PR_URL)

    def test_actions_permission_denied_writes_persistent_receipt(self):
        gh = FakeGh(list_out="[]", create=(1, "",
                    "GraphQL: GitHub Actions is not permitted to create or approve pull requests (createPullRequest)"))
        rec = self._propose(gh)
        self.assertEqual(rec["outcome"], "NEEDS_REVIEW")
        self.assertIsNone(rec["pr_url"], "denial must not be relabelled as delivered")
        on_disk = json.loads(Path(self.receipt).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["outcome"], "NEEDS_REVIEW")
        self.assertEqual(on_disk["source_max_date"], "2026-10-09")

    def test_unexpected_api_failure_stays_failure(self):
        gh = FakeGh(list_out="[]", create=(1, "", "HTTP 502: bad gateway"))
        with self.assertRaises(P.HandoffError):
            self._propose(gh)

    def test_head_drift_fails_closed(self):
        gh = FakeGh(list_out="[]", view=dict(
            number=77, url=PR_URL, headRefName=HEAD, baseRefName=BASE,
            headRefOid="b" * 40, state="OPEN"))
        with self.assertRaises(P.HandoffError) as cm:
            self._propose(gh)
        self.assertIn("head drift", str(cm.exception))

    def test_receipt_excludes_secrets_and_is_pii_free(self):
        os.environ["GH_TOKEN"] = "ghp_SECRETVALUE123"
        try:
            gh = FakeGh(list_out="[]", create=(1, "",
                        "GitHub Actions is not permitted to create or approve pull requests"))
            self._propose(gh)
            text = Path(self.receipt).read_text(encoding="utf-8")
            self.assertNotIn("ghp_", text)
            self.assertNotIn("SECRETVALUE", text)
            rec = json.loads(text)
            self.assertEqual(set(rec), set(P.RECEIPT_KEYS))
        finally:
            del os.environ["GH_TOKEN"]

    def test_main_failure_writes_failed_receipt_and_exit_1(self):
        data = Path(self.tmp) / "k.json"
        data.write_text(json.dumps({"captured_at": "2026-10-10T14-15-25Z",
                                    "rows": [{"d": "2026-10-09", "p": "14.00"}]}),
                        encoding="utf-8")
        orig = P.run_gh
        P.run_gh = FakeGh(list_out="[]", create=(1, "", "HTTP 502"))
        try:
            rc = P.main(["--repo", REPO, "--head", HEAD, "--head-sha", SHA,
                         "--title", "t", "--body", "b", "--data", str(data),
                         "--artifact", str(data), "--receipt", self.receipt])
        finally:
            P.run_gh = orig
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(Path(self.receipt).read_text(encoding="utf-8"))["outcome"],
                         "FAILED")


if __name__ == "__main__":
    unittest.main()
