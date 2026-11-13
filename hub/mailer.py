"""Send queued e-mails (email_outbox) over SMTP. Works with any free SMTP plan (e.g. Brevo, Gmail app password).

Configured only by environment variables; without HUB_SMTP_HOST mails stay queued (visible to the admin).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import EmailOutbox


def smtp_configured() -> bool:
    return bool(os.environ.get("HUB_SMTP_HOST") and os.environ.get("HUB_SMTP_FROM"))


def send_pending(db: Session, limit: int = 50) -> dict:
    if not smtp_configured():
        return {"sent": 0, "skipped": "smtp_not_configured"}
    host = os.environ["HUB_SMTP_HOST"]
    port = int(os.environ.get("HUB_SMTP_PORT", "587"))
    user = os.environ.get("HUB_SMTP_USER", "")
    password = os.environ.get("HUB_SMTP_PASSWORD", "")
    sender = os.environ["HUB_SMTP_FROM"]
    rows = db.scalars(select(EmailOutbox).where(EmailOutbox.sent_at.is_(None)).order_by(EmailOutbox.id).limit(limit)).all()
    sent = failed = 0
    if not rows:
        return {"sent": 0, "failed": 0}
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            for r in rows:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = sender, r.to, r.subject
                msg.set_content(r.body)
                try:
                    s.send_message(msg)
                    r.sent_at = utcnow()
                    sent += 1
                except smtplib.SMTPException:
                    failed += 1
    except (OSError, smtplib.SMTPException) as e:
        db.commit()
        return {"sent": sent, "failed": failed, "error": type(e).__name__}
    db.commit()
    return {"sent": sent, "failed": failed}
