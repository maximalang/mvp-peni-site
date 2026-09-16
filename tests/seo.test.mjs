import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const BASE_URL = 'https://peni.recruiter-radar.ru';
const PAGE_FILES = readdirSync(ROOT)
  .filter((name) => name.endsWith('.html'))
  .sort((a, b) => (a === 'index.html' ? -1 : b === 'index.html' ? 1 : a.localeCompare(b)));

const EXPECTED_FILES = [
  'index.html',
  'faq.html',
  'istoriya-klyuchevoy-stavki.html',
  'metodika-raschyota.html',
].sort((a, b) => (a === 'index.html' ? -1 : b === 'index.html' ? 1 : a.localeCompare(b)));

const EXPECTED_TYPES = {
  'index.html': ['WebApplication', 'HowTo'],
  'faq.html': ['FAQPage'],
  'istoriya-klyuchevoy-stavki.html': ['WebPage'],
  'metodika-raschyota.html': ['WebPage'],
};

const EXPECTED_LINKS = {
  'index.html': [
    './istoriya-klyuchevoy-stavki.html',
    './metodika-raschyota.html',
    './faq.html',
  ],
  'faq.html': ['./', './istoriya-klyuchevoy-stavki.html', './metodika-raschyota.html'],
  'istoriya-klyuchevoy-stavki.html': ['./', './metodika-raschyota.html', './faq.html'],
  'metodika-raschyota.html': ['./', './istoriya-klyuchevoy-stavki.html', './faq.html'],
};

function routeFor(file) {
  return file === 'index.html' ? '/' : `/${file}`;
}

function expectedUrlFor(file) {
  return `${BASE_URL}${routeFor(file)}`;
}

function oneMatch(pattern, input, label) {
  const match = input.match(pattern);
  assert.ok(match, label);
  return match[1];
}

function metaValue(html, attribute, value) {
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return oneMatch(
    new RegExp(`<meta\\s+${attribute}=["']${escaped}["']\\s+content=["']([^"']+)["']`, 'i'),
    html,
    `${attribute}=${value} is missing`,
  );
}

function jsonLdItems(html, file) {
  const scripts = [...html.matchAll(
    /<script\b[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi,
  )];
  assert.ok(scripts.length > 0, `${file}: JSON-LD script is missing`);

  return scripts.flatMap((match) => {
    let value;
    try {
      value = JSON.parse(match[1]);
    } catch (error) {
      throw new Error(`${file}: JSON-LD is not valid JSON: ${error.message}`);
    }
    const items = Array.isArray(value) ? value : value['@graph'] ? value['@graph'] : [value];
    for (const item of items) {
      assert.equal(item['@context'], 'https://schema.org', `${file}: JSON-LD context is not schema.org`);
      assert.equal(typeof item['@type'], 'string', `${file}: JSON-LD type is missing`);
    }
    return items;
  });
}

function validateJsonLd(items, file, canonical) {
  const types = items.map((item) => item['@type']);
  for (const expectedType of EXPECTED_TYPES[file]) {
    assert.ok(types.includes(expectedType), `${file}: JSON-LD type ${expectedType} is missing`);
  }

  for (const item of items) {
    if (['WebApplication', 'WebPage', 'HowTo'].includes(item['@type'])) {
      assert.equal(typeof item.name, 'string', `${file}: ${item['@type']}.name is missing`);
      assert.equal(typeof item.description, 'string', `${file}: ${item['@type']}.description is missing`);
      if (item.url) assert.equal(item.url, canonical, `${file}: ${item['@type']}.url differs from canonical`);
    }
    if (item['@type'] === 'WebApplication') {
      assert.equal(typeof item.applicationCategory, 'string', `${file}: WebApplication.applicationCategory is missing`);
    }
    if (item['@type'] === 'HowTo') {
      assert.ok(Array.isArray(item.step) && item.step.length > 0, `${file}: HowTo.step is empty`);
      for (const step of item.step) {
        assert.equal(step['@type'], 'HowToStep', `${file}: HowTo step type is invalid`);
        assert.equal(typeof step.name, 'string', `${file}: HowTo step name is missing`);
        assert.equal(typeof step.text, 'string', `${file}: HowTo step text is missing`);
      }
    }
    if (item['@type'] === 'FAQPage') {
      assert.ok(Array.isArray(item.mainEntity) && item.mainEntity.length > 0, `${file}: FAQPage.mainEntity is empty`);
      for (const question of item.mainEntity) {
        assert.equal(question['@type'], 'Question', `${file}: FAQ question type is invalid`);
        assert.equal(typeof question.name, 'string', `${file}: FAQ question name is missing`);
        assert.equal(question.acceptedAnswer?.['@type'], 'Answer', `${file}: FAQ answer type is invalid`);
        assert.equal(typeof question.acceptedAnswer?.text, 'string', `${file}: FAQ answer text is missing`);
      }
    }
  }

  return [...new Set(types)];
}

function hrefs(html) {
  return [...html.matchAll(/\bhref=["']([^"']+)["']/gi)].map((match) => match[1]);
}

assert.deepEqual(PAGE_FILES, EXPECTED_FILES, 'top-level HTML page inventory changed unexpectedly');

for (const file of PAGE_FILES) {
  const html = readFileSync(join(ROOT, file), 'utf8');
  const route = routeFor(file);
  const canonical = expectedUrlFor(file);

  assert.equal(
    oneMatch(/<link\s+rel=["']canonical["']\s+href=["']([^"']+)["']/i, html, `${file}: canonical is missing`),
    canonical,
    `${file}: canonical URL is incorrect`,
  );
  assert.equal(metaValue(html, 'property', 'og:title').length > 0, true, `${file}: og:title is empty`);
  assert.equal(metaValue(html, 'property', 'og:description').length > 0, true, `${file}: og:description is empty`);
  assert.equal(metaValue(html, 'property', 'og:type'), 'website', `${file}: og:type is not website`);
  assert.equal(metaValue(html, 'property', 'og:url'), canonical, `${file}: og:url differs from canonical`);
  assert.equal(metaValue(html, 'name', 'twitter:card'), 'summary', `${file}: twitter:card is not summary`);
  assert.equal(metaValue(html, 'name', 'twitter:title').length > 0, true, `${file}: twitter:title is empty`);
  assert.equal(metaValue(html, 'name', 'twitter:description').length > 0, true, `${file}: twitter:description is empty`);

  const items = jsonLdItems(html, file);
  const types = validateJsonLd(items, file, canonical);
  for (const link of EXPECTED_LINKS[file]) {
    assert.ok(hrefs(html).includes(link), `${file}: internal link ${link} is missing`);
  }

  console.log(`PASS ${route} (${file}): canonical + OG + Twitter + JSON-LD [${types.join(', ')}] + internal links`);
}

const sitemap = readFileSync(join(ROOT, 'sitemap.xml'), 'utf8');
assert.match(
  sitemap,
  /<urlset\b[^>]*xmlns=["']http:\/\/www\.sitemaps\.org\/schemas\/sitemap\/0\.9["']/,
  'sitemap.xml: standard sitemap namespace is missing',
);
assert.doesNotMatch(sitemap, /PAGES_URL/, 'sitemap.xml: placeholder remains');
const sitemapUrls = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => match[1]);
const expectedSitemapUrls = PAGE_FILES.map(expectedUrlFor);
assert.equal(sitemapUrls.length, expectedSitemapUrls.length, 'sitemap.xml: URL count differs from HTML page count');
assert.deepEqual([...new Set(sitemapUrls)].sort(), [...expectedSitemapUrls].sort(), 'sitemap.xml: URL set does not match existing HTML pages');
for (const url of sitemapUrls) {
  const parsed = new URL(url);
  assert.equal(parsed.protocol, 'https:', `sitemap.xml: ${url} is not HTTPS`);
  assert.equal(parsed.hostname, 'peni.recruiter-radar.ru', `sitemap.xml: ${url} has an unexpected host`);
}
console.log(`PASS sitemap.xml: ${sitemapUrls.length} existing HTML URLs, standard namespace, production hostname`);

const robots = readFileSync(join(ROOT, 'robots.txt'), 'utf8');
assert.match(robots, /^Sitemap:\s+https:\/\/peni\.recruiter-radar\.ru\/sitemap\.xml\s*$/m, 'robots.txt: sitemap link is missing or incorrect');
assert.doesNotMatch(robots, /[<>]|PAGES_URL/, 'robots.txt: unsafe placeholder remains');
console.log('PASS robots.txt: sitemap directive present and placeholder is syntax-safe');

console.log(`SEO gate PASS: ${PAGE_FILES.length} HTML pages + sitemap.xml + robots.txt`);
