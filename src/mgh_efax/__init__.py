"""eFax Enterprise API client (Milligram Health)."""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_REGION,
    EFaxClient,
    EFaxConfig,
    base_url_for_region,
)
from .errors import (
    AuthError,
    EFaxError,
    NotFound,
    RateLimited,
    RequestRejected,
    SendUncertain,
    ServerError,
    WebhookSignatureError,
)
from .models import CoverPage, Destination, Document, FaxMetadata, FaxRef, SendRequest
from .numbers import mask_number, normalize_fax_number
from .webhook import InboundNotification, parse_notification, verify_signature

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_REGION",
    "base_url_for_region",
    "AuthError",
    "CoverPage",
    "Destination",
    "Document",
    "EFaxClient",
    "EFaxConfig",
    "EFaxError",
    "FaxMetadata",
    "FaxRef",
    "InboundNotification",
    "NotFound",
    "RateLimited",
    "RequestRejected",
    "SendRequest",
    "SendUncertain",
    "ServerError",
    "WebhookSignatureError",
    "mask_number",
    "normalize_fax_number",
    "parse_notification",
    "verify_signature",
]
