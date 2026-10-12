"""Test settings. Test-only throwaway values; real environments never use these."""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production-use-0123456789")
os.environ.setdefault("DB_APP_PASSWORD", "test-app-password")
os.environ.setdefault("DB_OWNER_PASSWORD", "test-owner-password")
os.environ.setdefault("DB_ADMIN_PASSWORD", "postgres")

from .settings import *

DB_NAME = "fuelstation_test"
DATABASES["default"]["NAME"] = DB_NAME
DATABASES["owner"]["NAME"] = DB_NAME
DATABASES["default"]["CONN_MAX_AGE"] = 0
DATABASES["owner"]["CONN_MAX_AGE"] = 0

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {"login": "10000/min", "refresh": "10000/min"},
}

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
MEDIA_ROOT = "/tmp/fuelstation-test-media"
CELERY_TASK_ALWAYS_EAGER = True
BILLING_WEBHOOK_SECRET = "test-webhook-secret"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
