# Third-party components and licences

| Component | Version | Licence | Use |
|---|---|---|---|
| FastAPI | 0.118.0 | MIT | web framework |
| Starlette (via FastAPI) | — | BSD-3 | ASGI toolkit |
| Uvicorn | 0.37.0 | BSD-3 | ASGI server |
| Jinja2 | 3.1.6 | BSD-3 | server-side templates (autoescape on) |
| python-multipart | 0.0.20 | Apache-2.0 | form parsing |
| SQLAlchemy | 2.0.43 | MIT | ORM |
| openpyxl | 3.1.5 | MIT | workbook generation |
| httpx | 0.28.1 | BSD-3 | outbound HTTP (sources, Moyasar) |
| defusedxml | 0.7.1 | PSF | safe RSS parsing (XXE / entity bombs) |
| IBM Plex Sans Arabic (Fontsource 5.2.5) | — | SIL OFL 1.1 | self-hosted font, licence in `hub/static/fonts/LICENSE-OFL.txt` |
| PyJWT | 2.10.1 | MIT | verifies GitHub Actions OIDC tokens for the scheduled refresh (`hub/oidc.py`) |
| cryptography | 45.0.7 | Apache-2.0 / BSD | RS256 signature checks for PyJWT |
| pytest / ruff | 8.4.2 / 0.13.3 | MIT | dev only |
| Playwright + Chromium | preinstalled | Apache-2.0 | UI tests only |
| LibreOffice (headless) | system | MPL-2.0 | build-time recalculation + tests only; not shipped |

Not used on purpose: no CSS/JS framework, no CDN, no analytics or third-party trackers (privacy + CSP `'self'` only).
No code or data from the GHAZIBOT stock project is used.
