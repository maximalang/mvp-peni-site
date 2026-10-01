// Adversarial regression tests for PR9 render hardening (fail-closed JSON-LD +
// outbound href). Mirrors the night-audit probe (2026-10-01): a synthetic tool
// name containing a closing script tag must not create extra executable script
// elements (baseline 2 -> 0), and a synthetic javascript: URL must not reach an
// href (baseline 1 -> 0). Synthetic records only; the checked-in fixture file
// is never modified. Runs the generator in a temp workspace, no network.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const GENERATOR = join(ROOT, 'tools', 'generate_techstack_pages.py');
const PYTHON = process.env.PYTHON_BIN || 'python';

// Same shape as the audit reproducer: one payload with TWO injected scripts.
const PAYLOAD = '</script><script>alert(1)</script><script>alert(2)</script>';
const PAYLOAD_MIXED = '</ScRiPt><ScRiPt>alert(3)</ScRiPt>';
const EVIL_URL = 'javascript:alert(document.domain)';
const BENIGN_URL = 'https://example-benign.test/?a=1&b=2';

function adversarialWorkspace() {
  const ws = mkdtempSync(join(tmpdir(), 'techstack-adv-'));
  const dataDir = join(ws, 'data', 'techstack');
  mkdirSync(dataDir, { recursive: true });
  writeFileSync(
    join(dataDir, 'domains.txt'),
    'example-evil.test\nexample-mixed.test\nexample-benign.test\n',
    'utf8',
  );
  const scans = [
    {
      domain: 'example-evil.test',
      url: EVIL_URL,
      http_status: 200,
      scanned_at: '2026-09-29T12:00:00+00:00',
      tool_count: 1,
      tools: [{ name: PAYLOAD, categories: ['xss'], confidence: 90, version: '1.0' }],
      fetch_error: null,
    },
    {
      domain: 'example-mixed.test',
      url: 'https://example-mixed.test/',
      http_status: 200,
      scanned_at: '2026-09-29T12:00:00+00:00',
      tool_count: 1,
      tools: [{ name: PAYLOAD_MIXED, categories: ['xss'], confidence: 80, version: '2.0' }],
      fetch_error: null,
    },
    {
      domain: 'example-benign.test',
      url: BENIGN_URL,
      http_status: 200,
      scanned_at: '2026-09-29T12:00:00+00:00',
      tool_count: 1,
      tools: [{ name: 'Nginx', categories: ['Web servers'], confidence: 95, version: '1.25.3' }],
      fetch_error: null,
    },
  ];
  writeFileSync(join(dataDir, 'scans.json'), JSON.stringify(scans, null, 1), 'utf8');
  writeFileSync(join(dataDir, 'skips.json'), '[]', 'utf8');
  writeFileSync(
    join(ws, 'sitemap.xml'),
    '<?xml version="1.0" encoding="UTF-8"?>\n'
      + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n</urlset>\n',
    'utf8',
  );
  return ws;
}

function runGenerator(ws) {
  return spawnSync(
    PYTHON,
    [GENERATOR, '--data', join(ws, 'data', 'techstack'), '--out', ws,
      '--sitemap', join(ws, 'sitemap.xml')],
    { encoding: 'utf8' },
  );
}

function page(ws, slug) {
  return readFileSync(join(ws, 'techstack', slug, 'index.html'), 'utf8');
}

function scriptStats(html) {
  const opens = [...html.matchAll(/<script\b([^>]*)>/gi)].map((m) => m[1].toLowerCase());
  const executable = opens.filter((attrs) => {
    const m = /type\s*=\s*["']?([^"'\s>]+)/.exec(attrs);
    if (!m) return true; // no type => executable by default
    return m[1] !== 'application/ld+json';
  }).length;
  return {
    open: opens.length,
    executable,
    closing: (html.match(/<\/script\s*>/gi) || []).length,
  };
}

function hrefs(html) {
  return [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
}

function dangerousScheme(href) {
  const normalized = href.replace(/&colon;/gi, ':').trim();
  return /^(javascript|data|vbscript):/i.test(normalized);
}

function jsonLdInner(html) {
  const m = /<script type="application\/ld\+json">\n([\s\S]*?)\n<\/script>/.exec(html);
  assert.ok(m, 'JSON-LD script block not found');
  return m[1];
}

// One workspace shared by the assertions below (generator runs once).
const ws = adversarialWorkspace();
const res = runGenerator(ws);
assert.equal(res.status, 0, `generator failed: ${res.stderr}`);

test('hardening: extra executable scripts 2 -> 0 on script-tag payload', () => {
  const stats = scriptStats(page(ws, 'example-evil-test'));
  assert.equal(stats.open, 1, `expected exactly 1 <script> open tag, got ${stats.open}`);
  assert.equal(stats.closing, 1, `expected exactly 1 </script> close tag, got ${stats.closing}`);
  assert.equal(stats.executable, 0,
    `expected 0 executable script elements, got ${stats.executable} (adversarial baseline was 2)`);
});

test('hardening: unsafe href 1 -> 0 on javascript: URL, fail-closed to plain text', () => {
  const html = page(ws, 'example-evil-test');
  const unsafe = hrefs(html).filter(dangerousScheme);
  assert.equal(unsafe.length, 0,
    `expected 0 dangerous-scheme hrefs, got ${unsafe.length} (adversarial baseline was 1): ${unsafe.join(', ')}`);
  // Fail-closed: the URL is still disclosed as escaped plain text, not a link.
  assert.ok(html.includes('javascript:alert(document.domain)'),
    'blocked URL must remain visible as plain text');
  assert.ok(!/<a[^>]+href="javascript:/i.test(html), 'javascript: URL must not be an anchor');
});

test('hardening: mixed-case closing-tag variant is neutralized', () => {
  const html = page(ws, 'example-mixed-test');
  const stats = scriptStats(html);
  assert.equal(stats.open, 1, `expected exactly 1 <script> open tag, got ${stats.open}`);
  assert.equal(stats.closing, 1, `expected exactly 1 </script> close tag, got ${stats.closing}`);
  assert.equal(stats.executable, 0, `expected 0 executable scripts, got ${stats.executable}`);
  assert.ok(!html.toLowerCase().includes('</script><script>'),
    'raw injected script sequence must not survive into the page');
});

test('hardening: JSON-LD stays parseable and semantically identical', () => {
  for (const slug of ['example-evil-test', 'example-mixed-test', 'example-benign-test']) {
    const inner = jsonLdInner(page(ws, slug));
    assert.ok(!/[<>&\u2028\u2029]/.test(inner),
      `${slug}: JSON-LD payload must not contain raw <, >, &, U+2028 or U+2029`);
    const parsed = JSON.parse(inner); // throws when serialization broke the JSON
    const names = parsed['@graph'][1].itemListElement.map((x) => x.name);
    if (slug === 'example-evil-test') assert.deepEqual(names, [PAYLOAD]);
    if (slug === 'example-mixed-test') assert.deepEqual(names, [PAYLOAD_MIXED]);
    if (slug === 'example-benign-test') assert.deepEqual(names, ['Nginx']);
  }
});

test('hardening: benign HTTPS URL keeps its link and escaping', () => {
  const html = page(ws, 'example-benign-test');
  assert.ok(html.includes(
    '<a rel="nofollow noopener" href="https://example-benign.test/?a=1&amp;b=2">'
    + 'https://example-benign.test/?a=1&amp;b=2</a>'),
    'benign https link must be preserved with rel and attribute escaping');
  const unsafe = hrefs(html).filter(dangerousScheme);
  assert.equal(unsafe.length, 0);
});

test('hardening: every href on adversarial pages is safe or internal', () => {
  for (const slug of ['example-evil-test', 'example-mixed-test', 'example-benign-test']) {
    for (const href of hrefs(page(ws, slug))) {
      const ok = dangerousScheme(href) === false
        && (href.startsWith('../') || href.startsWith('./') || href.startsWith('https://'));
      assert.ok(ok, `${slug}: unexpected href ${href}`);
    }
  }
});
