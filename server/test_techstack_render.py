"""Adversarial regression tests for the techstack renderer (PR9 hardening).

Fail-closed contract under test:
  * ``jsonld()`` serializes JSON for the HTML ``<script>`` context: no raw
    ``<``, ``>``, ``&``, U+2028 or U+2029 may appear in the payload, while the
    text stays valid JSON with identical semantic content.
  * ``safe_href()`` allowlists http/https schemes only and rejects malformed or
    dangerous URLs (javascript:, data:, vbscript:, embedded control chars,
    scheme-relative and empty-netloc forms) by returning ``None``.
  * ``render_domain_page()`` renders a dangerous ``url`` as escaped plain text
    without an anchor (documented fail-closed behavior), never as an href.

Synthetic records only; existing fixtures and catalog data are untouched.
Discovered by: python -B -m unittest discover -s server -p "test_*.py".
"""
import importlib.util
import json
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "techstack_generator_under_test", _ROOT / "tools" / "generate_techstack_pages.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

PAYLOAD = '</script><script>alert(1)</script><script>alert(2)</script>'
PAYLOAD_MIXED = '</ScRiPt><ScRiPt>alert(3)</ScRiPt>'
EVIL_URL = 'javascript:alert(document.domain)'


def make_rec(**overrides):
    rec = {
        "domain": "example-evil.test",
        "url": EVIL_URL,
        "http_status": 200,
        "scanned_at": "2026-09-29T12:00:00+00:00",
        "tool_count": 1,
        "tools": [{"name": PAYLOAD, "categories": ["xss"], "confidence": 90,
                   "version": "1.0"}],
    }
    rec.update(overrides)
    return rec


def script_open_tags(page):
    return re.findall(r"<script\b([^>]*)>", page, re.IGNORECASE)


def executable_script_count(page):
    count = 0
    for attrs in script_open_tags(page):
        m = re.search(r"type\s*=\s*[\"']?([^\"'\s>]+)", attrs, re.IGNORECASE)
        if not m or m.group(1).lower() != "application/ld+json":
            count += 1
    return count


def jsonld_inner(block):
    m = re.search(
        r'<script type="application/ld\+json">\n(.*)\n</script>\n', block, re.DOTALL
    )
    assert m is not None, "JSON-LD wrapper not found"
    return m.group(1)


class JsonLdScriptContextTest(unittest.TestCase):
    def test_script_tag_payload_cannot_break_out(self):
        block = gen.jsonld({"name": PAYLOAD})
        self.assertEqual(len(script_open_tags(block)), 1,
                         "jsonld() must emit exactly one <script> open tag")
        self.assertEqual(len(re.findall(r"</script\s*>", block, re.IGNORECASE)), 1,
                         "jsonld() must emit exactly one </script> close tag")
        inner = jsonld_inner(block)
        self.assertNotIn("<", inner)
        self.assertNotIn(">", inner)
        parsed = json.loads(inner)
        self.assertEqual(parsed, {"name": PAYLOAD},
                         "semantic content must survive serialization")

    def test_mixed_case_closing_tag_variant(self):
        block = gen.jsonld({"name": PAYLOAD_MIXED})
        self.assertEqual(len(script_open_tags(block)), 1)
        inner = jsonld_inner(block)
        self.assertNotIn("<", inner.lower())
        self.assertEqual(json.loads(inner), {"name": PAYLOAD_MIXED})

    def test_ampersand_and_line_separators_are_escaped(self):
        obj = {"a": "x&y", "b": "l\u2028r\u2029", "c": "</script>"}
        inner = jsonld_inner(gen.jsonld(obj))
        for ch in ("&", "<", ">", "\u2028", "\u2029"):
            self.assertNotIn(ch, inner, f"raw {ch!r} must not appear in JSON-LD")
        self.assertEqual(json.loads(inner), obj)


class SafeHrefTest(unittest.TestCase):
    SAFE = [
        "https://example.test/",
        "https://example.test/page?a=1&b=2#frag",
        "http://example.test/x",
        "HTTPS://EXAMPLE.TEST/Upper",
    ]
    UNSAFE = [
        EVIL_URL,
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHN2Zy9vbmxvYWQ9YWxlcnQoMSk+",
        "vbscript:msgbox(1)",
        "java\tscript:alert(1)",
        "jav\nascript:alert(1)",
        " javascript:alert(1)",
        "//evil.test/x",
        "file:///etc/passwd",
        "https://",
        "https:",
        "",
        "   ",
    ]

    def test_allows_plain_http_and_https(self):
        for url in self.SAFE:
            self.assertIsNotNone(gen.safe_href(url), f"must allow {url!r}")

    def test_rejects_dangerous_and_malformed(self):
        for url in self.UNSAFE:
            self.assertIsNone(gen.safe_href(url), f"must reject {url!r}")


class RenderDomainPageFailClosedTest(unittest.TestCase):
    def test_javascript_url_renders_as_plain_text_not_link(self):
        page = gen.render_domain_page(make_rec(), "https://base.test")
        self.assertEqual(executable_script_count(page), 0,
                         "adversarial baseline was 2 extra executable scripts")
        self.assertEqual(len(script_open_tags(page)), 1)
        for href in re.findall(r'href="([^"]*)"', page):
            self.assertFalse(re.match(r"(?i)(javascript|data|vbscript):", href),
                             f"unsafe href escaped rendering: {href!r}")
        # Fail-closed disclosure: visible escaped text, no anchor around it.
        self.assertIn("javascript:alert(document.domain)", page)
        self.assertNotIn('<a rel="nofollow noopener" href="javascript:', page)

    def test_benign_https_url_keeps_link(self):
        rec = make_rec(domain="example-benign.test",
                       url="https://example-benign.test/?a=1&b=2")
        page = gen.render_domain_page(rec, "https://base.test")
        self.assertIn('<a rel="nofollow noopener" '
                      'href="https://example-benign.test/?a=1&amp;b=2">', page)

    def test_missing_url_fallback_still_links(self):
        rec = make_rec(domain="example-fallback.test")
        rec.pop("url")
        page = gen.render_domain_page(rec, "https://base.test")
        self.assertIn('href="https://example-fallback.test"', page)

    def test_page_jsonld_roundtrip_under_payload(self):
        page = gen.render_domain_page(make_rec(), "https://base.test")
        block = re.search(r'<script type="application/ld\+json">\n.*?\n</script>\n',
                          page, re.DOTALL).group(0)
        inner = jsonld_inner(block)
        parsed = json.loads(inner)
        names = [x["name"] for x in parsed["@graph"][1]["itemListElement"]]
        self.assertEqual(names, [PAYLOAD])


if __name__ == "__main__":
    unittest.main()
