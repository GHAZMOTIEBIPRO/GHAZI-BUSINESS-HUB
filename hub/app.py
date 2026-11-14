"""GHAZI Business Hub — web application (FastAPI + server-rendered Arabic RTL pages)."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import directory, jobs, mailer, oidc, quotes, radar, store, totp
from .config import settings
from .db import get_db, init_db, utcnow
from .models import (
    CITIES,
    OPPORTUNITY_TYPES,
    PIPELINE_STAGES,
    PLANS,
    REVIEW_STATES,
    SOURCE_RIGHTS,
    TRADES,
    AuditLog,
    AuthSession,
    ContactLog,
    DownloadLog,
    EmailOutbox,
    EmailVerification,
    Entitlement,
    ErrorReport,
    FetchRun,
    JobRun,
    Notification,
    Opportunity,
    Order,
    PasswordReset,
    PipelineItem,
    Product,
    QuoteDraft,
    Source,
    Subscription,
    User,
)
from .security import (
    SESSION_COOKIE,
    EmailVerificationRequired,
    LoginRequired,
    TwoFactorSetupRequired,
    client_ip,
    constant_eq,
    create_session,
    csrf_token,
    current_user,
    current_user_optional,
    destroy_session,
    hash_password,
    rate_limit,
    rate_limit_key,
    require_role,
    verify_csrf,
    verify_password,
)

HERE = Path(__file__).resolve().parent
RIYADH = ZoneInfo("Asia/Riyadh")
app = FastAPI(title="GHAZI Business Hub", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
STARTED_AT = utcnow()  # process start: lets an outside check see that stored data outlived a restart
OIDC_KEY_RESOLVER = None  # test hook; None = GitHub's published key set


# ----------------------------------------------------------------- template helpers
def ksa(d: dt.datetime | None, fmt="%Y-%m-%d") -> str:
    if not d:
        return "—"
    return d.replace(tzinfo=dt.UTC).astimezone(RIYADH).strftime(fmt)


def ago(d: dt.datetime | None) -> str:
    if not d:
        return "غير معروف"
    days = (utcnow() - d).days
    if days <= 0:
        return "اليوم"
    if days == 1:
        return "أمس"
    return f"قبل {days} يوم"


def money(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


templates.env.globals.update(ksa=ksa, ago=ago, money=money, TRADES=TRADES, CITIES=CITIES, OPP_TYPES=OPPORTUNITY_TYPES,
                             STAGES=PIPELINE_STAGES, PLANS=PLANS, REVIEW_STATES=REVIEW_STATES, SOURCE_RIGHTS=SOURCE_RIGHTS,
                             MATCH_LABELS=radar.MATCH_LABELS, HEALTH_LABELS=radar.HEALTH_LABELS, settings=settings,
                             contact_ok=radar.contact_ok, now_utc=utcnow, public_summary=radar.public_summary,
                             summary_publishable=radar.summary_publishable)


def render(request: Request, name: str, db: Session, user: User | None, status=200, **ctx):
    unread = 0
    if user:
        unread = db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id,
                                                                     Notification.read.is_(False)))
    ctx.update(request=request, user=user, plan=store.active_plan(db, user), csrf=csrf_token(request), unread=unread,
               path=request.url.path)
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def redirect(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg})
    return RedirectResponse(url, status_code=303)


def safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else "/dashboard"


# ----------------------------------------------------------------- lifecycle & middleware
@app.on_event("startup")
def _startup():
    init_db()
    from .db import SessionLocal

    with SessionLocal() as db:
        jobs.seed(db)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    new_anon = None
    if not request.cookies.get("ghub_anon"):
        new_anon = secrets.token_urlsafe(16)
        request.state.anon = new_anon  # so CSRF tokens on this first page match the cookie set below
    response = await call_next(request)
    h = response.headers
    h["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                    "font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; "
                                    "object-src 'none'")
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.secure_cookies:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if new_anon:
        response.set_cookie("ghub_anon", new_anon, httponly=True, samesite="lax",
                            secure=settings.secure_cookies, max_age=86400 * 30)
    return response


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return redirect("/login?" + urlencode({"next": request.url.path}))


@app.exception_handler(TwoFactorSetupRequired)
async def _twofa_required(request: Request, exc: TwoFactorSetupRequired):
    return redirect("/account/2fa", "حسابات الإدارة تتطلب تفعيل التحقق بخطوتين أولًا")


@app.exception_handler(EmailVerificationRequired)
async def _email_required(request: Request, exc: EmailVerificationRequired):
    return redirect("/account/verify", "أكّد بريدك الإلكتروني أولًا لإتمام هذه العملية")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/payments/", "/internal/")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"request": request, "code": exc.status_code,
                                                             "detail": exc.detail, "user": None, "plan": {}, "csrf": "",
                                                             "unread": 0, "path": request.url.path},
                                      status_code=exc.status_code)


def set_session_cookie(resp: Response, token: str):
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=settings.secure_cookies,
                    max_age=settings.session_days * 86400, path="/")


# ----------------------------------------------------------------- public pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    latest = db.scalars(public_opps_query().order_by(Opportunity.verified_at.desc()).limit(4)).all()
    stats = {
        "opps": db.scalar(select(func.count()).select_from(public_opps_query().subquery())),
        "products": db.scalar(select(func.count(Product.id)).where(Product.status == "ready")),
        "sources": len(directory.sources()),
    }
    products = db.scalars(select(Product).where(Product.status == "ready").limit(4)).all()
    return render(request, "home.html", db, user, latest=latest, stats=stats, products=products)


def public_opps_query():
    return select(Opportunity).where(Opportunity.review_state == "approved", Opportunity.duplicate_of_id.is_(None))


@app.get("/radar", response_class=HTMLResponse)
def radar_list(request: Request, q: str = "", city: str = "", region: str = "", trade: str = "", type: str = "",
               show: str = "open", page: int = 1, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    stmt = public_opps_query()
    if q.strip():
        like = f"%{q.strip()[:80]}%"
        # search facts only: matching on withheld text would leak what we may not republish
        stmt = stmt.where(or_(Opportunity.title.ilike(like),
                              Opportunity.owner_entity.ilike(like), Opportunity.main_contractor.ilike(like)))
    if city in CITIES:
        stmt = stmt.where(Opportunity.city == city)
    if region in set(CITIES.values()):
        stmt = stmt.where(Opportunity.region == region)
    if type in OPPORTUNITY_TYPES:
        stmt = stmt.where(Opportunity.opportunity_type == type)
    if show != "all":
        stmt = stmt.where(Opportunity.opportunity_type != "CLOSED")
    rows = db.scalars(stmt.order_by(Opportunity.verified_at.desc())).all()
    if trade in TRADES:  # JSON list column: filter in Python (portable across SQLite/Postgres)
        rows = [o for o in rows if trade in (o.trades or [])]
    my_trades = user.trades if user else []
    my_cities = user.cities if user else []
    scored = [(o, *radar.match(o, my_trades, my_cities)) for o in rows]
    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    if user:
        scored.sort(key=lambda x: order[x[1]])
    per = 20
    page = max(1, page)
    total = len(scored)
    saved_ids = set()
    if user:
        saved_ids = set(db.scalars(select(PipelineItem.opportunity_id).where(PipelineItem.user_id == user.id)))
    f = {"q": q, "city": city, "region": region, "trade": trade, "type": type, "show": show}
    return render(request, "radar.html", db, user, items=scored[(page - 1) * per: page * per], total=total, page=page,
                  pages=max(1, -(-total // per)), f=f, qs=urlencode({k: v for k, v in f.items() if v and k != "show" or v == "all"}),
                  regions=sorted(set(CITIES.values())), saved_ids=saved_ids)


def get_public_opp(db: Session, opp_id: int) -> Opportunity:
    o = db.get(Opportunity, opp_id)
    if not o or o.review_state != "approved":
        raise HTTPException(404, "الفرصة غير موجودة أو لم تُعتمد بعد")
    return o


@app.get("/radar/{opp_id}", response_class=HTMLResponse)
def radar_detail(opp_id: int, request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    o = get_public_opp(db, opp_id)
    level, reasons = radar.match(o, user.trades if user else [], user.cities if user else [])
    pipe = None
    if user:
        pipe = db.scalar(select(PipelineItem).where(PipelineItem.user_id == user.id, PipelineItem.opportunity_id == o.id))
    tools = [p for p in db.scalars(select(Product).where(Product.status == "ready")).all()
             if not p.trades or set(p.trades) & set(o.trades or [])][:3]
    return render(request, "radar_detail.html", db, user, o=o, level=level, reasons=reasons, pipe=pipe, tools=tools,
                  show_contact=store.active_plan(db, user)["radar_contacts"] and radar.contact_ok(o.contact))


@app.post("/radar/{opp_id}/report", dependencies=[Depends(verify_csrf)])
def radar_report(opp_id: int, request: Request, category: str = Form(...), details: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(current_user_optional)):
    rate_limit(request, "report", 5, 3600)
    o = get_public_opp(db, opp_id)
    if category not in {"closed", "wrong_contact", "wrong_data", "rights", "other"}:
        raise HTTPException(400, "تصنيف غير صالح")
    db.add(ErrorReport(opportunity_id=o.id, user_id=user.id if user else None, category=category, details=details[:2000]))
    db.commit()
    return redirect(f"/radar/{o.id}", "شكرًا، أُرسل البلاغ للمراجعة")


@app.get("/sources", response_class=HTMLResponse)
def sources_public(request: Request, city: str = "all", cat: str = "", db: Session = Depends(get_db),
                   user=Depends(current_user_optional)):
    now = utcnow()
    city = city if city in directory.FOCUS_CITIES else "all"  # anything else = no city filter
    cat = cat if cat in directory.categories() else ""
    rows = [(s, radar.source_health(s, now))
            for s in db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.id)).all()]
    entries = directory.sources(city, cat or None)
    counts = {c: len(directory.sources(c)) for c in ["all", *directory.FOCUS_CITIES]}
    return render(request, "sources.html", db, user, rows=rows, job_runs=jobs.last_job_runs(db), entries=entries,
                  categories=directory.categories(), city=city, cat=cat, counts=counts, focus_cities=directory.FOCUS_CITIES,
                  EVIDENCE_STATUS=directory.EVIDENCE_STATUS, AUTOMATION=directory.AUTOMATION,
                  RIGHTS_BADGE=directory.RIGHTS_BADGE, total=len(directory.sources()))


@app.get("/store", response_class=HTMLResponse)
def store_list(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    products = db.scalars(select(Product).order_by(Product.id)).all()
    owned = {p.id for p in products if user and store.can_download(db, user, p)}
    return render(request, "store.html", db, user, products=products, owned=owned)


def get_product(db: Session, slug: str) -> Product:
    p = db.scalar(select(Product).where(Product.slug == slug))
    if not p:
        raise HTTPException(404, "المنتج غير موجود")
    return p


@app.get("/store/{slug}", response_class=HTMLResponse)
def product_page(slug: str, request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    p = get_product(db, slug)
    b, v, t = store.price_breakdown(p.price) if p.price is not None else (None, None, None)
    return render(request, "product.html", db, user, p=p, owned=bool(user and store.can_download(db, user, p)),
                  price=(b, v, t))


@app.get("/samples/{slug}.xlsx")
def sample_download(slug: str, db: Session = Depends(get_db)):
    p = get_product(db, slug)
    if p.status != "ready" or not p.file_stem:
        raise HTTPException(404, "لا توجد عينة")
    path = settings.sample_dir / f"{p.file_stem}.xlsx"
    if not path.is_file():
        raise HTTPException(404, "لا توجد عينة")
    # ASCII file names: some browsers drop non-ASCII names and save as "download" without .xlsx
    return FileResponse(path, filename=f"ghazi-{p.slug}-sample.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    return render(request, "pricing.html", db, user, breakdown={k: store.price_breakdown(v["price"]) for k, v in PLANS.items()})


@app.get("/calculator", response_class=HTMLResponse)
def calculator(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    return render(request, "calculator.html", db, user)


# ----------------------------------------------------------------- auth
@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    return render(request, "register.html", db, user, error=None, form={})


@app.post("/register", dependencies=[Depends(verify_csrf)])
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
             company: str = Form(""), agree: str = Form(""), db: Session = Depends(get_db)):
    rate_limit(request, "register", 10, 3600)
    rate_limit_key("register-global", 200, 3600)  # caps mass sign-ups (and verification mail) from forged IPs
    email = email.strip().lower()
    form = {"name": name, "email": email, "company": company}
    err = None
    if not EMAIL_RE.match(email):
        err = "بريد إلكتروني غير صالح"
    elif len(password) < 10:
        err = "كلمة المرور 10 أحرف على الأقل"
    elif not agree:
        err = "يجب الموافقة على الشروط وسياسة الخصوصية"
    elif db.scalar(select(User).where(User.email == email)):
        err = "يوجد حساب بهذا البريد"
    if err:
        return render(request, "register.html", db, None, status=400, error=err, form=form)
    u = User(email=email, name=name.strip()[:120], company=company.strip()[:160], password_hash=hash_password(password),
             trades=[], cities=[])
    db.add(u)
    db.commit()
    issue_verification(db, u)
    resp = redirect("/account/profile", "أهلًا بك! أرسلنا رابط تأكيد إلى بريدك. اختر الآن تخصصاتك ومدنك")
    set_session_cookie(resp, create_session(db, u))
    return resp


VERIFY_TTL = dt.timedelta(hours=48)


def issue_verification(db: Session, user: User) -> None:
    token = secrets.token_urlsafe(32)
    db.add(EmailVerification(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, email=user.email,
                             expires_at=utcnow() + VERIFY_TTL))
    db.add(EmailOutbox(to=user.email, subject="تأكيد بريدك — غازي للأعمال",
                       body=f"مرحبًا {user.name}،\nلتأكيد بريدك خلال 48 ساعة افتح الرابط:\n{settings.base_url}/verify/{token}\n"
                            "إن لم تنشئ حسابًا لدينا فتجاهل هذه الرسالة."))
    db.commit()
    mailer.send_pending(db)


@app.get("/verify/{token}")
def verify_email(token: str, db: Session = Depends(get_db)):
    row = db.scalar(select(EmailVerification).where(
        EmailVerification.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    user = db.get(User, row.user_id) if row else None
    if not row or row.used_at or row.expires_at < utcnow() or not user or user.email != row.email:
        raise HTTPException(400, "رابط التأكيد غير صالح أو منتهي — اطلب رابطًا جديدًا من حسابك")
    row.used_at = utcnow()
    user.email_verified_at = user.email_verified_at or utcnow()
    db.add(AuditLog(actor_id=user.id, action="email.verified", target=f"user:{user.id}"))
    db.commit()
    return redirect("/dashboard", "تم تأكيد بريدك")


@app.get("/account/verify", response_class=HTMLResponse)
def verify_status(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "verify.html", db, user)


@app.post("/account/verify/resend", dependencies=[Depends(verify_csrf)])
def verify_resend(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rate_limit(request, "verify_resend", 3, 3600)
    if user.email_verified_at:
        return redirect("/dashboard", "بريدك مؤكد مسبقًا")
    issue_verification(db, user)
    return redirect("/account/verify", "أُرسل رابط جديد")


def require_verified(user: User):
    if user.email_verified_at is None:
        raise EmailVerificationRequired()


# ----------------------------------------------------------------- first admin (one-time, secret code)
def setup_open(db: Session) -> bool:
    return len(settings.setup_token) >= 32 and db.scalar(select(User.id).where(User.role == "admin").limit(1)) is None


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)):
    if not setup_open(db):
        raise HTTPException(404, "الصفحة غير موجودة")
    return render(request, "setup.html", db, None, error=None)


@app.post("/setup", dependencies=[Depends(verify_csrf)])
def setup(request: Request, setup_code: str = Form(...), name: str = Form(...), email: str = Form(...),
          password: str = Form(...), db: Session = Depends(get_db)):
    rate_limit(request, "setup", 5, 3600)
    rate_limit_key("setup-global", 10, 3600)  # one site-wide budget: the setup code cannot be brute-forced
    if not setup_open(db):
        raise HTTPException(404, "الصفحة غير موجودة")
    if not constant_eq(setup_code.strip(), settings.setup_token):
        db.add(AuditLog(action="setup.bad_code", target=client_ip(request)))
        db.commit()
        raise HTTPException(403, "رمز الإعداد غير صحيح")
    email = email.strip().lower()
    err = None
    if not EMAIL_RE.match(email):
        err = "بريد غير صالح"
    elif len(password) < 14:
        err = "كلمة مرور المدير 14 حرفًا على الأقل"
    elif db.scalar(select(User.id).where(User.email == email)):
        # never promote an existing account: its owner has not been proven to be the operator
        err = "هذا البريد مسجل بحساب قائم؛ استخدم بريدًا جديدًا لحساب الإدارة"
    if err:
        return render(request, "setup.html", db, None, status=400, error=err)
    u = User(email=email, name=name.strip()[:120] or "المدير", role="admin", password_hash=hash_password(password),
             trades=[], cities=[])
    db.add(u)
    db.add(AuditLog(action="setup.admin_created", target=email))
    db.commit()
    if mailer.provider() is None:
        # no e-mail service yet: the one-time setup code (only visible in the host's dashboard) is the operator's proof.
        # Recorded so it can be reviewed; every other account still has to confirm its address by e-mail.
        u.email_verified_at = utcnow()
        db.add(AuditLog(action="setup.verified_by_setup_code", target=email, detail="no mail provider configured"))
        db.commit()
        resp = redirect("/account/2fa", "أُنشئ حساب الإدارة. فعّل التحقق بخطوتين الآن (لم يُضبط مزود البريد بعد)")
        set_session_cookie(resp, create_session(db, u))
        return resp
    issue_verification(db, u)
    resp = redirect("/account/verify", "أُنشئ حساب الإدارة. أكّد بريدك ثم فعّل التحقق بخطوتين")
    set_session_cookie(resp, create_session(db, u))
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "", db: Session = Depends(get_db), user=Depends(current_user_optional)):
    return render(request, "login.html", db, user, error=None, next=safe_next(next))


@app.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""),
          otp: str = Form(""), db: Session = Depends(get_db)):
    rate_limit(request, "login", 10, 900)
    rate_limit_key(f"login-account:{email.strip().lower()[:254]}", 20, 900)  # forged IPs cannot reset this
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not u or not u.is_active or not verify_password(password, u.password_hash):
        return render(request, "login.html", db, None, status=400, error="بيانات الدخول غير صحيحة", next=safe_next(next))
    if u.totp_secret:
        step = totp.verify(u.totp_secret, otp, u.totp_last_step)
        if step is None:
            return render(request, "login.html", db, None, status=400, next=safe_next(next),
                          error="أدخل رمز التحقق الصحيح من تطبيق المصادقة")
        u.totp_last_step = step
        db.commit()
    resp = redirect(safe_next(next))
    set_session_cookie(resp, create_session(db, u))
    return resp


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    return render(request, "forgot.html", db, user, sent=False)


@app.post("/forgot", dependencies=[Depends(verify_csrf)])
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    rate_limit(request, "forgot", 5, 3600)
    rate_limit_key(f"forgot-account:{email.strip().lower()[:254]}", 3, 3600)
    u = db.scalar(select(User).where(User.email == email.strip().lower(), User.is_active.is_(True)))
    if u:
        token = secrets.token_urlsafe(32)
        db.add(PasswordReset(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=u.id,
                             expires_at=utcnow() + dt.timedelta(hours=1)))
        db.add(EmailOutbox(to=u.email, subject="إعادة تعيين كلمة المرور — غازي للأعمال",
                           body=f"لإعادة تعيين كلمة المرور خلال ساعة:\n{settings.base_url}/reset/{token}\n"
                                "إن لم تطلب ذلك فتجاهل هذه الرسالة."))
        db.commit()
        mailer.send_pending(db)
    # same answer whether or not the e-mail exists (no account enumeration)
    return render(request, "forgot.html", db, None, sent=True)


def _reset_row(db: Session, token: str) -> PasswordReset | None:
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not row or row.used_at or row.expires_at < utcnow():
        return None
    return row


@app.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    if not _reset_row(db, token):
        raise HTTPException(400, "الرابط غير صالح أو منتهي")
    return render(request, "reset.html", db, None, token=token, error=None)


@app.post("/reset/{token}", dependencies=[Depends(verify_csrf)])
def reset(token: str, request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    row = _reset_row(db, token)
    if not row:
        raise HTTPException(400, "الرابط غير صالح أو منتهي")
    if len(password) < 10:
        return render(request, "reset.html", db, None, status=400, token=token, error="كلمة المرور 10 أحرف على الأقل")
    u = db.get(User, row.user_id)
    u.password_hash = hash_password(password)
    u.email_verified_at = u.email_verified_at or utcnow()  # following the e-mailed link proves the address
    row.used_at = utcnow()
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
        db.delete(s)  # sign out everywhere
    db.add(AuditLog(actor_id=u.id, action="password.reset", target=f"user:{u.id}"))
    db.commit()
    return redirect("/login", "تم تغيير كلمة المرور، سجّل الدخول")


@app.get("/account/2fa", response_class=HTMLResponse)
def twofa_form(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    pending = None
    if not user.totp_secret:
        pending = request.cookies.get("ghub_2fa_pending") or totp.new_secret()
    resp = render(request, "twofa.html", db, user, pending=pending,
                  uri=totp.provisioning_uri(pending, user.email) if pending else None, error=None)
    if pending:
        resp.set_cookie("ghub_2fa_pending", pending, httponly=True, samesite="strict", secure=settings.secure_cookies,
                        max_age=900, path="/account/2fa")
    return resp


@app.post("/account/2fa", dependencies=[Depends(verify_csrf)])
def twofa_enable(request: Request, code: str = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    pending = request.cookies.get("ghub_2fa_pending")
    step = totp.verify(pending, code) if pending else None
    if step is None:
        return render(request, "twofa.html", db, user, status=400, pending=pending,
                      uri=totp.provisioning_uri(pending, user.email) if pending else None,
                      error="الرمز غير صحيح. تأكد من ضبط وقت جهازك.")
    user.totp_secret, user.totp_last_step = pending, step
    db.add(AuditLog(actor_id=user.id, action="2fa.enabled", target=f"user:{user.id}"))
    db.commit()
    resp = redirect("/dashboard", "فُعّل التحقق بخطوتين")
    resp.delete_cookie("ghub_2fa_pending", path="/account/2fa")
    return resp


@app.post("/account/2fa/disable", dependencies=[Depends(verify_csrf)])
def twofa_disable(password: str = Form(...), code: str = Form(...), db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    if not user.totp_secret or not verify_password(password, user.password_hash) or \
            totp.verify(user.totp_secret, code, user.totp_last_step) is None:
        return redirect("/account/2fa", "تعذر الإلغاء: تحقق من كلمة المرور والرمز")
    user.totp_secret, user.totp_last_step = None, 0
    db.add(AuditLog(actor_id=user.id, action="2fa.disabled", target=f"user:{user.id}"))
    db.commit()
    return redirect("/account/2fa", "أُلغي التحقق بخطوتين")


@app.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    resp = redirect("/")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ----------------------------------------------------------------- contractor workspace
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    opps = db.scalars(public_opps_query().where(Opportunity.opportunity_type != "CLOSED")
                      .order_by(Opportunity.verified_at.desc()).limit(200)).all()
    matches = [(o, *radar.match(o, user.trades, user.cities)) for o in opps]
    matches = [m for m in matches if m[1] in ("high", "medium")][:6]
    stage_counts = dict(db.execute(select(PipelineItem.stage, func.count(PipelineItem.id))
                                   .where(PipelineItem.user_id == user.id).group_by(PipelineItem.stage)).all())
    upcoming = db.scalars(select(PipelineItem).where(PipelineItem.user_id == user.id,
                                                     PipelineItem.next_action_at.is_not(None))
                          .order_by(PipelineItem.next_action_at).limit(5)).all()
    notes = db.scalars(select(Notification).where(Notification.user_id == user.id)
                       .order_by(Notification.created_at.desc()).limit(5)).all()
    return render(request, "dashboard.html", db, user, matches=matches, stage_counts=stage_counts, upcoming=upcoming,
                  notes=notes, needs_profile=not user.trades)


@app.get("/account/profile", response_class=HTMLResponse)
def profile_form(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "profile.html", db, user)


@app.post("/account/profile", dependencies=[Depends(verify_csrf)])
async def profile_save(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    user.trades = [t for t in form.getlist("trades") if t in TRADES]
    user.cities = [c for c in form.getlist("cities") if c in CITIES]
    user.alerts_enabled = bool(form.get("alerts"))
    user.name = str(form.get("name") or user.name).strip()[:120]
    user.company = str(form.get("company") or "").strip()[:160]
    db.commit()
    return redirect("/dashboard", "حُفظت تفضيلاتك")


@app.get("/account/export")
def account_export(db: Session = Depends(get_db), user: User = Depends(current_user)):
    def rows(model, *cols):
        return [{c: (getattr(r, c).isoformat() if isinstance(getattr(r, c), dt.datetime) else getattr(r, c)) for c in cols}
                for r in db.scalars(select(model).where(model.user_id == user.id))]
    data = {
        "exported_at": utcnow().isoformat() + "Z",
        "profile": {"email": user.email, "name": user.name, "company": user.company, "trades": user.trades,
                    "cities": user.cities, "created_at": user.created_at.isoformat()},
        "pipeline": rows(PipelineItem, "opportunity_id", "stage", "notes", "created_at"),
        "contact_logs": rows(ContactLog, "pipeline_id", "channel", "outcome", "at"),
        "quotes": rows(QuoteDraft, "id", "client", "project", "lines", "discount", "vat_rate"),
        "orders": rows(Order, "id", "kind", "plan", "product_id", "total", "status", "invoice_number", "created_at"),
        "downloads": rows(DownloadLog, "product_id", "at"),
    }
    return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=ghazi-hub-my-data.json"})


@app.post("/account/delete", dependencies=[Depends(verify_csrf)])
def account_delete(request: Request, password: str = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    if not verify_password(password, user.password_hash):
        return redirect("/account/profile", "كلمة المرور غير صحيحة")
    if db.scalar(select(func.count(Order.id)).where(Order.user_id == user.id, Order.status == "paid")):
        # paid orders are kept for tax-record retention; personal data is anonymised
        user.email = f"deleted-{user.id}-{secrets.token_hex(4)}@deleted.invalid"
        user.name, user.company, user.is_active = "حساب محذوف", "", False
        user.password_hash = "!"  # noqa: S105 - not a password: an unusable hash that can never verify
        for model in (PipelineItem, QuoteDraft, Notification, AuthSession):
            for r in db.scalars(select(model).where(model.user_id == user.id)):
                db.delete(r)
    else:
        db.delete(user)
    db.add(AuditLog(action="account.deleted", target=f"user:{user.id}"))
    db.commit()
    resp = redirect("/", "حُذف حسابك")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)
                      .order_by(Notification.created_at.desc()).limit(100)).all()
    resp = render(request, "notifications.html", db, user, rows=rows)
    for r in rows:
        r.read = True
    db.commit()
    return resp


# ----------------------------------------------------------------- pipeline (radar -> work)
def own_pipe(db: Session, user: User, pipe_id: int) -> PipelineItem:
    p = db.get(PipelineItem, pipe_id)
    if not p or p.user_id != user.id:  # IDOR guard: same 404 for missing and foreign rows
        raise HTTPException(404, "غير موجود")
    return p


@app.post("/radar/{opp_id}/save", dependencies=[Depends(verify_csrf)])
def save_opp(opp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    o = get_public_opp(db, opp_id)
    p = db.scalar(select(PipelineItem).where(PipelineItem.user_id == user.id, PipelineItem.opportunity_id == o.id))
    if not p:
        p = PipelineItem(user_id=user.id, opportunity_id=o.id)
        db.add(p)
        db.commit()
    return redirect(f"/pipeline/{p.id}", "حُفظت الفرصة في متابعتك")


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(PipelineItem).where(PipelineItem.user_id == user.id)
                      .order_by(PipelineItem.updated_at.desc())).all()
    return render(request, "pipeline.html", db, user, rows=rows)


@app.get("/pipeline/{pipe_id}", response_class=HTMLResponse)
def pipeline_item(pipe_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own_pipe(db, user, pipe_id)
    logs = db.scalars(select(ContactLog).where(ContactLog.pipeline_id == p.id, ContactLog.user_id == user.id)
                      .order_by(ContactLog.at.desc())).all()
    qs = db.scalars(select(QuoteDraft).where(QuoteDraft.pipeline_id == p.id, QuoteDraft.user_id == user.id)).all()
    tools = [x for x in db.scalars(select(Product).where(Product.status == "ready")).all()
             if x.slug in ("finishing-pricing", "quotation", "execution-tracking", "progress-invoices")]
    return render(request, "pipeline_item.html", db, user, p=p, logs=logs, quotes=qs, tools=tools)


@app.post("/pipeline/{pipe_id}/update", dependencies=[Depends(verify_csrf)])
def pipeline_update(pipe_id: int, stage: str = Form(...), notes: str = Form(""), next_action_at: str = Form(""),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own_pipe(db, user, pipe_id)
    if stage not in PIPELINE_STAGES:
        raise HTTPException(400, "مرحلة غير صالحة")
    p.stage, p.notes = stage, notes[:4000]
    try:
        p.next_action_at = dt.datetime.strptime(next_action_at, "%Y-%m-%d") if next_action_at else None
    except ValueError:
        p.next_action_at = None
    p.updated_at = utcnow()
    db.commit()
    return redirect(f"/pipeline/{p.id}", "حُدّثت الحالة")


@app.post("/pipeline/{pipe_id}/log", dependencies=[Depends(verify_csrf)])
def pipeline_log(pipe_id: int, channel: str = Form(...), outcome: str = Form(...), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    p = own_pipe(db, user, pipe_id)
    if channel not in {"phone", "email", "visit", "platform", "other"}:
        raise HTTPException(400, "قناة غير صالحة")
    db.add(ContactLog(user_id=user.id, pipeline_id=p.id, channel=channel, outcome=outcome.strip()[:2000]))
    p.updated_at = utcnow()
    db.commit()
    return redirect(f"/pipeline/{p.id}", "سُجّلت نتيجة التواصل")


@app.post("/pipeline/{pipe_id}/quote", dependencies=[Depends(verify_csrf)])
def pipeline_quote(pipe_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own_pipe(db, user, pipe_id)
    q = QuoteDraft(user_id=user.id, pipeline_id=p.id, client=p.opportunity.main_contractor or p.opportunity.owner_entity or "",
                   project=p.opportunity.title[:300], lines=quotes.starter_lines(p.opportunity.trades),
                   vat_rate=settings.vat_rate)
    db.add(q)
    if p.stage in ("saved", "studying"):
        p.stage = "pricing"
    db.commit()
    return redirect(f"/quotes/{q.id}", "أُنشئت مسودة عرض سعر — أدخل الكميات والأسعار")


def own_quote(db: Session, user: User, qid: int) -> QuoteDraft:
    q = db.get(QuoteDraft, qid)
    if not q or q.user_id != user.id:
        raise HTTPException(404, "غير موجود")
    return q


@app.get("/quotes", response_class=HTMLResponse)
def quote_list(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(QuoteDraft).where(QuoteDraft.user_id == user.id).order_by(QuoteDraft.updated_at.desc())).all()
    return render(request, "quotes.html", db, user, rows=rows)


@app.post("/quotes", dependencies=[Depends(verify_csrf)])
def quote_create(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = QuoteDraft(user_id=user.id, lines=quotes.starter_lines([]), vat_rate=settings.vat_rate)
    db.add(q)
    db.commit()
    return redirect(f"/quotes/{q.id}")


@app.get("/quotes/{qid}", response_class=HTMLResponse)
def quote_view(qid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = own_quote(db, user, qid)
    return render(request, "quote.html", db, user, q=q, t=quotes.totals(q), max_lines=quotes.MAX_LINES)


@app.post("/quotes/{qid}", dependencies=[Depends(verify_csrf)])
async def quote_save(qid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = own_quote(db, user, qid)
    form = await request.form()
    q.client = str(form.get("client", ""))[:200]
    q.project = str(form.get("project", ""))[:300]
    q.lines = quotes.parse_lines(form)
    q.discount = quotes.num(form.get("discount"), 0)
    q.vat_rate = min(max(quotes.num(form.get("vat_rate"), 15) / 100, 0), 1)
    q.validity_days = int(min(max(quotes.num(form.get("validity_days"), 15), 1), 365))
    q.updated_at = utcnow()
    db.commit()
    return redirect(f"/quotes/{q.id}", "حُفظت المسودة")


@app.get("/quotes/{qid}/xlsx")
def quote_xlsx(qid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = own_quote(db, user, qid)
    data = quotes.to_xlsx(q, user)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="ghazi-quote-{q.id}.xlsx"',
                             "Cache-Control": "no-store"})


# ----------------------------------------------------------------- purchases & downloads
@app.post("/buy/product/{slug}", dependencies=[Depends(verify_csrf)])
def buy_product(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rate_limit(request, "buy", 20, 3600)
    require_verified(user)
    try:
        order = store.create_order(db, user, product=get_product(db, slug))
    except store.StoreError as e:
        return redirect(f"/store/{slug}", str(e))
    db.commit()
    return redirect(f"/checkout/{order.id}")


@app.post("/buy/plan/{plan}", dependencies=[Depends(verify_csrf)])
def buy_plan(plan: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rate_limit(request, "buy", 20, 3600)
    require_verified(user)
    try:
        order = store.create_order(db, user, plan=plan)
    except store.StoreError as e:
        return redirect("/pricing", str(e))
    db.commit()
    return redirect(f"/checkout/{order.id}")


def own_order(db: Session, user: User, oid: int) -> Order:
    o = db.get(Order, oid)
    if not o or o.user_id != user.id:
        raise HTTPException(404, "غير موجود")
    return o


@app.get("/checkout/{oid}", response_class=HTMLResponse)
def checkout(oid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    o = own_order(db, user, oid)
    if o.status != "pending":
        return redirect(f"/orders/{o.id}")
    return render(request, "checkout.html", db, user, o=o, sandbox_ok=sandbox_allowed(user))


def sandbox_allowed(user: User | None) -> bool:
    return bool(user) and (settings.sandbox_checkout_open or user.role in ("admin", "reviewer"))


@app.post("/checkout/{oid}/sandbox", dependencies=[Depends(verify_csrf)])
def checkout_sandbox(oid: int, result: str = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    if settings.payment_provider != "sandbox" or settings.env == "production":
        raise HTTPException(404, "غير متاح")
    if not sandbox_allowed(user):
        raise HTTPException(403, "الدفع التجريبي متاح لفريق المنصة فقط في هذه البيئة")
    o = own_order(db, user, oid)
    body = store.sandbox_event(o, "paid" if result == "paid" else "failed")
    store.handle_payment_event(db, "sandbox", store.verify_sandbox(body, store.sandbox_sign(body)))
    return redirect(f"/orders/{o.id}")


@app.post("/payments/webhook/sandbox")
async def webhook_sandbox(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    try:
        vp = store.verify_sandbox(body, request.headers.get("x-signature", ""))
    except (store.StoreError, ValueError, KeyError):
        return JSONResponse({"error": "invalid"}, status_code=400)
    return {"outcome": store.handle_payment_event(db, "sandbox", vp)}


@app.post("/payments/webhook/moyasar")
async def webhook_moyasar(request: Request, db: Session = Depends(get_db)):
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "invalid"}, status_code=400)
    if not constant_eq(str(payload.get("secret_token", "")), settings.moyasar_webhook_token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    data = payload.get("data") or {}
    try:
        vp = store.verify_moyasar(str(data.get("id", "")), str(payload.get("id", "")))
    except store.StoreError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"outcome": store.handle_payment_event(db, "moyasar", vp)}


@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_view(oid: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "order.html", db, user, o=own_order(db, user, oid))


@app.get("/account/orders", response_class=HTMLResponse)
def my_orders(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc())).all()
    products = [p for p in db.scalars(select(Product).where(Product.status == "ready")).all()
                if store.can_download(db, user, p)]
    used = {p.id: store.downloads_used(db, user, p) for p in products}
    return render(request, "orders.html", db, user, rows=rows, products=products, used=used,
                  sub=store.active_subscription(db, user))


@app.post("/downloads/{slug}", dependencies=[Depends(verify_csrf)])
def download_token(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rate_limit(request, "dl", 30, 3600)
    try:
        token = store.issue_download_token(db, user, get_product(db, slug))
    except store.StoreError as e:
        return redirect("/account/orders", str(e))
    db.commit()
    return redirect(f"/dl/{token}")


@app.get("/dl/{token}")
def download_file(token: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        p = store.redeem_download_token(db, user, token)
    except store.StoreError as e:
        db.rollback()
        raise HTTPException(403, str(e)) from e
    db.commit()
    path = (settings.product_dir / f"{p.file_stem}.xlsx").resolve()
    if not path.is_file() or settings.product_dir.resolve() not in path.parents:
        raise HTTPException(404, "الملف غير متوفر")
    return FileResponse(path, filename=f"ghazi-{p.slug}.xlsx", headers={"Cache-Control": "no-store"},
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ----------------------------------------------------------------- admin
staff = require_role("admin", "reviewer")
admin_only = require_role("admin")


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    now = utcnow()
    since = now - dt.timedelta(days=1)
    stats = {
        "pending": db.scalar(select(func.count(Opportunity.id)).where(Opportunity.review_state == "pending_review")),
        "stale": db.scalar(select(func.count(Opportunity.id)).where(Opportunity.review_state == "stale")),
        "approved": db.scalar(select(func.count(Opportunity.id)).where(Opportunity.review_state == "approved")),
        "new_24h": db.scalar(select(func.count(Opportunity.id)).where(Opportunity.first_seen_at >= since)),
        "reports": db.scalar(select(func.count(ErrorReport.id)).where(ErrorReport.status == "open")),
        "users": db.scalar(select(func.count(User.id))),
        "paid_orders": db.scalar(select(func.count(Order.id)).where(Order.status == "paid")),
        "outbox": db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.sent_at.is_(None))),
    }
    sources = [(s, radar.source_health(s, now)) for s in db.scalars(select(Source)).all()]
    runs = db.scalars(select(FetchRun).order_by(FetchRun.id.desc()).limit(15)).all()
    return render(request, "admin_home.html", db, user, stats=stats, sources=sources, runs=runs)


@app.get("/admin/review", response_class=HTMLResponse)
def admin_review(request: Request, state: str = "pending_review", db: Session = Depends(get_db),
                 user: User = Depends(staff)):
    state = state if state in REVIEW_STATES else "pending_review"
    rows = db.scalars(select(Opportunity).where(Opportunity.review_state == state)
                      .order_by(Opportunity.first_seen_at.desc()).limit(100)).all()
    reports = db.scalars(select(ErrorReport).where(ErrorReport.status == "open").order_by(ErrorReport.id.desc())).all()
    return render(request, "admin_review.html", db, user, rows=rows, state=state, reports=reports)


@app.get("/admin/opportunities/new", response_class=HTMLResponse)
def admin_opp_form(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    sources = db.scalars(select(Source).where(Source.kind == "manual")).all()
    return render(request, "admin_opp_form.html", db, user, sources=sources, error=None, o=None)


def _opp_from_form(form) -> dict:
    raw = {k: (str(form.get(k)).strip() if form.get(k) else None) for k in
           ("title", "summary", "city", "owner_entity", "main_contractor", "responsible_company", "url",
            "published_at", "deadline_at", "opportunity_type")}
    raw["trades"] = form.getlist("trades")
    # the reviewer must attest the summary is in their own words; otherwise it is treated as source text
    raw["summary_origin"] = "reviewer" if form.get("summary_own_words") else "source"
    if form.get("contact_name") or form.get("contact_email") or form.get("contact_phone"):
        raw["contact"] = {"public_business_name": form.get("contact_name") or None, "role": form.get("contact_role") or None,
                          "public_business_email": form.get("contact_email") or None,
                          "public_business_phone": form.get("contact_phone") or None,
                          "source_url": form.get("contact_source_url") or None,
                          "verified_at": utcnow().isoformat() if form.get("contact_source_url") else None,
                          "display_allowed": bool(form.get("contact_display_allowed"))}
    if form.get("evidence_url"):
        raw["evidence"] = [{"url": form.get("evidence_url"), "checked_at": utcnow().isoformat(),
                            "finding": str(form.get("evidence_finding") or "")[:500]}]
    return raw


@app.post("/admin/opportunities/new", dependencies=[Depends(verify_csrf)])
async def admin_opp_create(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    form = await request.form()
    src = db.get(Source, int(form.get("source_id") or 0))
    if not src or src.kind != "manual":
        raise HTTPException(400, "مصدر غير صالح")
    raw = _opp_from_form(form)
    raw["external_id"] = f"manual-{secrets.token_hex(6)}"
    n = radar.normalise(raw)
    if not n["title"] or not n["source_url"]:
        sources = db.scalars(select(Source).where(Source.kind == "manual")).all()
        return render(request, "admin_opp_form.html", db, user, status=400, sources=sources, o=None,
                      error="العنوان ورابط المصدر (HTTPS) إلزاميان")
    radar.upsert(db, src, [raw])
    db.add(AuditLog(actor_id=user.id, action="opportunity.manual_create", target=n["title"][:120]))
    db.commit()
    return redirect("/admin/review", "أُضيفت الفرصة إلى قائمة المراجعة")


IMPORT_COLUMNS = ["title", "url", "city", "owner_entity", "main_contractor", "responsible_company", "trades",
                  "opportunity_type", "published_at", "deadline_at", "summary", "summary_is_own_words", "evidence_url",
                  "evidence_finding"]
IMPORT_MAX_BYTES = 2_000_000
IMPORT_MAX_ROWS = 1000


@app.get("/admin/import", response_class=HTMLResponse)
def admin_import_form(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    sources = db.scalars(select(Source).where(Source.kind == "manual")).all()
    return render(request, "admin_import.html", db, user, sources=sources, columns=IMPORT_COLUMNS, result=None, error=None)


@app.post("/admin/import", dependencies=[Depends(verify_csrf)])
async def admin_import(request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    form = await request.form()
    sources = db.scalars(select(Source).where(Source.kind == "manual")).all()
    src = db.get(Source, int(form.get("source_id") or 0))
    upload = form.get("file")

    def fail(msg):
        return render(request, "admin_import.html", db, user, status=400, sources=sources, columns=IMPORT_COLUMNS,
                      result=None, error=msg)
    if not src or src.kind != "manual":
        return fail("اختر مصدرًا يدويًا")
    if upload is None or not hasattr(upload, "read"):
        return fail("أرفق ملف CSV")
    raw = await upload.read(IMPORT_MAX_BYTES + 1)
    if len(raw) > IMPORT_MAX_BYTES:
        return fail("الملف أكبر من 2 ميجابايت")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return fail("احفظ الملف بترميز UTF-8")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"title", "url"} <= set(reader.fieldnames):
        return fail("الأعمدة الإلزامية: title و url")
    rows = []
    for i, r in enumerate(reader):
        if i >= IMPORT_MAX_ROWS:
            return fail(f"الحد الأقصى {IMPORT_MAX_ROWS} صف في الملف الواحد")
        r = {k: (v or "").strip() for k, v in r.items() if k in IMPORT_COLUMNS}
        rec = {**r, "trades": [t.strip() for t in r.get("trades", "").split("|") if t.strip()],
               "external_id": "csv-" + hashlib.sha256(f"{r.get('url')}|{r.get('title')}".encode()).hexdigest()[:24],
               "summary_origin": "reviewer" if r.get("summary_is_own_words", "").lower() in {"1", "true", "yes", "نعم"} else "source"}
        if r.get("evidence_url"):
            rec["evidence"] = [{"url": r["evidence_url"], "checked_at": utcnow().isoformat(),
                                "finding": r.get("evidence_finding", "")[:500]}]
        rows.append(rec)
    st = radar.upsert(db, src, rows)
    db.add(AuditLog(actor_id=user.id, action="opportunity.csv_import", target=src.key,
                    detail=f"seen={st.seen} new={st.new} updated={st.updated} skipped={st.skipped}"))
    db.commit()
    return render(request, "admin_import.html", db, user, sources=sources, columns=IMPORT_COLUMNS, result=st, error=None)


@app.post("/admin/opportunities/{opp_id}/review", dependencies=[Depends(verify_csrf)])
async def admin_opp_review(opp_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(staff)):
    form = await request.form()
    o = db.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "غير موجود")
    decision = form.get("decision")
    typ = form.get("opportunity_type") or o.opportunity_type
    if typ not in OPPORTUNITY_TYPES:
        raise HTTPException(400, "نوع غير صالح")
    if decision == "approve":
        if typ == "SUBCONTRACT_CONFIRMED" and not o.evidence:
            return redirect("/admin/review", "لا يمكن اعتماد «باطن مؤكد» دون دليل مرفق")
        if not o.source_url.startswith("https://"):
            return redirect("/admin/review", "رابط المصدر غير صالح")
        rewritten = str(form.get("reviewer_summary") or "").strip()[:4000]
        if rewritten:  # reviewer's own wording replaces source text and becomes publishable
            o.summary, o.summary_origin = rewritten, "reviewer"
        o.opportunity_type, o.review_state, o.review_reasons = typ, "approved", []
        if o.summary and not radar.summary_publishable(o.summary, o.summary_origin, o.source.rights):
            o.review_reasons = ["اعتُمدت دون الوصف: نص المصدر غير مرخّص لإعادة النشر"]
        o.verified_at = utcnow()
        o.review_due_at = utcnow() + dt.timedelta(days=o.source.verify_ttl_days)
    elif decision == "reject":
        o.review_state = "rejected"
        o.review_reasons = [str(form.get("reason") or "رُفضت")[:300]]
    else:
        raise HTTPException(400, "قرار غير صالح")
    o.reviewer_id = user.id
    db.add(AuditLog(actor_id=user.id, action=f"opportunity.{decision}", target=f"opp:{o.id}", detail=typ))
    db.commit()
    return redirect("/admin/review", "حُفظ القرار")


@app.post("/admin/reports/{rid}/close", dependencies=[Depends(verify_csrf)])
def admin_report_close(rid: int, db: Session = Depends(get_db), user: User = Depends(staff)):
    r = db.get(ErrorReport, rid)
    if not r:
        raise HTTPException(404, "غير موجود")
    r.status = "closed"
    db.add(AuditLog(actor_id=user.id, action="report.closed", target=f"report:{rid}"))
    db.commit()
    return redirect("/admin/review", "أُغلق البلاغ")


@app.post("/admin/sources/{sid}/run", dependencies=[Depends(verify_csrf)])
def admin_source_run(sid: int, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404, "غير موجود")
    run = radar.run_source(db, s, force=True)
    db.add(AuditLog(actor_id=user.id, action="source.run", target=s.key, detail=run.outcome))
    db.commit()
    return redirect("/admin", f"{s.name}: {run.outcome} — {run.reason}")


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    rows = db.scalars(select(Order).order_by(Order.id.desc()).limit(200)).all()
    totals = dict(db.execute(select(Order.status, func.sum(Order.total)).group_by(Order.status)).all())
    subs = db.scalar(select(func.count(Subscription.id)).where(Subscription.revoked.is_(False),
                                                               Subscription.ends_at > utcnow()))
    ents = db.scalar(select(func.count(Entitlement.id)).where(Entitlement.revoked.is_(False)))
    return render(request, "admin_orders.html", db, user, rows=rows, totals=totals, subs=subs, ents=ents)


@app.post("/admin/orders/{oid}/refund", dependencies=[Depends(verify_csrf)])
def admin_refund(oid: int, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    o = db.get(Order, oid)
    if not o:
        raise HTTPException(404, "غير موجود")
    outcome = store.refund(db, o, user)
    db.commit()
    return redirect("/admin/orders", f"الطلب {oid}: {outcome}")


# ----------------------------------------------------------------- internal jobs & SEO
@app.post("/internal/jobs/refresh")
def internal_refresh(request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "jobs", 30, 3600)
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    trigger = None
    if settings.jobs_token and bearer and constant_eq(bearer, settings.jobs_token):
        trigger = "token"
    elif bearer.count(".") == 2:
        try:
            claims = oidc.verify_github_token(bearer, settings.jobs_oidc_repository, settings.jobs_oidc_audience,
                                              key_resolver=OIDC_KEY_RESOLVER)
            trigger = f"github_oidc:{claims['event_name']}"
        except oidc.OIDCRejected as e:
            db.add(AuditLog(action="jobs.oidc_rejected", target=client_ip(request), detail=str(e)[:200]))
            db.commit()
    if not trigger:
        raise HTTPException(401, "unauthorized")
    return JSONResponse(json.loads(json.dumps(jobs.recorded_refresh(db, trigger), default=str)))


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    last = db.scalar(select(func.max(JobRun.finished_at)).where(JobRun.job == "refresh", JobRun.ok.is_(True)))
    # non-secret facts that let an outside check prove which database and build are live, that the scheduler
    # ran, and (last_refresh_at < started_at) that stored data outlived a restart
    return {"ok": True, "db": db.get_bind().dialect.name, "env": settings.env,
            "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:12], "started_at": STARTED_AT.isoformat() + "Z",
            "base_url": settings.base_url,  # public address used in e-mailed links (must be the real https URL)
            "last_refresh_at": last.isoformat() + "Z" if last else None}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return ("User-agent: *\nDisallow: /admin\nDisallow: /account\nDisallow: /dashboard\nDisallow: /pipeline\n"
            "Disallow: /quotes\nDisallow: /checkout\nDisallow: /dl/\nDisallow: /orders\n"
            f"Sitemap: {settings.base_url}/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap(db: Session = Depends(get_db)):
    urls = ["/", "/radar", "/store", "/pricing", "/calculator", "/sources", "/about", "/privacy", "/terms", "/refunds"]
    urls += [f"/store/{s}" for s in db.scalars(select(Product.slug))]
    urls += [f"/radar/{i}" for i in db.scalars(public_opps_query().with_only_columns(Opportunity.id))]
    body = "".join(f"<url><loc>{settings.base_url}{u}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                    media_type="application/xml")


# content pages: registered LAST so the path parameter never shadows another route
@app.get("/{page}", response_class=HTMLResponse)
def static_page(page: str, request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    if page not in {"privacy", "terms", "refunds", "about"}:
        raise HTTPException(404, "الصفحة غير موجودة")
    return render(request, f"page_{page}.html", db, user)
