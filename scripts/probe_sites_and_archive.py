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


TOPIC = r"(?i).*(tender|contract|competition|procure|purchas|monafs|munafas|cr-number|compan|establishment|contractor|" \
        r"supplier|vendor|award|project|%D9%85%D9%86%D8%A7%D9%81%D8%B3).*"


def archive_topics(max_files: int = 14) -> None:
    """Archived open-data files about tenders / contracts / companies: dataset page facts, columns and row counts.

    Reads public archive copies in memory to describe their structure. Prints column names and counts only — never
    the rows — so nothing from the files is republished by this log.
    """
    import io
    with httpx.Client(timeout=90, headers={"User-Agent": UA}, follow_redirects=True) as c:
        params = {"url": "data.gov.sa", "matchType": "domain", "output": "json", "collapse": "urlkey", "limit": "3000",
                  "fl": "original,timestamp,mimetype,statuscode,length", "filter": ["statuscode:200", f"original:{TOPIC}"]}
        r = c.get("https://web.archive.org/cdx/search/cdx", params=params)
        rows = r.json()[1:] if r.status_code == 200 and r.text.strip() else []
        files = [x for x in rows if re.search(r"\.(xlsx|xls|csv)(\?|$)", x[0], re.I)]
        pages = {x[0] for x in rows if "/dataset/" in x[0] and "/resource/" not in x[0]}
        print(f"\n== archive topic search (data.gov.sa and subdomains): {len(rows)} captures, {len(files)} data files, "
              f"{len(pages)} dataset pages")
        for x in files[:80]:
            print("   file: " + json.dumps(dict(zip(["url", "captured", "type", "status", "bytes"], x, strict=False)),
                                         ensure_ascii=False))
        for original, ts, *_ in files[:max_files]:
            print(f"\n-- {urllib.parse.unquote(original)} (captured {ts})")
            m = re.search(r"/dataset/([0-9a-f-]{36})", original)
            if m:
                dataset_page(c, m.group(1))
            try:
                blob = c.get(f"https://web.archive.org/web/{ts}id_/{original}").content
            except httpx.HTTPError as e:
                print(f"   file: unreachable {type(e).__name__}")
                continue
            describe(blob, original, io)


def dataset_page(c: httpx.Client, dataset_id: str) -> None:
    params = {"url": f"data.gov.sa/Data/*/dataset/{dataset_id}", "output": "json", "limit": "5",
              "filter": ["statuscode:200", "mimetype:text/html"], "fl": "original,timestamp"}
    try:
        r = c.get("https://web.archive.org/cdx/search/cdx", params=params)
        hits = r.json()[1:] if r.status_code == 200 and r.text.strip() else []
        if not hits:
            print("   dataset page: not archived")
            return
        original, ts = hits[0]
        page = c.get(f"https://web.archive.org/web/{ts}id_/{original}").text
    except (httpx.HTTPError, ValueError) as e:
        print(f"   dataset page: unreachable {type(e).__name__}")
        return
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    org = re.search(r'(?is)class="[^"]*(?:organization|publisher)[^"]*"[^>]*>(.*?)</', page)
    lic = re.search(r"(?is)(license|licence|رخصة|الترخيص)[^<]{0,40}</[^>]+>\s*<[^>]+>(.{0,160}?)<", page)
    print(f"   dataset page ({ts}): title={text_of(title.group(1))[:120] if title else '-'} | "
          f"org={text_of(org.group(1))[:80] if org else '-'} | licence={text_of(lic.group(2))[:100] if lic else '-'}")


def describe(blob: bytes, name: str, io) -> None:
    print(f"   bytes={len(blob)}")
    if re.search(r"\.csv(\?|$)", name, re.I):
        for enc in ("utf-8-sig", "cp1256"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                text = ""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        print(f"   csv rows={max(len(lines) - 1, 0)} header={lines[0][:300] if lines else '-'}")
        return
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as e:
        print(f"   not a readable workbook ({type(e).__name__})")
        return
    for ws in wb.worksheets[:3]:
        count, header = 0, None
        for row in ws.iter_rows(values_only=True):
            if header is None and sum(v not in (None, "") for v in row) >= 2:
                header = [str(v)[:40] for v in row if v not in (None, "")]
            elif header is not None and any(v not in (None, "") for v in row):
                count += 1
        print(f"   sheet '{ws.title}': rows={count} header={header}")
    wb.close()


INSPECT = [  # archived official files whose structure decides whether they can feed the archive
    ("20211204002823", "https://data.gov.sa/Data/dataset/c0ffd6e6-e12d-4394-886b-fa9092f89daa/resource/"
                       "29e806d2-8d37-4503-b409-ce848e8a6351/download/procurement-data-2019-2020-2021.xls"),
    ("20170425235717", "http://www.data.gov.sa:80/sites/default/files/Contractors%20data%20report.xlsx"),
    ("20180727195621", "http://www.data.gov.sa:80/sites/default/files/CR%20Company%20Tayp.xlsx"),
]


def inspect_files() -> None:
    import io
    with httpx.Client(timeout=90, headers={"User-Agent": UA}, follow_redirects=True) as c:
        for ts, original in INSPECT:
            print(f"\n-- {urllib.parse.unquote(original)} (captured {ts})")
            m = re.search(r"/dataset/([0-9a-f-]{36})", original)
            if m:
                dataset_page(c, m.group(1))
            try:
                blob = c.get(f"https://web.archive.org/web/{ts}id_/{original}").content
            except httpx.HTTPError as e:
                print(f"   unreachable {type(e).__name__}")
                continue
            if original.lower().endswith(".xls"):
                import xlrd
                book = xlrd.open_workbook(file_contents=blob)
                for sh in book.sheets()[:3]:
                    header_row = next((r for r in range(min(sh.nrows, 10))
                                       if sum(str(v).strip() != "" for v in sh.row_values(r)) >= 2), 0)
                    header = [str(v)[:40] for v in sh.row_values(header_row) if str(v).strip()] if sh.nrows else []
                    print(f"   sheet '{sh.name}': rows={max(sh.nrows - header_row - 1, 0)} header={header}")
                    kinds = {}
                    for r in range(header_row + 1, min(sh.nrows, header_row + 200)):
                        for i, v in enumerate(sh.row_values(r)[:len(header)]):
                            kinds.setdefault(header[i] if i < len(header) else i, set()).add(type(v).__name__)
                    print(f"   value types (first 200 rows): { {k: sorted(v) for k, v in kinds.items()} }")
            else:
                describe(blob, original, io)


def main() -> int:
    if "--inspect" in sys.argv:
        inspect_files()
    if "--sites" in sys.argv:
        sites()
    if "--archive" in sys.argv:
        archive()
    if "--archive-topics" in sys.argv:
        archive_topics()
    return 0


if __name__ == "__main__":
    sys.exit(main())
