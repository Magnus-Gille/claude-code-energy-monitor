/* Screenshots for scripts/demo.py: node demo_screens.cjs REPORT.html OUTDIR SESSION.html OVERHEAD.html
   PLAYWRIGHT_MODULE names the playwright package (default: the cached npx copy). */
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/Users/magnus/.npm/_npx/705bc6b22212b352/node_modules/playwright');
const [report, outdir, sessionHtml, overheadHtml] = process.argv.slice(2);
(async () => {
  const browser = await playwright.chromium.launch({headless: true});
  try {
    const errors = [];
    let context = await browser.newContext({viewport: {width: 1440, height: 900}, offline: true, deviceScaleFactor: 2});
    let page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent(fs.readFileSync(report, 'utf8'), {waitUntil: 'load'});
    // The page CSP forbids eval, which waitForFunction needs; poll through evaluate instead.
    for (const deadline = Date.now() + 60000; !(await page.evaluate(() => window.reportReady === true));) {
      if (Date.now() > deadline) throw new Error('report did not become ready: ' + errors.join('; '));
      await page.waitForTimeout(50);
    }
    await page.screenshot({path: path.join(outdir, 'overview.png')});
    await context.close();
    for (const [file, name] of [[sessionHtml, 'session.png'], [overheadHtml, 'overhead.png']]) {
      context = await browser.newContext({viewport: {width: 1200, height: 700}, deviceScaleFactor: 2});
      page = await context.newPage();
      await page.setContent(fs.readFileSync(file, 'utf8'), {waitUntil: 'load'});
      await page.screenshot({path: path.join(outdir, name)});
      await context.close();
    }
    if (errors.length) throw new Error('page errors: ' + errors.join('; '));
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
