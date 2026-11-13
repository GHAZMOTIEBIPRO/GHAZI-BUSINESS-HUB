"""Log-only probe of the Saudi national open data portal for datasets that could feed the radar lawfully.

Run on a GitHub runner (sources-probe.yml): the portal resets connections from some cloud egress.
It reads robots.txt, then asks the portal's CKAN search API (if present) for tender / contract / project datasets and
prints each dataset's publisher, licence, last update and file formats. A small fixed number of requests,
no scraping of pages, nothing stored, and a 401/403/451 is reported, never retried.

    python scripts/probe_open_data.py
"""
from __future__ import annotations

import json
import sys
import urllib.robotparser

import httpx

HOSTS = ["https://od.data.gov.sa", "https://open.data.gov.sa"]
API_PATHS = ["/data/api/3/action/package_search", "/api/3/action/package_search"]
QUERIES = ["منافسات", "مناقصات", "عقود", "مشاريع", "tenders", "contracts"]
UA = {"User-Agent": "ghazi-hub-source-probe/1.0 (open-data licence check; log only)"}


def robots_ok(client: httpx.Client, host: str, path: str) -> tuple[bool, str]:
    try:
        r = client.get(host + "/robots.txt")
    except httpx.HTTPError as e:
        return False, f"robots.txt unreachable: {type(e).__name__}"
    if r.status_code == 404:
        return True, "no robots.txt (404)"
    if r.status_code != 200:
        return False, f"robots.txt HTTP {r.status_code}"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(UA["User-Agent"], host + path), "robots.txt read"


def main() -> int:
    found = 0
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        for host in HOSTS:
            print(f"\n== {host}")
            for path in API_PATHS:
                ok, why = robots_ok(c, host, path)
                print(f"  {path}: robots allows={ok} ({why})")
                if not ok:
                    continue
                try:
                    r = c.get(host + path, params={"q": QUERIES[0], "rows": 1})
                except httpx.HTTPError as e:
                    print(f"  {path}: unreachable ({type(e).__name__})")
                    continue
                print(f"  {path}: HTTP {r.status_code} {r.headers.get('content-type', '')[:40]}")
                if r.status_code in (401, 403, 451):
                    print("  refused: not retried")
                    break
                if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                    continue
                for q in QUERIES:
                    data = c.get(host + path, params={"q": q, "rows": 10}).json().get("result", {})
                    print(f"\n  query '{q}': {data.get('count', 0)} datasets")
                    for d in data.get("results", []):
                        found += 1
                        fmts = sorted({(x.get("format") or "?").upper() for x in d.get("resources", [])})
                        print("   - " + json.dumps({
                            "title": d.get("title"), "publisher": (d.get("organization") or {}).get("title"),
                            "licence": d.get("license_title") or d.get("license_id"), "updated": d.get("metadata_modified"),
                            "formats": fmts, "url": f"{host}/dataset/{d.get('name')}"}, ensure_ascii=False))
                break
    print(f"\n{found} candidate datasets listed. A dataset becomes a radar source only after its licence and terms are "
          "recorded in rights_evidence (see docs/STATUS.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
