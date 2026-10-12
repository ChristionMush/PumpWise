"""
Outbound messages (daily summary, billing reminders, welcome invites) behind ONE interface.

The product needs SMS and/or WhatsApp, but the vendor is not chosen yet. Two providers ship:
 * ConsoleProvider    - logs the message (development, tests, demo).
 * HttpGatewayProvider - POSTs JSON {"to","channel","message"} to a gateway you configure
                          (MESSAGE_GATEWAY_URL / MESSAGE_GATEWAY_TOKEN). Any SMS/WhatsApp vendor, or a small
                          bridge service, can sit behind that URL.
A vendor-specific provider later is one more subclass and a settings change.
"""
import json
import logging
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string

from .models import OutboundMessage

log = logging.getLogger(__name__)


class MessageError(Exception):
    pass


class MessageProvider(ABC):
    @abstractmethod
    def send(self, *, to: str, channel: str, body: str) -> str:
        """Send one message; return a provider reference. Raise MessageError on failure."""


class ConsoleProvider(MessageProvider):
    sent: list = []  # inspected by tests

    def send(self, *, to, channel, body):
        log.info("[%s -> %s] %s", channel, to, body)
        ConsoleProvider.sent.append({"to": to, "channel": channel, "body": body})
        return "console"


class HttpGatewayProvider(MessageProvider):
    def send(self, *, to, channel, body):
        if not settings.MESSAGE_GATEWAY_URL:
            raise MessageError("MESSAGE_GATEWAY_URL is not set")
        req = urllib.request.Request(
            settings.MESSAGE_GATEWAY_URL,
            data=json.dumps({"to": to, "channel": channel, "message": body}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.MESSAGE_GATEWAY_TOKEN}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - URL comes from our own settings
                return f"http-{resp.status}"
        except Exception as exc:  # noqa: BLE001
            raise MessageError(str(exc)[:200])


def get_provider() -> MessageProvider:
    return import_string(settings.MESSAGE_PROVIDER)()


def send_message(*, tenant_id, channel, to, kind, body):
    """Send and record. Never raises: a failed SMS must not break the job that wanted to send it."""
    if channel == "none" or not to:
        return OutboundMessage.objects.create(tenant_id=tenant_id, channel=channel or "none", to=to or "", kind=kind, body=body, status="skipped", detail="no channel or phone")
    try:
        ref = get_provider().send(to=to, channel=channel, body=body)
        return OutboundMessage.objects.create(tenant_id=tenant_id, channel=channel, to=to, kind=kind, body=body, status="sent", detail=ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("message to %s failed: %s", to, exc)
        return OutboundMessage.objects.create(tenant_id=tenant_id, channel=channel, to=to, kind=kind, body=body, status="failed", detail=str(exc)[:300])
