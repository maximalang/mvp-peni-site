// Generator test for /techstack/ pages (fixture-based, no live HTTP).
// Runs tools/generate_techstack_pages.py against tests/fixtures/techstack data
// in a temp dir and asserts page inventory, canonical URLs, PII-free output and
// sitemap idempotency.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const GENERATOR = join(ROOT, 'tools', 'generate_techstack_pages.py');
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, 'tests', 'fixtures', 'techstack', 'fixture_data.json'), 'utf8'),
);
const BASE_URL = 'https://peni.recruiter-radar.ru';
const PYTHON = process.env.PYTHON_BIN || 'python';

function makeWorkspace() {
  const ws = mkdtempSync(join(tmpdir(), 'techstack-gen-'));
  const dataDir = join(ws, 'data', 'techstack');
  mkdirSync(dataDir, { recursive: true });
  writeFileSync(join(dataDir, 'domains.txt'), FIXTURE.domains, 'utf8');
  writeFileSync(join(dataDir, 'scans.json'), JSON.stringify(FIXTURE.scans, null, 1), 'utf8');
  writeFileSync(join(dataDir, 'skips.json'), JSON.stringify(FIXTURE.skips, null, 1), 'utf8');
  writeFileSync(join(ws, 'sitemap.xml'), FIXTURE.sitemap, 'utf8');
  return ws;
}

function runGenerator(ws) {
  const res = spawnSync(
    PYTHON,
    [GENERATOR, '--data', join(ws, 'data', 'techstack'), '--out', ws, '--sitemap', join(ws, 'sitemap.xml')],
    { encoding: 'utf8' },
  );
  return res;
}

test('generator: fixture run produces exactly the successful pages', () => {
  const ws = makeWorkspace();
  const res = runGenerator(ws);
  assert.equal(res.status, 0, `generator failed: ${res.stderr}`);
  const summary = JSON.parse(res.stdout.trim().split('\n').pop());
  assert.equal(summary.pages_generated, 2);
  assert.equal(summary.sitemap_urls, 3); // /techstack/ + 2 domain pages

  const one = join(ws, 'techstack', 'example-one-test', 'index.html');
  const two = join(ws, 'techstack', 'example-two-test', 'index.html');
  const index = join(ws, 'techstack', 'index.html');
  assert.ok(existsSync(one), 'example-one-test page missing');
  assert.ok(existsSync(two), 'example-two-test page missing');
  assert.ok(existsSync(index), 'index page missing');
  // zero-tool, fetch-error and not-listed domains get no page
  assert.ok(!existsSync(join(ws, 'techstack', 'example-zero-test')), 'zero-tool page must not exist');
  assert.ok(!existsSync(join(ws, 'techstack', 'example-broken-test')), 'fetch-error page must not exist');
  assert.ok(!existsSync(join(ws, 'techstack', 'not-in-domains-test')), 'unlisted-domain page must not exist');
});

test('generator: canonical, heading, best-record selection', () => {
  const ws = makeWorkspace();
  assert.equal(runGenerator(ws).status, 0);
  const one = readFileSync(join(ws, 'techstack', 'example-one-test', 'index.html'), 'utf8');
  const two = readFileSync(join(ws, 'techstack', 'example-two-test', 'index.html'), 'utf8');

  assert.match(one, new RegExp(`<link rel="canonical" href="${BASE_URL}/techstack/example-one-test/">`));
  assert.match(two, new RegExp(`<link rel="canonical" href="${BASE_URL}/techstack/example-two-test/">`));
  assert.match(one, /<h1>Технологии сайта example-one\.test — что используется<\/h1>/);
  assert.match(one, /Nginx/);
  assert.match(one, /1\.25\.3/);
  // duplicate domain records: the richest (2-tool, WordPress 6.6) record must win
  assert.match(two, /WordPress/);
  assert.match(two, /6\.6/);
  assert.match(one, /по данным открытой главной страницы|открытой главной страницы/i);
});

test('generator: PII fields never reach HTML (whitelist rendering)', () => {
  const ws = makeWorkspace();
  assert.equal(runGenerator(ws).status, 0);
  const one = readFileSync(join(ws, 'techstack', 'example-one-test', 'index.html'), 'utf8');
  const index = readFileSync(join(ws, 'techstack', 'index.html'), 'utf8');
  for (const html of [one, index]) {
    assert.ok(!html.includes('ivan.petrov'), 'PII: person name leaked');
    assert.ok(!html.includes('example-mail.test'), 'PII: email domain leaked');
    assert.ok(!html.includes('Set-Cookie'), 'PII: raw header leaked');
    assert.ok(!/[\w.+-]+@[\w-]+\.[\w.]+/.test(html), 'PII: email-like string leaked');
  }
});

test('generator: index links pages and reports skips honestly', () => {
  const ws = makeWorkspace();
  assert.equal(runGenerator(ws).status, 0);
  const index = readFileSync(join(ws, 'techstack', 'index.html'), 'utf8');
  assert.match(index, /href="\.\/example-one-test\/"/);
  assert.match(index, /href="\.\/example-two-test\/"/);
  assert.match(index, /example-fortress\.test/);
  assert.match(index, /TLS handshake reset/);
});

test('generator: sitemap section is updated and idempotent', () => {
  const ws = makeWorkspace();
  assert.equal(runGenerator(ws).status, 0);
  const sitemapPath = join(ws, 'sitemap.xml');
  const first = readFileSync(sitemapPath, 'utf8');
  assert.match(first, /<loc>https:\/\/peni\.recruiter-radar\.ru\/<\/loc>/, 'pre-existing URL lost');
  assert.match(first, /<loc>https:\/\/peni\.recruiter-radar\.ru\/techstack\/<\/loc>/);
  assert.match(first, /<loc>https:\/\/peni\.recruiter-radar\.ru\/techstack\/example-one-test\/<\/loc>/);
  assert.match(first, /<loc>https:\/\/peni\.recruiter-radar\.ru\/techstack\/example-two-test\/<\/loc>/);
  const urlCount = (first.match(/<loc>/g) || []).length;
  assert.equal(urlCount, 4);

  // second run over the same sitemap: byte-identical (no duplicate entries)
  const pagesBefore = readdirSync(join(ws, 'techstack')).sort();
  assert.equal(runGenerator(ws).status, 0);
  const second = readFileSync(sitemapPath, 'utf8');
  assert.equal(second, first, 'sitemap is not idempotent across runs');
  const pagesAfter = readdirSync(join(ws, 'techstack')).sort();
  assert.deepEqual(pagesAfter, pagesBefore);
});

test('generator: empty success set exits non-zero', () => {
  const ws = makeWorkspace();
  writeFileSync(
    join(ws, 'data', 'techstack', 'scans.json'),
    JSON.stringify(FIXTURE.scans.filter((r) => r.tool_count < 1)),
    'utf8',
  );
  const res = runGenerator(ws);
  assert.notEqual(res.status, 0, 'generator must fail when nothing succeeded');
});
