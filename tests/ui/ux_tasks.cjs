// Browser acceptance tasks against a running staging server seeded with scripts/seed_demo.py.
// usage: node tests/ui/ux_tasks.cjs http://127.0.0.1:8800 <screenshot_dir>
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8800';
const SHOTS = process.argv[3] || 'ui-shots';
fs.mkdirSync(SHOTS, { recursive: true });
const results = [];
const rec = (ok, name, detail = '') => results.push({ ok: !!ok, name, detail });
const PAGES = ['/', '/radar', '/radar/1', '/store', '/store/progress-invoices', '/pricing', '/calculator', '/sources', '/login', '/register', '/privacy'];
const stamp = Date.now();

async function audit(browser, label, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: 'ar-SA' });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  for (const p of PAGES) {
    const resp = await page.goto(BASE + p);
    rec(resp.status() === 200, `${label} ${p} loads`, `status ${resp.status()}`);
    await page.screenshot({ path: path.join(SHOTS, `${label}${p.replace(/\//g, '_') || '_home'}.png`), fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    rec(overflow <= 1, `${label} ${p} no horizontal scroll`, `overflow ${overflow}px`);
    const a11y = await page.evaluate(() => {
      const unlabeled = [...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(el =>
        !el.closest('label') && !el.getAttribute('aria-label') && !(el.id && document.querySelector(`label[for="${el.id}"]`)));
      const noAlt = [...document.querySelectorAll('img')].filter(i => !i.hasAttribute('alt'));
      return { unlabeled: unlabeled.length, noAlt: noAlt.length, lang: document.documentElement.lang, dir: document.documentElement.dir,
        h1: document.querySelectorAll('h1').length };
    });
    rec(a11y.unlabeled === 0 && a11y.noAlt === 0 && a11y.lang === 'ar' && a11y.dir === 'rtl' && a11y.h1 === 1,
      `${label} ${p} labels/alt/lang/h1`, JSON.stringify(a11y));
    if (viewport.width < 500) {
      const small = await page.evaluate(() => [...document.querySelectorAll('button, .btn, input:not([type=hidden]):not([type=checkbox]), select, .nav a, .menu-btn')]
        .filter(n => n.offsetParent !== null).map(n => ({ t: (n.textContent || n.name || '').trim().slice(0, 20), h: n.getBoundingClientRect().height }))
        .filter(x => x.h > 0 && x.h < 40));
      rec(small.length === 0, `${label} ${p} tap targets >= 40px`, JSON.stringify(small.slice(0, 4)));
    }
  }
  rec(errors.length === 0, `${label} no console errors (incl. CSP violations)`, errors.slice(0, 3).join(' | '));
  if (viewport.width < 500) {
    await page.goto(BASE + '/');
    await page.click('#menuBtn');
    rec(await page.locator('#siteNav').isVisible(), 'mobile menu opens');
    await page.screenshot({ path: path.join(SHOTS, `${label}_menu_open.png`) });
  }
  await ctx.close();
}

async function journey(browser) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true, locale: 'ar-SA' });
  const page = await ctx.newPage();
  page.on('dialog', d => d.accept());
  const email = `ui-${stamp}@demo.example.sa`;
  await page.goto(BASE + '/register');
  await page.fill('input[name=name]', 'مؤسسة اختبار');
  await page.fill('input[name=email]', email);
  await page.fill('input[name=password]', 'ui-password-123');
  await page.check('input[name=agree]');
  await page.click('form[action="/register"] button[type=submit]');
  rec(page.url().includes('/account/profile'), 'register -> profile', page.url());
  await page.check('input[name=trades][value="الدهانات"]');
  await page.check('input[name=cities][value="ينبع"]');
  await page.click('form[action="/account/profile"] button[type=submit]');
  rec(page.url().includes('/dashboard'), 'profile saved -> dashboard');
  rec(await page.locator('.badge.b-high').count() > 0, 'dashboard shows high-match opportunity');
  await page.screenshot({ path: path.join(SHOTS, 'flow_dashboard_mobile.png'), fullPage: true });

  await page.goto(BASE + '/radar?trade=' + encodeURIComponent('الدهانات'));
  rec(await page.locator('#results .opp').count() >= 1, 'radar trade filter returns results');
  await page.locator('#results .opp h3 a').first().click();
  await page.click('#saveOpp');
  rec(/\/pipeline\/\d+/.test(page.url()), 'save opportunity -> pipeline item');
  await page.selectOption('#stageSelect', 'studying');
  await page.click('form[action$="/update"] button[type=submit]');
  rec((await page.locator('.stage.on').textContent()).includes('قيد الدراسة'), 'pipeline stage updated');
  await page.fill('input[name=outcome]', 'تواصلنا مع المشتريات وطلبوا عرضًا');
  await page.click('form[action$="/log"] button[type=submit]');
  rec(await page.locator('text=تواصلنا مع المشتريات').count() === 1, 'contact log recorded');
  await page.click('#makeQuote');
  rec(/\/quotes\/\d+/.test(page.url()), 'quote draft created from opportunity');
  const qty = page.locator('input[name=qty]');
  const price = page.locator('input[name=unit_price]');
  await qty.nth(0).fill('100'); await price.nth(0).fill('20');
  await qty.nth(1).fill('100'); await price.nth(1).fill('25');
  await page.click('#quoteTotals button[type=submit]');
  const totals = await page.locator('#quoteTotals').textContent();
  rec(totals.includes('4,500.00') && totals.includes('5,175.00'), 'quote totals 4,500 + 15% = 5,175', totals.replace(/\s+/g, ' ').slice(0, 160));
  await page.screenshot({ path: path.join(SHOTS, 'flow_quote_mobile.png'), fullPage: true });
  const [qdl] = await Promise.all([page.waitForEvent('download'), page.click('#quoteXlsx')]);
  const qpath = await qdl.path();
  const magic = fs.readFileSync(qpath).subarray(0, 2).toString();
  rec(magic === 'PK' && (await qdl.suggestedFilename()).endsWith('.xlsx'), 'quote exported as xlsx',
    `name=${await qdl.suggestedFilename()} magic=${magic} size=${fs.statSync(qpath).size}`);

  await page.goto(BASE + '/store/progress-invoices');
  const [sample] = await Promise.all([page.waitForEvent('download'), page.click('a[href="/samples/progress-invoices.xlsx"]')]);
  rec(fs.statSync(await sample.path()).size > 5000, 'sample download works');
  await page.click('#buyBtn');
  await page.click('#payOk');
  rec((await page.locator('#orderStatus').textContent()).includes('مدفوع'), 'sandbox payment -> paid receipt');
  await page.screenshot({ path: path.join(SHOTS, 'flow_receipt_mobile.png'), fullPage: true });
  await page.goto(BASE + '/account/orders');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('button[data-slug="progress-invoices"]')]);
  rec(fs.statSync(await dl.path()).size > 10000 && (await dl.suggestedFilename()).endsWith('.xlsx'), 'purchased file downloads as .xlsx', await dl.suggestedFilename());
  const leaked = await page.evaluate(() => document.body.innerHTML.includes('03_progress_invoices'));
  rec(!leaked, 'internal file path never exposed in HTML');
  await ctx.close();
}

async function adminAndCalc(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ar-SA' });
  const page = await ctx.newPage();
  await page.goto(BASE + '/login');
  await page.fill('input[name=email]', 'admin@demo.example.sa');
  await page.fill('input[name=password]', 'demo-password-123');
  await page.click('form[action="/login"] button[type=submit]');
  await page.goto(BASE + '/admin');
  rec(await page.locator('#sourcesTable tbody tr').count() >= 4, 'admin sees source status table');
  await page.screenshot({ path: path.join(SHOTS, 'admin_dashboard_desktop.png'), fullPage: true });
  await page.goto(BASE + '/admin/review');
  await page.screenshot({ path: path.join(SHOTS, 'admin_review_desktop.png'), fullPage: true });
  rec(await page.locator('text=المدينة غير محددة').count() >= 1, 'review queue shows reasons');

  await page.goto(BASE + '/calculator');
  rec((await page.locator('#grandTotal').textContent()).trim() === '—', 'calculator shows no number before costs are entered');
  await page.fill('input[name=materials]', '10');
  await page.fill('input[name=labor]', '5');
  const ok = await page.locator('#grandTotal').textContent();
  rec(ok.includes('٢٬٣٠٠') || ok.includes('2,300'), 'calculator: 100*(10+5), 10%+15% -> 2,300 incl. VAT', ok);
  await page.fill('input[name=margin]', '95');
  const bad = (await page.locator('#grandTotal').textContent()).trim();
  rec(bad === '—' && await page.locator('#copyQuote').isDisabled(), 'FIXED: invalid input clears stale totals and disables copy', bad);
  await page.screenshot({ path: path.join(SHOTS, 'calculator_invalid_desktop.png') });
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  await audit(browser, 'm390', { width: 390, height: 844 });
  await audit(browser, 'd1440', { width: 1440, height: 900 });
  await journey(browser);
  await adminAndCalc(browser);
  await browser.close();
  let fails = 0;
  for (const r of results) { if (!r.ok) fails++; console.log(`${r.ok ? 'PASS' : 'FAIL'} | ${r.name}${r.detail && !r.ok ? ' | ' + r.detail : ''}`); }
  console.log(`\n${results.length - fails} passed, ${fails} failed`);
  process.exit(fails ? 1 : 0);
})();
