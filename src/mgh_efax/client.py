"""eFax Enterprise API client.

One client per AppID + fax number (eFax's `user-id`). Credentials come from the host app; this
package holds none and phones nowhere but eFax.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from . import errors
from .models import FaxMetadata, FaxRef, SendRequest, iso8601
from .numbers import mask_number
from .transport import Response, Transport, TransportError, urllib_transport

log = logging.getLogger("mgh_efax")

# eFax hosts each data-sovereignty region on its own top-level domain: api.securedocex.us for US
# accounts, .ca for Canada, .com for the global (non-sovereign) platform. An account can only be
# reached on its own region's host. eFax doesn't publish the full list, so any region code is
# accepted and mapped to api.securedocex.<region>; `base_url` overrides the whole thing.
DEFAULT_REGION = "us"
_REGION_ALIASES = {"global": "com"}
_REGION_RE = re.compile(r"^[a-z]{2,10}$")


def base_url_for_region(region: str) -> str:
    code = _REGION_ALIASES.get(region.strip().lower(), region.strip().lower())
    if not _REGION_RE.match(code):
        raise ValueError(f"not an eFax region code: {region!r} (expected e.g. 'us', 'ca')")
    return f"https://api.securedocex.{code}"


DEFAULT_BASE_URL = base_url_for_region(DEFAULT_REGION)
_TOKEN_SKEW_SECONDS = 60  # refresh this long before eFax says the token expires
_DEFAULT_TOKEN_TTL = 3600  # used only if the token response carries no expires_in (eFax: 86399)
MAX_DESTINATIONS = 50
MAX_DOCUMENTS = 25
MAX_PAGE_SIZE = 100  # pagination-limit default and maximum


@dataclass(frozen=True)
class EFaxConfig:
    app_id: str
    api_key: str
    user_id: str
    # Data-sovereignty region of the eFax account ('us', 'ca', 'global'...). Ignored when base_url
    # is given; base_url is the escape hatch for a host that doesn't follow the pattern.
    region: str = DEFAULT_REGION
    base_url: str | None = None
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.0

    def __post_init__(self) -> None:
        url = (self.base_url or base_url_for_region(self.region)).rstrip("/")
        if not url.startswith("https://"):
            raise ValueError("eFax base_url must be https")
        object.__setattr__(self, "base_url", url)

    def __repr__(self) -> str:  # never print the key
        return (
            f"EFaxConfig(app_id={self.app_id!r}, user_id={self.user_id!r}, "
            f"base_url={self.base_url!r})"
        )


class EFaxClient:
    def __init__(
        self,
        config: EFaxConfig,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._transport = transport or urllib_transport
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    # ---- auth --------------------------------------------------------------------------------

    def _fetch_token(self) -> None:
        basic = base64.b64encode(f"{self.config.app_id}:{self.config.api_key}".encode()).decode()
        try:
            resp = self._transport(
                "POST",
                f"{self.config.base_url}/tokens",
                {
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                b"grant_type=client_credentials",
                self.config.timeout,
            )
        except TransportError as exc:
            raise errors.AuthError(f"token request failed: {exc}") from None
        if resp.status != 200:
            raise errors.AuthError("eFax refused the AppID/API key", status=resp.status)
        data = _json(resp)
        token = data.get("access_token")
        if not token:
            raise errors.AuthError("token response had no access_token", status=resp.status)
        ttl = float(data.get("expires_in") or _DEFAULT_TOKEN_TTL)
        self._token = token
        self._token_expires_at = self._clock() + max(ttl - _TOKEN_SKEW_SECONDS, 0)

    def _bearer(self, *, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or self._clock() >= self._token_expires_at:
                self._fetch_token()
            assert self._token
            return self._token

    # ---- request loop ------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> Response:
        """Auth, retries and error mapping.

        Idempotent calls retry on network errors, 429 and 5xx. POST /faxes (idempotent=False)
        retries only when eFax provably did not take the request (429, or a connection that never
        opened), and raises SendUncertain otherwise.
        """
        url = self.config.base_url + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        raw_body = json.dumps(body).encode() if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            req_headers = {
                "Authorization": f"Bearer {self._bearer()}",
                "user-id": self.config.user_id,
                "Accept": "application/json",
                **(headers or {}),
            }
            if raw_body is not None:
                req_headers["Content-Type"] = "application/json"
            try:
                resp = self._transport(method, url, req_headers, raw_body, self.config.timeout)
            except TransportError as exc:
                if not idempotent and exc.sent:
                    raise errors.SendUncertain(f"{method} {path}: {exc}") from None
                if attempt >= self.config.max_retries:
                    raise errors.EFaxError(f"{method} {path}: {exc}") from None
                self._backoff(attempt, None)
                attempt += 1
                continue

            if resp.status == 401 and not refreshed:
                refreshed = True
                self._bearer(force=True)
                continue
            if resp.status == 401:
                raise errors.AuthError(f"{method} {path}: token rejected", status=401)
            if resp.status == 429 or (resp.status >= 500 and idempotent):
                if attempt >= self.config.max_retries:
                    cls = errors.RateLimited if resp.status == 429 else errors.ServerError
                    raise cls(f"{method} {path}: HTTP {resp.status}", status=resp.status)
                self._backoff(attempt, resp.headers.get("retry-after"))
                attempt += 1
                continue
            if resp.status >= 500:
                raise errors.SendUncertain(
                    f"{method} {path}: HTTP {resp.status}", status=resp.status
                )
            if resp.status == 404:
                raise errors.NotFound(f"{method} {path}: not found", status=404)
            if resp.status >= 400:
                raise errors.RequestRejected(
                    f"{method} {path}: HTTP {resp.status} {_error_summary(resp)}",
                    status=resp.status,
                    code=_error_code(resp),
                )
            return resp

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.config.backoff_base * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        self._sleep(min(delay, 60.0))

    # ---- API ---------------------------------------------------------------------------------

    def health(self) -> bool:
        try:
            resp = self._transport(
                "GET", f"{self.config.base_url}/health", {}, None, self.config.timeout
            )
        except TransportError:
            return False
        return resp.status == 200

    def send_fax(self, request: SendRequest, *, transaction_id: str | None = None) -> FaxRef:
        """Send to a single destination. For several, use send_fax_multi."""
        if len(request.destinations) != 1:
            raise ValueError("send_fax takes exactly one destination; use send_fax_multi")
        return self.send_fax_multi(request, transaction_id=transaction_id)[0]

    def send_fax_multi(
        self, request: SendRequest, *, transaction_id: str | None = None
    ) -> list[FaxRef]:
        payload = request.to_payload()
        if len(payload["destinations"]) > MAX_DESTINATIONS:
            # eFax silently ignores recipients past 50.
            raise ValueError(f"eFax accepts at most {MAX_DESTINATIONS} destinations per call")
        if len(payload["documents"]) > MAX_DOCUMENTS:
            raise ValueError(f"eFax accepts at most {MAX_DOCUMENTS} documents per call")
        # transaction-id is for tracing only; eFax does not treat it as an idempotency key.
        transaction_id = transaction_id or str(uuid.uuid4())
        resp = self._request(
            "POST",
            "/faxes",
            body=payload,
            headers={"transaction-id": transaction_id},
            idempotent=False,
        )
        refs = FaxRef.list_from_response(_json(resp), transaction_id)
        log.info(
            "efax sent fax_ids=%s to=%s docs=%d",
            ",".join(r.fax_id for r in refs),
            ",".join(mask_number(d["fax_number"]) for d in payload["destinations"]),
            len(payload["documents"]),
        )
        return refs

    def get_metadata(self, fax_id: str) -> FaxMetadata:
        resp = self._request("GET", f"/faxes/{quote(fax_id, safe='')}/metadata")
        return FaxMetadata.from_response(_json(resp))

    def download_image(self, fax_id: str, *, desired_format: str | None = "PDF") -> bytes:
        """The fax image (PDF by default, or TIFF). Raw bytes, never logged.

        eFax answers JSON {"fax_id", "file_name", "image": <base64>} and marks the fax as
        downloaded, which is how image_downloaded=false polling avoids repeats.
        """
        query = {"desired_format": desired_format} if desired_format else None
        resp = self._request("GET", f"/faxes/{quote(fax_id, safe='')}/image", query=query)
        if "json" not in resp.headers.get("content-type", "") and not resp.body.startswith(b"{"):
            return resp.body
        content = _json(resp).get("image")
        if not content:
            raise errors.EFaxError("image response had no content", status=resp.status)
        return base64.b64decode(content)

    def delete(self, fax_id: str) -> None:
        self._request("DELETE", f"/faxes/{quote(fax_id, safe='')}")

    def cancel(self, fax_id: str) -> None:
        self._request("POST", f"/faxes/{quote(fax_id, safe='')}/cancel")

    def list_sent(self, **query: Any) -> list[FaxMetadata]:
        return self._list("/faxes/sent", query)

    def list_received(self, **query: Any) -> list[FaxMetadata]:
        return self._list("/faxes/received", query)

    def iter_sent(self, *, page_size: int = MAX_PAGE_SIZE, **query: Any) -> Iterator[FaxMetadata]:
        yield from self._iter("/faxes/sent", query, page_size)

    def iter_received(
        self, *, page_size: int = MAX_PAGE_SIZE, **query: Any
    ) -> Iterator[FaxMetadata]:
        yield from self._iter("/faxes/received", query, page_size)

    def find_sent(self, client_reference_id: str, **query: Any) -> FaxMetadata | None:
        """Reconcile a SendUncertain: did a fax with our reference actually go out?"""
        for fax in self.iter_sent(client_reference_id=client_reference_id, **query):
            if fax.client_reference_id == client_reference_id:
                return fax
        return None

    def _list(
        self, path: str, query: dict[str, Any], headers: dict[str, str] | None = None
    ) -> list[FaxMetadata]:
        resp = self._request("GET", path, query=_encode_query(query), headers=headers)
        data = _json(resp)
        items = data if isinstance(data, list) else _first_list(data)
        return [FaxMetadata.from_response(item) for item in items]

    def _iter(self, path: str, query: dict[str, Any], page_size: int) -> Iterator[FaxMetadata]:
        offset = 0
        while True:
            page = self._list(
                path,
                query,
                {"pagination-offset": str(offset), "pagination-limit": str(page_size)},
            )
            yield from page
            if len(page) < page_size:
                return
            offset += page_size


def _encode_query(query: dict[str, Any]) -> dict[str, Any]:
    return {k: iso8601(v) if isinstance(v, datetime) else v for k, v in query.items()}


def _json(resp: Response) -> Any:
    if not resp.body:
        return {}
    try:
        return json.loads(resp.body)
    except ValueError:
        raise errors.EFaxError("eFax returned non-JSON", status=resp.status) from None


def _first_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("faxes", "items", "data", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _error_code(resp: Response) -> str | None:
    """eFax errors: {"transaction_id", "errors": [{"error_code", "developer_message", ...}]}."""
    try:
        data = json.loads(resp.body or b"{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    errs = data.get("errors")
    if isinstance(errs, list) and errs and isinstance(errs[0], dict):
        code = errs[0].get("error_code")
    else:
        code = data.get("error_code")
    return str(code) if code is not None else None


def _error_summary(resp: Response) -> str:
    # Only the code: error bodies can echo request fields such as the destination number.
    code = _error_code(resp)
    return f"(code {code})" if code else ""
