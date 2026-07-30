"""
config.py
---------
Central configuration for Smart DIT Online Archive.

All paths are computed relative to this file so the application can be
started from any working directory with `python app.py`.

Nothing sensitive is hardcoded here. Secrets (SECRET_KEY, GEMINI_API_KEY)
are read from environment variables or an optional local .env file.
"""

import os
import secrets
from datetime import timedelta
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Load variables from a .env file if present. This never overwrites
# variables that are already set in the real environment.
load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESOURCE_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "resources")
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

DB_FILENAME = "smart_dit_archive.db"
DB_PATH = os.path.join(DATA_DIR, DB_FILENAME)


def _database_uri():
    """Use a managed Postgres database when DATABASE_URL is supplied.

    Local development remains zero-configuration with SQLite. The normalised
    psycopg URL works with Supabase and other hosted Postgres providers.
    """
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        return f"sqlite:///{DB_PATH}"
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


DATABASE_URI = _database_uri()
DATABASE_ENGINE_OPTIONS = (
    {"connect_args": {"timeout": 15}}
    if DATABASE_URI.startswith("sqlite")
    else {"pool_pre_ping": True, "pool_recycle": 280}
)

def _get_or_create_secret_key():
    """Resolve the Flask secret key, preferring (in order):

    1. A real SECRET_KEY set in the environment / .env file (recommended for
       any real deployment).
    2. A key persisted on disk from a previous run, so logged-in sessions
       survive an app restart even if the user never configured .env.
    3. A freshly generated key, saved to disk for next time.
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    os.makedirs(DATA_DIR, exist_ok=True)
    key_path = os.path.join(DATA_DIR, ".flask_secret_key")
    try:
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        new_key = secrets.token_hex(32)
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(new_key)
        return new_key
    except OSError:
        # Filesystem not writable for some reason - fall back to an
        # in-memory-only key rather than crashing the app.
        return secrets.token_hex(32)


class Config:
    SECRET_KEY = _get_or_create_secret_key()

    SQLALCHEMY_DATABASE_URI = DATABASE_URI
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = DATABASE_ENGINE_OPTIONS

    MAX_CONTENT_LENGTH = 30 * 1024 * 1024  # 30 MB upload ceiling

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Sessions are intentionally long-lived only when a learner chooses
    # “remember me”; ordinary sessions remain browser-session scoped.
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    REMEMBER_COOKIE_DURATION = timedelta(days=21)
    SESSION_REFRESH_EACH_REQUEST = True
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    ALLOWED_RESOURCE_EXTENSIONS = {"pdf", "docx", "pptx", "txt", "md", "mp4", "webm"}

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
    # Gemini 3.5 Flash is the stable, current default for high-quality
    # teaching explanations, multi-step reasoning, and Google Search grounding.
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
    GEMINI_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "4096"))
    # Google Search is used only for supplementary, module-scoped guidance
    # when lecturer material does not provide a confident answer.
    GEMINI_ENABLE_WEB_GROUNDING = os.environ.get("GEMINI_ENABLE_WEB_GROUNDING", "1") == "1"

    # Transactional email. Any standards-compliant SMTP provider can be used.
    # Question/claim notifications remain available in-app when email has not
    # yet been configured.
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1") == "1"
    SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "0") == "1"
    SMTP_TIMEOUT_SECONDS = int(os.environ.get("SMTP_TIMEOUT_SECONDS", "10"))
    MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

    # When these values are configured, uploaded resources are kept in a
    # private Supabase Storage bucket instead of the web server's temporary
    # filesystem. Leave them unset for simple local development.
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "resources").strip() or "resources"

    INITIAL_STUDENT_PASSWORD = os.environ.get("INITIAL_STUDENT_PASSWORD", "Student@123")
    INITIAL_LECTURER_PASSWORD = os.environ.get("INITIAL_LECTURER_PASSWORD", "Lecturer@123")

    # A one-time protected activation code is required before the first
    # department-head account can be created. Keep it only in Render's
    # environment variables, never in GitHub or a public form.
    HOD_ACTIVATION_CODE = os.environ.get("HOD_ACTIVATION_CODE", "").strip()

    APP_NAME = "Smart DIT Learning Hub"
    APP_TAGLINE = "Learning support, curriculum resources and actionable academic insights."
    HOST = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    PORT = int(os.environ.get("PORT", "5080"))
    APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
    IS_PRODUCTION = APP_ENV == "production"
    TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "0") == "1"
    PREFERRED_URL_SCHEME = "https"

    # Flask-WTF CSRF protection is on globally; AJAX calls send the token via
    # the X-CSRFToken header (see static/js/main.js).
    WTF_CSRF_ENABLED = True



def ensure_directories():
    """Create every folder the app needs. Safe to call on every startup."""
    for path in (DATA_DIR, UPLOAD_DIR, RESOURCE_UPLOAD_DIR, INSTANCE_DIR):
        os.makedirs(path, exist_ok=True)
