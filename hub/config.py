"""Runtime settings, read from environment variables only (never from code or the repo)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("HUB_ENV", "development"))
    database_url: str = field(default_factory=lambda: os.environ.get("HUB_DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'hub.db'}"))
    secret_key: str = field(default_factory=lambda: os.environ.get("HUB_SECRET_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("HUB_BASE_URL", "http://127.0.0.1:8000"))
    # private product files: NEVER under a static/public directory
    product_dir: Path = field(default_factory=lambda: Path(os.environ.get("HUB_PRODUCT_DIR", ROOT / "products" / "dist" / "full")))
    sample_dir: Path = field(default_factory=lambda: Path(os.environ.get("HUB_SAMPLE_DIR", ROOT / "products" / "dist" / "sample")))
    payment_provider: str = field(default_factory=lambda: os.environ.get("HUB_PAYMENT_PROVIDER", "sandbox"))
    payments_live_enabled: bool = field(default_factory=lambda: _bool("HUB_PAYMENTS_LIVE_ENABLED", False))
    sandbox_webhook_secret: str = field(default_factory=lambda: os.environ.get("HUB_SANDBOX_WEBHOOK_SECRET", ""))
    moyasar_secret_key: str = field(default_factory=lambda: os.environ.get("MOYASAR_SECRET_KEY", ""))
    moyasar_publishable_key: str = field(default_factory=lambda: os.environ.get("MOYASAR_PUBLISHABLE_KEY", ""))
    moyasar_webhook_token: str = field(default_factory=lambda: os.environ.get("MOYASAR_WEBHOOK_SECRET_TOKEN", ""))
    jobs_token: str = field(default_factory=lambda: os.environ.get("HUB_JOBS_TOKEN", ""))
    admin_alert_email: str = field(default_factory=lambda: os.environ.get("HUB_ADMIN_ALERT_EMAIL", ""))
    seller_name: str = field(default_factory=lambda: os.environ.get("HUB_SELLER_NAME", "غازي للأعمال (بيانات البائع غير مكتملة)"))
    seller_vat_number: str = field(default_factory=lambda: os.environ.get("HUB_SELLER_VAT_NUMBER", ""))
    vat_rate: float = field(default_factory=lambda: float(os.environ.get("HUB_VAT_RATE", "0.15")))
    vat_registered: bool = field(default_factory=lambda: _bool("HUB_VAT_REGISTERED", False))
    download_token_ttl_seconds: int = 300
    download_limit_per_product: int = 10
    session_days: int = 14

    def __post_init__(self):
        if not self.secret_key:
            if self.env == "production":
                raise RuntimeError("HUB_SECRET_KEY is required in production")
            self.secret_key = secrets.token_urlsafe(32)
        if not self.sandbox_webhook_secret:
            self.sandbox_webhook_secret = self.secret_key + ":sandbox"
        if self.env == "production" and self.payment_provider == "sandbox" and self.payments_live_enabled:
            raise RuntimeError("live payments cannot use the sandbox provider")

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


settings = Settings()
