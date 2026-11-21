"""Log-only survey of Saudi government and project platforms, and of public archive copies of official open data files.

Run on a GitHub runner (procurement-probe.yml). Two parts, nothing stored but the log:

--sites    one GET per homepage (and robots.txt): status, final URL, title, whether the page shows a CAPTCHA or a
           sign-in form, and the links whose text or address mentions tenders / suppliers / projects / contracts.
           A CAPTCHA, 401, 403 or 451 is recorded and never retried or worked around.
--archive  asks the Internet Archive's public index (CDX API) which spreadsheet / CSV files from the national open data
           platforms it holds. Lists address, capture time, type and size only; downloads nothing.

    python scripts/probe_sites_and_archive.py --sites --archive
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

import httpx

UA = "ghazi-hub-source-probe/1.3 (public-sector source survey; log only)"
SITES = {
    "procurement and finance": [
        "https://portal.etimad.sa/", "https://www.mof.gov.sa/", "https://lcgpa.gov.sa/", "https://www.expro.gov.sa/",
        "https://www.ncgr.gov.sa/", "https://www.my.gov.sa/",
    ],
    "open data and statistics": [
        "https://open.data.gov.sa/", "https://od.data.gov.sa/", "https://www.stats.gov.sa/", "https://sdaia.gov.sa/",
    ],
    "municipal, housing, industry": [
        "https://momah.gov.sa/", "https://balady.gov.sa/", "https://furas.momah.gov.sa/", "https://www.housing.gov.sa/",
        "https://sakani.sa/", "https://modon.gov.sa/", "https://www.mim.gov.sa/",
    ],
    "ministries with large works": [
        "https://www.moe.gov.sa/", "https://www.moh.gov.sa/", "https://www.mot.gov.sa/", "https://www.mewa.gov.sa/",
        "https://www.moi.gov.sa/", "https://www.hajj.gov.sa/",
    ],
    "royal commissions and regions": [
        "https://www.rcjy.gov.sa/", "https://www.rcyanbu.gov.sa/", "https://www.rcrc.gov.sa/", "https://www.rcu.gov.sa/",
        "https://www.alriyadh.gov.sa/", "https://www.jeddah.gov.sa/",
    ],
    "contractors and registries": [
        "https://muqawil.org/", "https://sca.sa/", "https://mc.gov.sa/", "https://www.monshaat.gov.sa/",
    ],
    "giga projects and developers": [
        "https://www.neom.com/", "https://www.redseaglobal.com/", "https://www.roshn.sa/", "https://www.qiddiya.com/",
        "https://www.dgda.gov.sa/", "https://www.pif.gov.sa/", "https://www.newmurabba.com/", "https://www.kingsalmanpark.sa/",
    ],
    "utilities and national companies": [
        "https://www.nwc.com.sa/", "https://www.se.com.sa/", "https://www.aramco.com/", "https://www.maaden.com.sa/",
        "https://www.sabic.com/", "https://www.swpc.sa/",
    ],
}
LINK_HINT = re.compile(r"(tender|procure|supplier|vendor|bid|contract|project|opportunit|منافس|مناقص|مشتريات|موردين|"
                       r"المورد|عقود|مشاريع|فرص|الترسية)", re.I)
CAPTCHA = re.compile(r"(captcha|are you a human|human visitor|verify you are human|cf-challenge|اختبار الأمان)", re.I)


def text_of(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))).strip()


def survey(url: str) -> dict:
    out = {"url": url}
    parts = urllib.parse.urlsplit(url)
    with httpx.Client(timeout=httpx.Timeout(20, connect=12), headers={"User-Agent": UA, "Accept-Language": "ar,en"},
                      follow_redirects=True) as c:
        try:
            r = c.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
            if r.status_code == 200 and "text/plain" in r.headers.get("content-type", ""):
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                out["robots"] = f"allows homepage={rp.can_fetch(UA, url)}"
            else:
                out["robots"] = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            out["robots"] = f"unreachable ({type(e).__name__})"
        try:
            r = c.get(url)
        except httpx.HTTPError as e:
            out["result"] = f"unreachable: {type(e).__name__}"
            return out
        body = r.text if "html" in r.headers.get("content-type", "") else ""
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
        out.update(status=r.status_code, final=str(r.url)[:120], title=text_of(title.group(1))[:80] if title else "",
                   captcha=bool(CAPTCHA.search(body[:20000])), sign_in=bool(re.search(r'type=["\']password', body)))
        if r.status_code in (401, 403, 451) or out["captcha"]:
            out["result"] = "refused or challenged: recorded, not retried"
            return out
        links = []
        for href, label in re.findall(r'(?is)<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', body):
            name = text_of(label)[:60]
            if LINK_HINT.search(href) or LINK_HINT.search(name):
                links.append(f"{name} -> {urllib.parse.urljoin(str(r.url), html.unescape(href))[:140]}")
        out["links"] = list(dict.fromkeys(links))[:12]
        out["result"] = "ok"
    return out


def sites() -> None:
    urls = [(group, u) for group, us in SITES.items() for u in us]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda gu: (gu[0], survey(gu[1])), urls))
    current = None
    counts = {"ok": 0, "challenged": 0, "unreachable": 0}
    for group, r in results:
        if group != current:
            print(f"\n## {group}")
            current = group
        res = r.get("result", "")
        counts["ok" if res == "ok" else "challenged" if res.startswith("refused") else "unreachable"] += 1
        print(f"- {r['url']} | {res} | HTTP {r.get('status', '-')} | robots: {r.get('robots')} | "
              f"captcha={r.get('captcha', '-')} sign-in={r.get('sign_in', '-')} | {r.get('title', '')}")
        for link in r.get("links", []):
            print(f"    link: {link}")
    print(f"\n== sites: {len(results)} visited — ok {counts['ok']}, challenged/refused {counts['challenged']}, "
          f"unreachable {counts['unreachable']}")


def archive() -> None:
    """Which official spreadsheet / CSV files does the Internet Archive hold? Index lookup only."""
    queries = ["open.data.gov.sa/*", "od.data.gov.sa/*", "data.gov.sa/*"]
    with httpx.Client(timeout=60, headers={"User-Agent": UA}) as c:
        for q in queries:
            params = {"url": q, "output": "json", "collapse": "urlkey", "limit": "400",
                      "filter": "mimetype:.*(spreadsheet|excel|csv|octet-stream).*",
                      "fl": "original,timestamp,mimetype,statuscode,length"}
            try:
                r = c.get("https://web.archive.org/cdx/search/cdx", params=params)
            except httpx.HTTPError as e:
                print(f"\n== archive {q}: unreachable {type(e).__name__}")
                continue
            if r.status_code != 200:
                print(f"\n== archive {q}: HTTP {r.status_code}")
                continue
            rows = r.json()[1:] if r.text.strip() else []
            hits = [x for x in rows if re.search(r"(tender|competition|contract|procure|منافس|عقود|مشتريات|ترسي)",
                                                 urllib.parse.unquote(x[0]), re.I)]
            print(f"\n== archive {q}: {len(rows)} spreadsheet/CSV captures, {len(hits)} about tenders/contracts")
            for x in (hits or rows)[:60]:
                print("   " + json.dumps(dict(zip(["url", "captured", "type", "status", "bytes"], x, strict=False)),
                                         ensure_ascii=False))


def main() -> int:
    if "--sites" in sys.argv:
        sites()
    if "--archive" in sys.argv:
        archive()
    return 0


if __name__ == "__main__":
    sys.exit(main())
