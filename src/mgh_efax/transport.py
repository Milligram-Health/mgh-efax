"""The one place that does HTTP. Swappable so tests (and any app that wants httpx) can inject."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class TransportError(Exception):
    """Network-level failure. `sent` says whether the request may have reached the server."""

    def __init__(self, message: str, *, sent: bool):
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> Response: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> Response:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
    except urllib.error.URLError as exc:
        # A refused or unresolvable connection never reached eFax; anything else (timeouts, resets)
        # might have.
        reason = exc.reason
        sent = not isinstance(reason, (ConnectionRefusedError, socket.gaierror))
        raise TransportError(f"eFax unreachable: {type(reason).__name__}", sent=sent) from None
    except TimeoutError:
        raise TransportError("eFax timed out", sent=True) from None
