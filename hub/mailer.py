"""Send queued e-mails (email_outbox) through an HTTPS e-mail API.

Render Free blocks outbound SMTP (ports 25/465/587), so only HTTPS APIs are supported:
  HUB_MAIL_PROVIDER = brevo  -> POST https://api.brevo.com/v3/smtp/email   (header: api-key)
  HUB_MAIL_PROVIDER = resend -> POST https://api.resend.com/emails          (header: Authorization: Bearer)
  HUB_MAIL_API_KEY, HUB_MAIL_FROM (a sender verified with the provider), HUB_MAIL_FROM_NAME (optional)
Without a provider, messages stay queued and visible to the admin. Nothing is ever sent by SMTP.
"""
from __future__ import annotations

import os

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import EmailOutbox

MAX_ATTEMPTS = 5
TRANSPORT: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here; None = real network
ENDPOINTS = {"brevo": "https://api.brevo.com/v3/smtp/email", "resend": "https://api.resend.com/emails"}


def provider() -> str | None:
    p = os.environ.get("HUB_MAIL_PROVIDER", "").strip().lower()
    if p in ENDPOINTS and os.environ.get("HUB_MAIL_FROM") and os.environ.get("HUB_MAIL_API_KEY"):
        return p
    return None


def _request(p: str, row: EmailOutbox) -> tuple[str, dict, dict]:
    key = os.environ["HUB_MAIL_API_KEY"]
    sender = os.environ["HUB_MAIL_FROM"]
    name = os.environ.get("HUB_MAIL_FROM_NAME", "غازي للأعمال")
    if p == "brevo":
        return ENDPOINTS[p], {"api-key": key, "accept": "application/json"}, {
            "sender": {"email": sender, "name": name}, "to": [{"email": row.to}],
            "subject": row.subject, "textContent": row.body}
    return ENDPOINTS[p], {"Authorization": f"Bearer {key}"}, {
        "from": f"{name} <{sender}>", "to": [row.to], "subject": row.subject, "text": row.body}


def send_pending(db: Session, limit: int = 50, transport: httpx.BaseTransport | None = None) -> dict:
    p = provider()
    if not p:
        return {"sent": 0, "skipped": "mail_provider_not_configured"}
    rows = db.scalars(select(EmailOutbox).where(EmailOutbox.sent_at.is_(None), EmailOutbox.attempts < MAX_ATTEMPTS)
                      .order_by(EmailOutbox.id).limit(limit)).all()
    sent = failed = 0
    with httpx.Client(timeout=20, transport=transport or TRANSPORT) as client:
        for row in rows:
            url, headers, payload = _request(p, row)
            row.attempts += 1
            try:
                r = client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as e:
                row.last_error = f"network: {type(e).__name__}"
                failed += 1
                continue
            if r.status_code in (200, 201, 202):
                row.sent_at = utcnow()
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                row.provider_message_id = str(body.get("messageId") or body.get("id") or "")[:200] or None
                row.last_error = ""
                sent += 1
            else:
                # never store the key; keep a short provider message for the admin
                row.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                failed += 1
                if r.status_code in (401, 403):
                    break  # bad key or unverified sender: stop, don't burn attempts on every row
    db.commit()
    return {"sent": sent, "failed": failed, "provider": p}
