#!/usr/bin/env node
/**
 * Build guide/Careerkit-Guide.pdf from guide/careerkit-guide.html via headless Chromium.
 *
 *   node guide/build-pdf.mjs
 *
 * System fonts only, and the brand mark is a local file. The locked Playwright Chromium
 * renders without webfont, CDN, or other network fetches at print time. A failed asset is
 * reported loudly rather than shipped as a silently missing logo.
 *
 * Needs the locked toolchain: npm ci && npx playwright install chromium
 */
import { chromium } from 'playwright';
import { createHash } from 'crypto';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { existsSync, readFileSync, statSync } from 'fs';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, 'careerkit-guide.html');
const out = join(here, 'Careerkit-Guide.pdf');
const projectRoot = join(here, '..');

if (!existsSync(src)) {
  console.error(`missing source: ${src}`);
  process.exit(1);
}

// Make freshness machine-verifiable without comparing platform-specific PDF
// drawing bytes. The short id covers every source that can change the rendered
// artifact, and is printed unobtrusively on the final page. CI compares
// normalized extracted text from the committed PDF with a fresh locked build.
const sourceHash = createHash('sha256');
for (const input of [
  src,
  fileURLToPath(import.meta.url),
  join(projectRoot, 'package.json'),
  join(projectRoot, 'package-lock.json'),
  join(projectRoot, 'brand', 'careerkit-mark.svg'),
]) {
  sourceHash.update(readFileSync(input));
  sourceHash.update('\0');
}
const sourceId = sourceHash.digest('hex').slice(0, 12);

let browser;
try {
  browser = await chromium.launch();
} catch (error) {
  console.error('Could not launch the Playwright-managed Chromium used to build the guide.');
  console.error('Run `npm ci && npx playwright install chromium`, then try again.');
  console.error(error.message);
  process.exit(1);
}
const page = await browser.newPage();

const failed = [];
const external = new Set();
page.on('requestfailed', (r) => failed.push(r.url()));
// Printing is a release build, not a browsing session. Block every network
// request rather than merely hoping the current HTML has no remote assets.
// This turns a later CDN image, webfont, or tracking pixel into a loud build
// failure and keeps the PDF reproducible/offline by construction.
await page.route(/^https?:\/\//, async (route) => {
  external.add(route.request().url());
  await route.abort('blockedbyclient');
});

await page.goto('file://' + src, { waitUntil: 'load' });
await page.locator('#build-id').evaluate((node, id) => {
  node.textContent = `Guide build ${id}`;
}, sourceId);

if (external.size) {
  await browser.close();
  console.error(`  ! ${external.size} external request(s) blocked; guide builds must be offline:`);
  [...external].forEach((u) => console.error(`    ${u}`));
  process.exit(1);
}

await page.pdf({
  path: out,
  format: 'Letter',
  printBackground: true,          // without this every panel and rule prints white
  displayHeaderFooter: true,
  headerTemplate: '<div></div>',  // Chromium requires a node even when empty
  footerTemplate: `
    <div style="width:100%; font-family:Menlo,Consolas,monospace; font-size:7.4pt;
                color:#5C6B63; letter-spacing:0.06em; padding:0 0.9in;
                display:flex; justify-content:space-between; align-items:center;">
      <span>CareerKit</span>
      <span>github.com/omoji-personal/careerkit</span>
      <span class="pageNumber"></span>
    </div>`,
  margin: { top: '0.85in', right: '0.9in', bottom: '0.95in', left: '1.05in' },
});

await browser.close();

if (failed.length) {
  console.error(`  ! ${failed.length} asset(s) failed to load, PDF may be missing artwork:`);
  failed.forEach((u) => console.error(`    ${u}`));
  process.exit(1);
}

const kb = Math.round(statSync(out).size / 1024);
console.log(`built ${out} (${kb} KB)`);
