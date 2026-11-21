"""Log-only probe of official procurement sources: what they publish, under which terms, and whether a runner reaches them.

Run on a GitHub runner (sources-probe.yml). For each page it prints the HTTP status, robots.txt verdict, the links
that look like datasets or downloads, and the sentences of terms pages that mention reuse, copying, automated access
or commercial use. A fixed, small list of URLs: no crawling, no login, no form posts. A 401/403/451 is reported and
never retried. Nothing is stored except the log.

    python scripts/probe_procurement_sources.py
"""
from __future__ import annotations

import html
import io
import re
import sys
import urllib.parse
import urllib.robotparser

import httpx

UA = "ghazi-hub-source-probe/1.1 (licence and terms check; log only)"
PAGES = [
    # (label, url, kind) kind: page = list links; terms = print clauses; pdf = terms in a PDF; api = JSON probe
    ("etimad open data page", "https://portal.etimad.sa/ar-sa/ecollaboration/indexopendatarequest", "page"),
    ("etimad open data policy", "https://portal.etimad.sa/ar-SA/simplepage/simplepagedetail?PageGuid=a2b884b7-3d01-4518-90f6-a244f3977731", "terms"),
    ("etimad usage policy (pdf)", "https://tenders.etimad.sa/StaticFIles/AboutMonafasat/" + urllib.parse.quote("سياسة الاستخدام.pdf"), "pdf"),
    ("etimad tenders for visitors", "https://tenders.etimad.sa/Tender/AllTendersForVisitor", "page"),
    ("etimad api products", "https://apiportal.etimad.sa/en/api_products", "page"),
    ("etimad api terms", "https://apiportal.etimad.sa/ar/terms-and-conditions", "terms"),
    ("etimad contracts plus", "https://apiportal.etimad.sa/en/api_products/ContractsPlus", "terms"),
    ("open data (new platform)", "https://open.data.gov.sa/", "page"),
    ("open data api (new platform)", "https://open.data.gov.sa/api/3/action/package_search?q=%D9%85%D9%86%D8%A7%D9%81%D8%B3%D8%A7%D8%AA&rows=20", "api"),
    ("open data (old portal)", "https://od.data.gov.sa/ar/government-procurement", "page"),
    ("rc yanbu", "https://www.rcyanbu.gov.sa/", "page"),
    ("rcjy", "https://www.rcjy.gov.sa/", "page"),
]
LINK_HINT = re.compile(r"(\.xlsx|\.xls|\.csv|\.json|\.zip|opendata|open-data|dataset|download|getattachment|"
                       r"tender|منافس|ترسي|عقود|بيانات)", re.I)
CLAUSE_HINT = re.compile(r"(نسخ|إعادة|اعادة|نشر|آلي|الآلي|برمجي|تجاري|ترخيص|رخصة|روابط|استخدام البيانات|"
                         r"copy|reproduc|redistribut|scrap|crawl|automat|commercial|licen[cs]e|resell|reuse)", re.I)


def text_of(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))).strip()


def robots(client: httpx.Client, url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    try:
        r = client.get(root + "/robots.txt")
    except httpx.HTTPError as e:
        return f"robots.txt unreachable ({type(e).__name__})"
    if r.status_code == 404:
        return "no robots.txt (404)"
    if r.status_code != 200:
        return f"robots.txt HTTP {r.status_code}"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    rules = [ln.strip() for ln in r.text.splitlines() if ln.strip().lower().startswith(("user-agent", "disallow", "allow"))]
    return f"allows this URL for our agent={rp.can_fetch(UA, url)}; rules: {' | '.join(rules[:12])}"


def clauses(text: str, limit: int = 25) -> list[str]:
    out = []
    for sentence in re.split(r"(?<=[.!؟?։።])\s+|\s+(?=\d+\s*[-–.)])", text):
        s = sentence.strip()
        if 20 < len(s) < 600 and CLAUSE_HINT.search(s):
            out.append(s)
        if len(out) >= limit:
            break
    return out


def pdf_text(blob: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    return " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)


def main() -> int:
    with httpx.Client(timeout=30, headers={"User-Agent": UA, "Accept-Language": "ar,en"}, follow_redirects=True) as c:
        for label, url, kind in PAGES:
            print(f"\n== {label}\n   {url}")
            print("   " + robots(c, url))
            try:
                r = c.get(url)
            except httpx.HTTPError as e:
                print(f"   unreachable: {type(e).__name__}: {str(e)[:160]}")
                continue
            print(f"   HTTP {r.status_code} {r.headers.get('content-type', '')[:50]} {len(r.content)}B final={r.url}")
            if r.status_code in (401, 403, 451):
                print("   refused: not retried")
                continue
            if r.status_code != 200:
                continue
            if kind == "api":
                print("   " + r.text[:1500].replace("\n", " "))
                continue
            if kind == "pdf":
                body = pdf_text(r.content)
                print(f"   pdf text chars={len(body)}")
                for s in clauses(body):
                    print("   > " + s)
                continue
            body = text_of(r.text)
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
            print(f"   title: {text_of(title.group(1)) if title else '-'}; text chars={len(body)}")
            links = []
            for href, label_html in re.findall(r'(?is)<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', r.text):
                name = text_of(label_html)[:80]
                if LINK_HINT.search(href) or LINK_HINT.search(name):
                    links.append(f"{name} -> {urllib.parse.urljoin(str(r.url), html.unescape(href))}")
            for link in list(dict.fromkeys(links))[:40]:
                print("   link: " + link)
            if kind == "terms":
                for s in clauses(body):
                    print("   > " + s)
    print("\nNothing stored. A source becomes automatic only after its terms are recorded in docs/SOURCES_MATRIX.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
