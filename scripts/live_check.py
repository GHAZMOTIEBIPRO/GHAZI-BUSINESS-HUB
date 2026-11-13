"""Acceptance checks against a DEPLOYED instance over HTTPS (run from GitHub Actions, see live-check.yml).

    python scripts/live_check.py https://ghazi-business-hub-dd3h.onrender.com [--write] [--out results.json]

Read-only by default. With --write it creates two throw-away accounts (live-check-*@example.com), proves one cannot
read the other's data or download a paid file, then deletes both accounts. It never touches opportunities, orders
or payments, and prints no cookie, token or secret.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time

import httpx

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
RESULTS: list[dict] = []


def check(name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "evidence": evidence[:300]})
    print(("PASS " if ok else "FAIL ") + name + (f" — {evidence[:300]}" if evidence else ""), flush=True)
    return ok


def csrf(c: httpx.Client, path: str) -> str:
    m = CSRF_RE.search(c.get(path).text)
    return m.group(1) if m else ""


def wake(base: str) -> dict | None:
    """Free instances sleep; the first request can take about a minute."""
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/healthz", timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404 and r.headers.get("x-render-routing") == "no-server":
                LAST["why"] = "Render has no service at this address yet (x-render-routing: no-server)"
                return None
            LAST["why"] = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            LAST["why"] = type(e).__name__
        time.sleep(10)
    return None


LAST = {"why": ""}


def read_only(base: str, health: dict) -> None:
    check("HTTPS URL answers /healthz", health is not None and health.get("ok") is True,
          json.dumps(health) if health else LAST["why"])
    if not health:
        return
    check("database engine is PostgreSQL (not SQLite fallback)", health.get("db") == "postgresql", f"db={health.get('db')}")
    check("environment is staging (no production mode)", health.get("env") == "staging", f"env={health.get('env')}")
    last, started = health.get("last_refresh_at"), health.get("started_at")
    if "--require-refresh" in sys.argv:
        check("a scheduled refresh has completed and is stored", bool(last), f"last_refresh_at={last}")
    if last and started:
        # informational unless required: a refresh stored before this process started proves data survived a restart
        RESULTS.append({"check": "data outlived a restart (refresh stored before current process start)",
                        "ok": last < started, "evidence": f"last_refresh_at={last} started_at={started}", "info": True})
        print(("PASS " if last < started else "INFO ") + RESULTS[-1]["check"] + " — " + RESULTS[-1]["evidence"])
    c = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
    for path, marker in [("/", "غازي"), ("/radar", "رادار"), ("/store", "متجر"), ("/pricing", "الاشتراكات"),
                         ("/sources", "المصادر"), ("/calculator", ""), ("/login", "دخول"), ("/register", "حساب")]:
        r = c.get(path)
        check(f"page {path} opens", r.status_code == 200 and marker in r.text, f"status={r.status_code}")
    r = c.get("/")
    h = {k.lower(): v for k, v in r.headers.items()}
    check("security headers (CSP, HSTS or https-only cookies, nosniff, frame deny)",
          "content-security-policy" in h and h.get("x-content-type-options") == "nosniff" and "x-frame-options" in h,
          ", ".join(k for k in ("content-security-policy", "strict-transport-security", "x-frame-options", "referrer-policy") if k in h))
    cookie = h.get("set-cookie", "")
    if base.startswith("https://"):
        check("HSTS header over HTTPS", "strict-transport-security" in h, h.get("strict-transport-security", ""))
        check("cookies are Secure + HttpOnly over HTTPS", "secure" in cookie.lower() and "httponly" in cookie.lower(), cookie.split("=")[0])
    r = c.get("/samples/finishing-pricing.xlsx")
    check("free sample downloads as a real .xlsx", r.status_code == 200 and r.content[:2] == b"PK", f"status={r.status_code} bytes={len(r.content)}")
    check("anonymous user cannot open a download link", c.get("/dl/not-a-real-token").status_code in (303, 401, 403), "")
    check("anonymous user is sent away from /admin", c.get("/admin").status_code in (303, 401, 403, 404), "")
    check("paid file is not served from /static", c.get("/static/../products/dist/full/01_finishing_pricing.xlsx").status_code in (400, 404), "")
    r = c.get("/sources")
    check("sources page shows the automatic-update log", 'id="jobRuns"' in r.text, "")


def write_checks(base: str) -> None:
    tag = secrets.token_hex(4)
    users = []
    for who in ("a", "b"):
        c = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
        email, pw = f"live-check-{tag}-{who}@example.com", secrets.token_urlsafe(18)
        r = c.post("/register", data={"csrf": csrf(c, "/register"), "name": f"فحص آلي {who}", "email": email,
                                      "password": pw, "agree": "1"})
        check(f"register independent account {who}", r.status_code == 303, f"status={r.status_code}")
        users.append((c, pw))
    (a, pw_a), (b, pw_b) = users
    r = a.post("/quotes", data={"csrf": csrf(a, "/quotes")})
    loc = r.headers.get("location", "")
    check("account a creates a private quote", r.status_code == 303 and loc.startswith("/quotes/"), loc)
    if loc.startswith("/quotes/"):
        check("account b cannot read account a's quote (IDOR)", b.get(loc).status_code == 404, f"GET {loc} as b")
        check("account b cannot export account a's quote", b.get(loc + "/xlsx").status_code == 404, "")
        check("account a can read its own quote", a.get(loc).status_code == 200, "")
    r = b.post("/downloads/finishing-pricing", data={"csrf": csrf(b, "/account/orders")})
    check("paid file refused without entitlement", r.status_code == 303 and "/dl/" not in r.headers.get("location", ""),
          r.headers.get("location", "")[:80])
    check("anonymous cannot read a quote", httpx.get(base + (loc or "/quotes/1"), timeout=60).status_code in (303, 401, 403), "")
    for i, (c, pw) in enumerate(users):
        r = c.post("/account/delete", data={"csrf": csrf(c, "/account/profile"), "password": pw})
        check(f"clean up: test account {'ab'[i]} deleted", r.status_code == 303 and r.headers.get("location", "").startswith("/"), "")


def main(argv: list[str]) -> int:
    if not argv or not argv[0].startswith(("https://", "http://127.0.0.1")):  # http only for a local dry run
        print(__doc__)
        return 2
    base = argv[0].rstrip("/")
    out = argv[argv.index("--out") + 1] if "--out" in argv else ""
    health = wake(base)
    read_only(base, health)
    if health and "--write" in argv:
        write_checks(base)
    required = [r for r in RESULTS if not r.get("info")]
    passed = sum(r["ok"] for r in required)
    print(f"\n{passed}/{len(required)} required checks passed against {base}")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"base": base, "health": health, "results": RESULTS}, f, ensure_ascii=False, indent=1)
    return 0 if required and passed == len(required) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
