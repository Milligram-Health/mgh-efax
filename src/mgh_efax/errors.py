"""Exceptions. Messages never carry fax content or full fax numbers."""

from __future__ import annotations


class EFaxError(Exception):
    """Any failure talking to eFax. `status` is the HTTP status, or None for network failures."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthError(EFaxError):
    """Token generation failed or the token was rejected twice in a row."""


class NotFound(EFaxError):
    pass


class RateLimited(EFaxError):
    pass


class ServerError(EFaxError):
    pass


class RequestRejected(EFaxError):
    """4xx other than 401/404/429: the request itself is wrong (bad number, bad document...)."""


class SendUncertain(EFaxError):
    """POST /faxes may or may not have been accepted (timeout, dropped connection, 5xx).

    Never blindly resend: a clinical fax sent twice is its own incident. Reconcile with
    `EFaxClient.find_sent(client_reference_id=...)` first.
    """


class WebhookSignatureError(EFaxError):
    pass
