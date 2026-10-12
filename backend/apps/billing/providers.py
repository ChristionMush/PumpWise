"""
BillingProvider: the seam between a payment company and our subscription logic.

A provider does two things: (1) verify and translate its webhook into a BillingEvent, (2) create a pay link.
Everything after that (extending paid_through, reactivating, creating a tenant for a new subscription, reseller
share) is provider-independent. The payment company has NOT been chosen yet. To add Stripe, Paystack,
Flutterwave or a local Zimbabwe provider: subclass BillingProvider, verify that vendor's signature scheme exactly
as its documentation says, map its payload to BillingEvent, and register it in settings.BILLING_PROVIDERS.
`HmacJsonProvider` is a complete, tested reference implementation (and a simple option for a bank/aggregator bridge).
"""
import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils.module_loading import import_string


class InvalidWebhook(Exception):
    """Bad signature, stale timestamp or unreadable body. Never reveal which."""


@dataclass
class BillingEvent:
    id: str
    type: str  # "payment.succeeded" | "payment.failed"
    amount: Decimal
    currency: str
    months: int = 1
    tenant_id: str | None = None  # an existing customer paying
    customer: dict | None = None  # a NEW customer: business_name, owner_name, owner_email/phone, stations, plan, addons, reseller
    raw: dict = field(default_factory=dict)


class BillingProvider(ABC):
    name = ""

    @abstractmethod
    def parse_webhook(self, headers, body: bytes) -> BillingEvent: ...

    @abstractmethod
    def create_checkout(self, *, tenant, amount, currency, return_url) -> str:
        """URL where the owner pays (used by the 'pay to reactivate' screen)."""


class HmacJsonProvider(BillingProvider):
    """Body is JSON; header X-Signature = hex HMAC-SHA256 of f"{X-Timestamp}.{body}"; timestamp must be recent (replay guard)."""

    name = "hmac"
    TOLERANCE_SECONDS = 300

    def parse_webhook(self, headers, body):
        secret = settings.BILLING_WEBHOOK_SECRET
        if not secret:
            raise InvalidWebhook()
        ts, sig = headers.get("X-Timestamp", ""), headers.get("X-Signature", "")
        try:
            if abs(time.time() - int(ts)) > self.TOLERANCE_SECONDS:
                raise InvalidWebhook()
        except ValueError:
            raise InvalidWebhook()
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise InvalidWebhook()
        try:
            data = json.loads(body)
            return BillingEvent(
                id=str(data["id"])[:120], type=str(data["type"]), amount=Decimal(str(data["amount"])), currency=str(data.get("currency", "USD")).upper(),
                months=int(data.get("months", 1)), tenant_id=data.get("tenant_id"), customer=data.get("customer"), raw={"type": data["type"]},
            )
        except (KeyError, ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
            raise InvalidWebhook()

    def create_checkout(self, *, tenant, amount, currency, return_url):
        return f"{settings.BILLING_CHECKOUT_URL}?tenant={tenant.pk}&amount={amount}&currency={currency}&return={return_url}"


def sign(secret: str, body: bytes, ts: int | None = None):
    """Helper for tests and for whoever builds the bridge that calls our webhook."""
    ts = str(ts if ts is not None else int(time.time()))
    return {"X-Timestamp": ts, "X-Signature": hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()}


def get_provider(name):
    path = settings.BILLING_PROVIDERS.get(name)
    return import_string(path)() if path else None
