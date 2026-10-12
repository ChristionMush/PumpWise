"""
Django settings.

Two database roles are used against the SAME database (see docs/rls.md):

* ``default``  -> ``app_user``  : what the running app uses. Not a superuser, does
  not own the tables, no BYPASSRLS, so Row-Level Security always applies.
* ``owner``    -> ``app_owner`` : used ONLY for migrations
  (``manage.py migrate --database=owner``). Also subject to RLS (FORCE).

All secrets come from environment variables. Nothing secret is committed.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and value in (None, ""):
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def env_bool(name, default=False):
    return str(os.environ.get(name, str(default))).lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DJANGO_DEBUG", False)

# No insecure fallback: a missing key must fail loudly outside of DEBUG.
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key" if DEBUG else None, required=True)

ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "rest_framework",
    "apps.core",
    "apps.tenants",
    "apps.accounts",
    "apps.stations",
    "apps.operations",
    "apps.reports",
    "apps.insights",
    "apps.billing",
    "apps.exports",
    "apps.onboarding",
    "apps.audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DB_HOST = env("DB_HOST", "localhost")
DB_PORT = env("DB_PORT", "5432")
DB_NAME = env("DB_NAME", "fuelstation")
DB_APP_USER = env("DB_APP_USER", "app_user")
DB_OWNER_USER = env("DB_OWNER_USER", "app_owner")
# Superuser credentials: used ONLY by `manage.py bootstrap_db` (and tests). Never by the app.
DB_ADMIN_USER = env("DB_ADMIN_USER", "postgres")
DB_ADMIN_PASSWORD = env("DB_ADMIN_PASSWORD", "")


def _db(user, password_var, atomic):
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": DB_NAME,
        "USER": user,
        "PASSWORD": env(password_var, "", required=not DEBUG),
        "HOST": DB_HOST,
        "PORT": DB_PORT,
        "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", "60")),
        # Atomic requests are REQUIRED: the tenant context uses SET LOCAL, which only
        # lives inside a transaction.
        "ATOMIC_REQUESTS": atomic,
    }


DATABASES = {
    "default": _db(DB_APP_USER, "DB_APP_PASSWORD", atomic=True),
    "owner": _db(DB_OWNER_USER, "DB_OWNER_PASSWORD", atomic=False),
}
DATABASE_ROUTERS = ["apps.core.routers.OwnerMigrationRouter"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = "Africa/Harare"
USE_I18N = True
USE_TZ = True

# Default country calling code used to normalise local phone numbers (0771234567 -> +263771234567)
DEFAULT_PHONE_COUNTRY_CODE = env("DEFAULT_PHONE_COUNTRY_CODE", "263")

# Used to build absolute links (QR codes, export links)
# Where QR codes point (the PWA)
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", "http://localhost:5173")
USE_S3_MEDIA = bool(env("AWS_STORAGE_BUCKET_NAME"))
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "media"))
MEDIA_URL = "/media/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024

# Branding shown on login, header and report footers (tenants add their own logo/names later)
BUILT_BY = "Tendeka & Elichi"

# Behind the AWS load balancer set this so audit logs record the real client IP.
TRUST_X_FORWARDED_FOR = env_bool("TRUST_X_FORWARDED_FOR", False)

if env("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("REDIS_URL")}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.TenantJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_THROTTLE_RATES": {"login": env("THROTTLE_LOGIN", "10/min"), "refresh": env("THROTTLE_REFRESH", "30/min")},
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}

# Attendants work offline for long stretches, so refresh tokens are long-lived; revocation
# is instant via User.token_version instead of short expiry.
JWT_SIGNING_KEY = SECRET_KEY
JWT_ACCESS_LIFETIME_SECONDS = 30 * 60
JWT_REFRESH_LIFETIME_SECONDS = 30 * 24 * 3600

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]  # load-balancer health checks arrive over plain HTTP
    SECURE_HSTS_SECONDS = 31536000

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

if USE_S3_MEDIA:
    # Private bucket, short-lived signed URLs (photos belong to their tenant).
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", "af-south-1")
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = 300
    AWS_DEFAULT_ACL = None
    STORAGES = {"default": {"BACKEND": "storages.backends.s3.S3Storage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}

# ---- background jobs (Celery + Redis) ----
from celery.schedules import crontab  # noqa: E402

CELERY_BROKER_URL = env("CELERY_BROKER_URL", env("REDIS_URL", "redis://localhost:6379/0"))
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_BEAT_SCHEDULE = {
    "alerts-hourly": {"task": "apps.insights.tasks.run_alerts", "schedule": crontab(minute=5)},
    "daily-summary": {"task": "apps.insights.tasks.send_daily_summaries", "schedule": crontab(hour=19, minute=0)},
    "billing-daily": {"task": "apps.billing.tasks.run_billing_cycle", "schedule": crontab(hour=6, minute=0)},
    "monthly-exports": {"task": "apps.exports.tasks.run_monthly_exports", "schedule": crontab(day_of_month=1, hour=2, minute=0)},
    "purge-expired-exports": {"task": "apps.exports.tasks.purge_expired_exports", "schedule": crontab(hour=3, minute=30)},
}

# ---- outbound messages (SMS / WhatsApp) behind one interface ----
MESSAGE_PROVIDER = env("MESSAGE_PROVIDER", "apps.insights.messaging.ConsoleProvider")
MESSAGE_GATEWAY_URL = env("MESSAGE_GATEWAY_URL", "")
MESSAGE_GATEWAY_TOKEN = env("MESSAGE_GATEWAY_TOKEN", "")

# ---- billing ----
BILLING_PROVIDERS = {"hmac": "apps.billing.providers.HmacJsonProvider"}  # add the chosen payment company here
BILLING_DEFAULT_PROVIDER = env("BILLING_DEFAULT_PROVIDER", "hmac")
BILLING_WEBHOOK_SECRET = env("BILLING_WEBHOOK_SECRET", "")
BILLING_CHECKOUT_URL = env("BILLING_CHECKOUT_URL", "https://pay.example.invalid/checkout")
BILLING_REMINDER_DAYS = (1, 3, 7)  # days past due
BILLING_SUSPEND_AFTER_DAYS = 10
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "PumpWise <no-reply@localhost>")
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)

# ---- data export ----
EXPORT_S3_BUCKET = env("EXPORT_S3_BUCKET", "")  # versioned bucket in production; empty = local disk
EXPORT_LINK_TTL_SECONDS = int(env("EXPORT_LINK_TTL_SECONDS", str(24 * 3600)))
EXPORT_FILE_DAYS = int(env("EXPORT_FILE_DAYS", "30"))
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", "af-south-1")
