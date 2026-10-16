"""Inbound webhook: HMAC verification and payload parsing.

Per eFax's spec: each POST carries `X-HMAC-Signature`, a hex SHA256 HMAC of the raw JSON body using
the secret we gave eFax when registering the URL (HMAC enrolment goes through the account team).
Reply 200 with a JSON body within 10 s. eFax retries 5xx only, 5 attempts over ~20 minutes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .errors import WebhookSignatureError
from .models import _pick


def verify_signature(
    body: bytes,
    signature: str | None,
    secret: str,
    *,
    algorithm: str = "sha256",
    encoding: str = "hex",
) -> None:
    """Raise WebhookSignatureError unless `signature` is the HMAC of the raw request body."""
    if not secret:
        raise WebhookSignatureError("no webhook secret configured; refusing unsigned inbound fax")
    if not signature:
        raise WebhookSignatureError("missing signature")
    digest = hmac.new(secret.encode(), body, getattr(hashlib, algorithm)).digest()
    expected = base64.b64encode(digest).decode() if encoding == "base64" else digest.hex()
    candidate = signature.strip()
    if "=" in candidate and candidate.split("=", 1)[0].lower() == algorithm:
        candidate = candidate.split("=", 1)[1]  # tolerate 'sha256=<digest>'
    if not hmac.compare_digest(expected, candidate):
        raise WebhookSignatureError("signature mismatch")


@dataclass(frozen=True)
class InboundNotification:
    """An inbound-fax or outbound-status notification.

    The image is never in the payload. Fetch it with EFaxClient.download_image(fax_id); the
    payload's resource_url is deliberately ignored so a forged URL can't redirect our token.
    """

    fax_id: str
    notification_id: str | None
    direction: str | None
    from_number: str | None
    to_number: str | None
    pages: int | None
    # Outbound notifications only: COMPLETE or ERROR, plus error / error_reason on failure.
    status: str | None
    error: str | None
    raw: dict[str, Any]

    @property
    def is_inbound(self) -> bool:
        return (self.direction or "INBOUND").upper() == "INBOUND"

    @property
    def client_reference_id(self) -> str | None:
        data = self.raw.get("client_data")
        return data.get("client_reference_id") if isinstance(data, dict) else None


def parse_notification(body: bytes) -> InboundNotification:
    try:
        raw = json.loads(body)
    except ValueError:
        raise ValueError("notification is not JSON") from None
    if not isinstance(raw, dict) or not raw.get("fax_id"):
        raise ValueError("notification has no fax_id")
    pages = raw.get("pages")
    error = _pick(raw, "error_reason", "error")
    return InboundNotification(
        fax_id=str(raw["fax_id"]),
        notification_id=_pick(raw, "notification_id"),
        direction=_pick(raw, "direction"),
        from_number=_pick(raw, "sending_fax_number", "originating_fax_number"),
        to_number=_pick(raw, "receiving_fax_number", "destination_fax_number"),
        pages=int(pages) if pages not in (None, "") else None,
        status=_pick(raw, "status"),
        error=str(error) if error is not None else None,
        raw=raw,
    )
