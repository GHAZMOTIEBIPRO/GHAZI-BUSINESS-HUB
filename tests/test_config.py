"""Neon / host-provided database URLs and the local .env loader."""
import os

from hub import config
from hub.db import normalise_url


def test_dotenv_loader_does_not_override_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# written by neon init\nDATABASE_URL="postgresql://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require"\n'
                   "export HUB_X_TEST=from-file\nHUB_Y_TEST=from-file\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HUB_X_TEST", raising=False)
    monkeypatch.setenv("HUB_Y_TEST", "from-env")
    config.load_dotenv(env)
    assert os.environ["DATABASE_URL"].startswith("postgresql://u:p@ep-x-pooler")  # quotes stripped
    assert os.environ["HUB_X_TEST"] == "from-file"
    assert os.environ["HUB_Y_TEST"] == "from-env"


def test_database_url_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon/db")
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    assert config._default_db_url() == "postgresql://neon/db"
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://explicit/db")
    assert config._default_db_url() == "postgresql://explicit/db"


def test_neon_url_gets_psycopg_driver_and_keeps_ssl():
    url = "postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    assert normalise_url(url) == url.replace("postgresql://", "postgresql+psycopg://", 1)
