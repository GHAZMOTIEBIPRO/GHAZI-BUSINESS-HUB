"""Passwords, sessions, CSRF and rate limiting. Standard library only."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, utcnow
from .models import AuthSession, User

SESSION_COOKIE = "ghub_session"
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(test, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    t = secrets.token_urlsafe(32)
    return t, _h(t)


def create_session(db: Session, user: User) -> str:
    token, th = new_token()
    db.add(AuthSession(token_hash=th, user_id=user.id, csrf=secrets.token_urlsafe(24),
                       expires_at=utcnow() + dt.timedelta(days=settings.session_days)))
    db.commit()
    return token


def destroy_session(db: Session, token: str | None):
    if token:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _h(token)))
        if s:
            db.delete(s)
            db.commit()


def load_session(db: Session, request: Request) -> AuthSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _h(token)))
    if not s or s.expires_at < utcnow() or not s.user.is_active:
        return None
    return s


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    s = load_session(db, request)
    request.state.session = s
    return s.user if s else None


class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = load_session(db, request)
    request.state.session = s
    if not s:
        raise LoginRequired()
    return s.user


class TwoFactorSetupRequired(Exception):
    pass


class EmailVerificationRequired(Exception):
    pass


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="غير مصرح")
        if user.email_verified_at is None:
            raise EmailVerificationRequired()
        if settings.require_staff_2fa and not user.totp_secret:
            raise TwoFactorSetupRequired()
        return user
    return dep


def csrf_token(request: Request) -> str:
    s = getattr(request.state, "session", None)
    if s is not None:
        return s.csrf
    # anonymous forms (login/register) use a signed double-submit token
    anon = request.cookies.get("ghub_anon") or getattr(request.state, "anon", "")
    return hmac.new(settings.secret_key.encode(), anon.encode(), hashlib.sha256).hexdigest()


async def verify_csrf(request: Request, db: Session = Depends(get_db)):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if getattr(request.state, "session", None) is None:
        request.state.session = load_session(db, request)  # independent of route dependency order
    form = await request.form()
    sent = str(form.get("csrf") or request.headers.get("x-csrf-token") or "")
    expected = csrf_token(request)
    if not sent or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="انتهت صلاحية النموذج، أعد تحميل الصفحة")


def constant_eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """Sliding-window, per-process limiter. Behind several workers use a shared store (documented)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window_s: int):
    if not limiter.check(f"{bucket}:{client_ip(request)}", limit, window_s):
        raise HTTPException(status_code=429, detail="محاولات كثيرة، حاول بعد قليل")
