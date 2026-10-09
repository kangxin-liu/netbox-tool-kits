# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Django settings, read entirely from the environment.

There is no default SECRET_KEY, NETBOX_URL, or NETBOX_TOKEN in this file by design: a missing
one stops startup naming the variable (Requirements 2.1, 2.2, 14.1).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from config import env

BASE_DIR = Path(__file__).resolve().parent.parent

# Local development convenience only. Real deployments set the environment directly; values
# already present in the environment win.
load_dotenv(BASE_DIR / ".env", override=False)

ENVIRON = os.environ

# --- Django core ---------------------------------------------------------------------------
SECRET_KEY = env.require(ENVIRON, "DJANGO_SECRET_KEY")
DEBUG = env.flag(ENVIRON, "DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.csv_list(ENVIRON, "DJANGO_ALLOWED_HOSTS", default=("127.0.0.1", "localhost"))
CSRF_TRUSTED_ORIGINS = env.csv_list(ENVIRON, "DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "importer",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "importer.context_processors.netbox_target",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env.optional(ENVIRON, "DJANGO_DB_PATH") or BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-au"
TIME_ZONE = env.optional(ENVIRON, "DJANGO_TIME_ZONE") or "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Session and transport hardening -------------------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "importer:home"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_HTTPONLY = False  # the template needs to read it for the POST form token
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# Cookies go secure-only unless we are running the local plaintext development case.
_SERVED_OVER_TLS = env.flag(ENVIRON, "DJANGO_SERVED_OVER_TLS", default=not DEBUG)
SESSION_COOKIE_SECURE = _SERVED_OVER_TLS
CSRF_COOKIE_SECURE = _SERVED_OVER_TLS
SECURE_SSL_REDIRECT = env.flag(ENVIRON, "DJANGO_SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = env.integer(ENVIRON, "DJANGO_HSTS_SECONDS", default=0, minimum=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

# --- NetBox target ------------------------------------------------------------------------
NETBOX_TARGET = env.netbox_target(env.require(ENVIRON, "NETBOX_URL"))
NETBOX_URL = NETBOX_TARGET.url
NETBOX_HOST = NETBOX_TARGET.host
NETBOX_LOOPBACK_DEV = NETBOX_TARGET.loopback_dev
# CONFIDENTIAL. Held here only so the client factory can read it; never rendered, logged,
# persisted, or included in an error message.
NETBOX_TOKEN = env.require(ENVIRON, "NETBOX_TOKEN")
NETBOX_VERIFY_SSL = env.flag(ENVIRON, "NETBOX_VERIFY_SSL", default=True)
NETBOX_TIMEOUT = env.integer(ENVIRON, "NETBOX_TIMEOUT", default=30)
# Refuse to import outside this range rather than failing mid-run on a renamed field (Req 2.7).
# Defaults to >=4.6,<4.7, the range the importer is verified against. Both bounds are overridable
# per environment (e.g. a staging instance pinned to an older release) so the floor can be lowered
# without editing code; doing so is at the operator's risk and only the verified range is tested.
NETBOX_MIN_VERSION = env.version_tuple(ENVIRON, "NETBOX_MIN_VERSION", default=(4, 6))
NETBOX_MAX_VERSION_EXCLUSIVE = env.version_tuple(
    ENVIRON, "NETBOX_MAX_VERSION_EXCLUSIVE", default=(4, 7)
)

# --- Import behaviour ---------------------------------------------------------------------
RACK_NAME_DELIMITER = env.optional(ENVIRON, "RACK_NAME_DELIMITER") or "-"
NETBOX_DEFAULT_DEVICE_ROLE = env.optional(ENVIRON, "NETBOX_DEFAULT_DEVICE_ROLE")
NETBOX_AUTOCREATE_DEVICE_TYPES = env.flag(ENVIRON, "NETBOX_AUTOCREATE_DEVICE_TYPES", default=False)
# Off by default for D4's reason applied to a smaller object: the cabling sheet names two ports per
# row and the tool cannot tell a new port from a mistyped one (Requirement 9.24).
NETBOX_AUTOCREATE_INTERFACES = env.flag(ENVIRON, "NETBOX_AUTOCREATE_INTERFACES", default=False)
MAX_UPLOAD_MB = env.integer(ENVIRON, "MAX_UPLOAD_MB", default=5)
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE
# Uploads are parsed from a temp file that is deleted in a finally block, never from a served path.
FILE_UPLOAD_TEMP_DIR = env.optional(ENVIRON, "DJANGO_UPLOAD_TEMP_DIR") or None
PLAN_EXPIRY_MINUTES = env.integer(ENVIRON, "PLAN_EXPIRY_MINUTES", default=30)
MAX_APPLY_ROWS = env.integer(ENVIRON, "MAX_APPLY_ROWS", default=2000)
MAX_ISSUES_DISPLAYED = env.integer(ENVIRON, "MAX_ISSUES_DISPLAYED", default=500)

# --- Logging ------------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Security-relevant events: login success/failure, logout, and every apply (Req 14.7).
        # Deliberately handler-free so records propagate to root: one console line, and captured
        # by anything watching the root logger.
        "importer.audit": {"level": "INFO"},
    },
}

env.warn_about_transport(NETBOX_TARGET, NETBOX_VERIFY_SSL)
