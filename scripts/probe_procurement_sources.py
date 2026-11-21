"""Log-only probe of official procurement sources: what they publish, under which terms, and whether a runner reaches them.

Run on a GitHub runner (procurement-probe.yml). For each page it prints the HTTP status, robots.txt verdict, the links
that look like datasets or downloads, and the full text of terms pages (bounded). A fixed, small list of URLs: no
crawling, no login, no form posts. A 401/403/451 is reported and never retried. Nothing is stored except the log.

With --browser, the pages marked "visitor" are also opened once in headless Chromium exactly as an anonymous visitor
would see them (no credentials, no cookies carried in); if the result is a login form, that is reported and we stop.

    python scripts/probe_procurement_sources.py [--browser]
"""
from __future__ import annotations

import html
import io
import re
import sys
import urllib.parse
import urllib.robotparser

import httpx

UA = "ghazi-hub-source-probe/1.2 (licence and terms check; log only)"
# (label, url, kind) kind: links = list dataset-like links; text = print the page text; pdf; api = raw JSON head;
# visitor = links + (with --browser) an anonymous headless visit
# Run 44862352265 (2026-11-21) settled the others: tenders.etimad.sa answers automated clients with a CAPTCHA
# (never retried, never worked around); apiportal.etimad.sa, open.data.gov.sa, od.data.gov.sa time out from runners;
# the Royal Commission hosts do not resolve. Only the portal.etimad.sa pages remain worth probing.
PAGES = [
    ("etimad open data library", "https://portal.etimad.sa/ar-sa/ecollaboration/indexopendatarequest", "visitor"),
    ("etimad open data library (en)", "https://portal.etimad.sa/en-us/ecollaboration/indexopendatarequest", "visitor"),
]
LINK_HINT = re.compile(r"(\.xlsx|\.xls|\.csv|\.json|\.zip|opendata|open-data|dataset|download|getattachment|"
                       r"tender|منافس|ترسي|عقود|بيانات|ترخيص|رخصة|licen)", re.I)
TEXT_LIMIT = 9000


def text_of(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup)
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
    if "text/plain" not in r.headers.get("content-type", ""):
        return f"robots.txt answered {r.headers.get('content-type', '?')[:30]} (not a robots file)"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    rules = [ln.strip() for ln in r.text.splitlines() if ln.strip().lower().startswith(("user-agent", "disallow", "allow"))]
    return f"allows this URL for our agent={rp.can_fetch(UA, url)}; rules: {' | '.join(rules[:15]) or '(none)'}"


def links_of(markup: str, base: str) -> list[str]:
    out = []
    for href, label_html in re.findall(r'(?is)<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', markup):
        name = text_of(label_html)[:80]
        if LINK_HINT.search(href) or LINK_HINT.search(name):
            out.append(f"{name} -> {urllib.parse.urljoin(base, html.unescape(href))}")
    return list(dict.fromkeys(out))[:50]


def pdf_text(blob: bytes) -> str:
    if not blob.startswith(b"%PDF"):
        return ""
    from pypdf import PdfReader
    return " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)


def probe_page(c: httpx.Client, label: str, url: str, kind: str) -> None:
    print(f"\n== {label}\n   {url}")
    print("   " + robots(c, url))
    r = c.get(url)
    final = str(r.url)
    print(f"   HTTP {r.status_code} {r.headers.get('content-type', '')[:50]} {len(r.content)}B final={final[:160]}")
    if r.status_code in (401, 403, 451):
        print("   refused: not retried")
        return
    if r.status_code != 200:
        return
    if "login." in urllib.parse.urlsplit(final).netloc or "/connect/authorize" in final:
        print("   redirected to a sign-in endpoint (OpenID); the plain HTTP client stops here")
        return
    if kind == "api":
        print("   " + r.text[:1500].replace("\n", " "))
        return
    if kind == "pdf":
        body = pdf_text(r.content)
        if not body:
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
            print(f"   not a PDF (title: {text_of(title.group(1)) if title else '-'}); text head: {text_of(r.text)[:600]}")
            return
        print(f"   pdf text ({len(body)} chars): {body[:TEXT_LIMIT]}")
        return
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
    body = text_of(r.text)
    print(f"   title: {text_of(title.group(1)) if title else '-'}; text chars={len(body)}")
    for link in links_of(r.text, final):
        print("   link: " + link)
    if kind == "text":
        print(f"   text: {body[:TEXT_LIMIT]}")


def browser_visit(url: str) -> None:
    """One anonymous headless visit, as a member of the public would make it. No credentials, no form filling."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(locale="ar-SA", user_agent=UA).new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:  # the portal keeps background requests open; settle briefly, then read whatever rendered
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as e:
            print(f"   [browser] still loading after 20s ({type(e).__name__}); reading what rendered")
        final = page.url
        print(f"   [browser] HTTP {resp.status if resp else '?'} final={final[:160]}")
        has_password = page.locator("input[type=password]").count() > 0
        if has_password or "login." in urllib.parse.urlsplit(final).netloc:
            print("   [browser] a sign-in form is shown: content is not public to anonymous visitors; stopping")
            b.close()
            return
        body = re.sub(r"\s+", " ", page.inner_text("body"))
        print(f"   [browser] title: {page.title()}; text chars={len(body)}")
        print(f"   [browser] text: {body[:TEXT_LIMIT]}")
        for link in links_of(page.content(), final):
            print("   [browser] link: " + link)
        b.close()


def main() -> int:
    use_browser = "--browser" in sys.argv
    with httpx.Client(timeout=30, headers={"User-Agent": UA, "Accept-Language": "ar,en"}, follow_redirects=True) as c:
        for label, url, kind in PAGES:
            try:
                probe_page(c, label, url, kind)
            except Exception as e:  # one page failing must not hide the others
                print(f"   error: {type(e).__name__}: {str(e)[:200]}")
            if use_browser and kind == "visitor":
                try:
                    browser_visit(url)
                except Exception as e:
                    print(f"   [browser] error: {type(e).__name__}: {str(e)[:200]}")
    print("\nNothing stored. A source becomes automatic only after its terms are recorded in docs/SOURCES_MATRIX.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
