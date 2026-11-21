"""Log-only reachability and robots check of every host in hub/data/sources_directory.json.

Run on a GitHub runner (sources-probe.yml) and locally. For each distinct host it makes at most two requests:
GET /robots.txt and GET of the directory's terms URL (status only, body discarded). A 401/403/451 is reported
as a refusal and never retried. Nothing is stored; the output is the evidence a reviewer uses to update
`rights_checked_at` / `rights_evidence` by hand.

    python scripts/probe_sources.py [--json out.json]
"""
from __future__ import annotations

import json
import sys
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
UA = "ghazi-hub-source-probe/1.0 (terms and robots check; log only)"
REFUSALS = {401, 403, 451}


def classify(status: int | None, err: str, final_url: str, start_url: str) -> str:
    if status is None:
        return f"unreachable ({err})"
    if status in REFUSALS:
        return f"refused ({status}) — not retried"
    if urlsplit(final_url).netloc != urlsplit(start_url).netloc and "login" in final_url:
        return f"login required (redirected to {urlsplit(final_url).netloc})"
    if 200 <= status < 300:
        return "ok"
    return f"http {status}"


def fetch(c: httpx.Client, url: str) -> tuple[int | None, str, str, str]:
    try:
        r = c.get(url)
        return r.status_code, "", str(r.url), r.text if r.status_code == 200 else ""
    except httpx.HTTPError as e:
        return None, type(e).__name__, url, ""


def main(argv: list[str]) -> int:
    data = json.loads((ROOT / "hub" / "data" / "sources_directory.json").read_text(encoding="utf-8"))
    out_path = argv[argv.index("--json") + 1] if "--json" in argv else ""
    results = []
    robots_cache: dict[str, dict] = {}
    with httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as c:
        for s in data["sources"]:
            host = urlsplit(s["terms_url"]).netloc
            if host not in robots_cache:
                status, err, final, body = fetch(c, f"https://{host}/robots.txt")
                info = {"status": status, "verdict": classify(status, err, final, f"https://{host}/"), "disallow_all": False,
                        "terms_allowed": True}
                if status == 200 and body:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(body.splitlines())
                    info["disallow_all"] = not rp.can_fetch("*", f"https://{host}/")
                    info["terms_allowed"] = rp.can_fetch("*", s["terms_url"])
                robots_cache[host] = info
            robots = robots_cache[host]
            if robots["status"] in REFUSALS or not robots["terms_allowed"]:
                t_status, t_err, t_final = None, "skipped (robots refused or disallows)", s["terms_url"]
            else:
                t_status, t_err, t_final, _ = fetch(c, s["terms_url"])
            row = {"key": s["key"], "host": host, "rights": s["rights"], "evidence_status": s["evidence_status"],
                   "rights_checked_at": s["rights_checked_at"], "robots": robots["verdict"],
                   "terms": classify(t_status, t_err, t_final, s["terms_url"]) if t_err != "skipped (robots refused or disallows)"
                   else t_err}
            results.append(row)
            print(f"{row['key']:<22} {host:<28} robots={row['robots']:<32} terms={row['terms']}")
    reachable = sum(r["terms"] == "ok" for r in results)
    print(f"\n{reachable}/{len(results)} terms pages readable from this network; refusals are final, unreachable hosts "
          "may be geo-restricted (check from inside the Kingdom).")
    if out_path:
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
