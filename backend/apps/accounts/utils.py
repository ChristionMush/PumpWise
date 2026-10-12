import re
import secrets

from django.conf import settings


def normalize_email(value):
    return (value or "").strip().lower() or None


def normalize_phone(value):
    """Return +E.164-style number or None.

    '0771234567' -> '+263771234567', '263 77 123 4567' -> '+263771234567'.
    """
    if not value:
        return None
    raw = re.sub(r"[\s\-().]", "", str(value))
    if not re.fullmatch(r"\+?\d{7,15}", raw):
        return None
    cc = settings.DEFAULT_PHONE_COUNTRY_CODE
    if raw.startswith("+"):
        return raw
    if raw.startswith("00"):
        return "+" + raw[2:]
    if raw.startswith("0"):
        return f"+{cc}{raw[1:]}"
    if raw.startswith(cc):
        return "+" + raw
    return f"+{cc}{raw}"


def normalize_identifier(value):
    """Login identifier: email if it contains '@', else phone. None if unusable."""
    value = (value or "").strip()
    if "@" in value:
        return normalize_email(value)
    return normalize_phone(value)


# No 0/O/1/l/I: temporary passwords get read aloud and typed on small phones.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRTUVWXYZ23456789"


def generate_temporary_password(length=10):
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))
