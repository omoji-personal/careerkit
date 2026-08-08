#!/usr/bin/env node
/**
 * Build guide/Careerkit-Guide.pdf from guide/careerkit-guide.html via headless Chromium.
 *
 *   node guide/build-pdf.mjs
 *
 * System fonts only, and the brand mark is a local file. The PDF must render identically
 * offline with no webfont fetch, no CDN, and no network at print time. A failed asset is
 * reported loudly rather than shipped as a silently missing logo.
 *
 * Needs playwright:  npm install
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { existsSync, statSync } from 'fs';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, 'careerkit-guide.html');
const out = join(here, 'Careerkit-Guide.pdf');

if (!existsSync(src)) {
  console.error(`missing source: ${src}`);
  process.exit(1);
}

// `npm install` installs the Playwright library but, depending on npm settings,
// may not download its bundled Chromium. CareerKit already documents Chrome as
// a prerequisite, so use it when available instead of failing with Playwright's
// opaque "executable doesn't exist" message. PLAYWRIGHT_CHROMIUM_PATH supports
// nonstandard installations and CI images.
const browserCandidates = [
  process.env.PLAYWRIGHT_CHROMIUM_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);
const systemBrowser = browserCandidates.find(existsSync);
let browser;
try {
  browser = await chromium.launch(systemBrowser ? { executablePath: systemBrowser } : {});
} catch (error) {
  console.error('Could not launch Chromium to build the guide.');
  console.error('Run `npx playwright install chromium`, or set PLAYWRIGHT_CHROMIUM_PATH.');
  console.error(error.message);
  process.exit(1);
}
const page = await browser.newPage();

const failed = [];
page.on('requestfailed', (r) => failed.push(r.url()));

await page.goto('file://' + src, { waitUntil: 'load' });

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
