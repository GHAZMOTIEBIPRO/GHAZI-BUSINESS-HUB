// Real screenshots of a deployed instance on a phone (iPhone 13 emulation) and a 1440px desktop.
// Fails on horizontal overflow, console errors (incl. CSP violations), non-200 pages or missing RTL.
//   node scripts/live_screens.cjs https://ghazi-business-hub-dd3h.onrender.com live-shots
const { chromium, devices } = require("playwright");
const fs = require("fs");

const base = (process.argv[2] || "").replace(/\/$/, "");
const out = process.argv[3] || "live-shots";
const PAGES = ["/", "/radar", "/store", "/store/finishing-pricing", "/pricing", "/sources", "/calculator", "/login", "/register"];
const VIEWS = { phone: devices["iPhone 13"], desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 } };

(async () => {
  if (!/^https?:\/\//.test(base)) { console.error("usage: live_screens.cjs <base-url> [out-dir]"); process.exit(2); }
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const results = [];
  for (const [view, opts] of Object.entries(VIEWS)) {
    const ctx = await browser.newContext({ ...opts, locale: "ar-SA" });
    const page = await ctx.newPage();
    let errors = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
    page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
    for (const path of PAGES) {
      errors = [];
      const resp = await page.goto(base + path, { waitUntil: "networkidle", timeout: 120000 });
      const facts = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        dir: document.documentElement.getAttribute("dir"),
        lang: document.documentElement.getAttribute("lang"),
        h1: (document.querySelector("h1") || {}).textContent || "",
        tinyTap: [...document.querySelectorAll("a,button,input,select")].filter((el) => {
          const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 24 && el.offsetParent; }).length,
      }));
      const name = `${view}${path === "/" ? "-home" : path.replace(/\//g, "-")}.png`;
      await page.screenshot({ path: `${out}/${name}`, fullPage: true });
      const ok = resp && resp.status() === 200 && facts.overflow <= 1 && facts.dir === "rtl" && errors.length === 0;
      results.push({ view, path, status: resp && resp.status(), ...facts, errors, shot: name, ok });
      console.log(`${ok ? "PASS" : "FAIL"} ${view} ${path} status=${resp && resp.status()} overflow=${facts.overflow}px dir=${facts.dir} smallTargets=${facts.tinyTap}${errors.length ? " errors=" + JSON.stringify(errors) : ""}`);
    }
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync(`${out}/results.json`, JSON.stringify(results, null, 1));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} page views passed`);
  process.exit(failed ? 1 : 0);
})();
