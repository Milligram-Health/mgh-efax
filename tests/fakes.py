from __future__ import annotations

import json
from collections.abc import Callable

from mgh_efax import EFaxClient, EFaxConfig
from mgh_efax.transport import Response, TransportError

Handler = Callable[[str, str, dict, bytes | None], "Response | Exception"]


def resp(status: int = 200, body=None, headers: dict | None = None) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    h = {"content-type": "application/json"} if not isinstance(body, bytes) else {}
    return Response(status, {**h, **(headers or {})}, raw)


class FakeTransport:
    """Routes (method, path) to queued responses; records every call."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def on(self, method: str, path: str, *responses) -> FakeTransport:
        self.routes.setdefault((method, path), []).extend(responses)
        return self

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("securedocex.test", 1)[1].split("?", 1)[0]
        self.calls.append((method, url, headers, body))
        if (method, path) == ("POST", "/tokens") and not self.routes.get(("POST", "/tokens")):
            return resp(200, {"access_token": "tok", "expires_in": 3600})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected {method} {path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def paths(self) -> list[str]:
        return [f"{m} {u.split('securedocex.test', 1)[1]}" for m, u, _, _ in self.calls]


def make_client(transport: FakeTransport, clock=None) -> EFaxClient:
    return EFaxClient(
        EFaxConfig(
            app_id="app",
            api_key="secret-key",
            user_id="u1",
            base_url="https://api.securedocex.test",
        ),
        transport=transport,
        clock=clock or (lambda: 0.0),
        sleep=lambda s: None,
    )


__all__ = ["FakeTransport", "TransportError", "make_client", "resp"]
