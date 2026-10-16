"""Typed request/response shapes.

Field names follow eFax's Fax Services OAS2 spec (Stoplight project consensus/fax-services, v1.2).
Every response object also keeps the untouched payload in `.raw`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .numbers import normalize_fax_number


def _pick(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if raw.get(name) not in (None, ""):
            return raw[name]
    return None


@dataclass(frozen=True)
class Destination:
    fax_number: str
    to_name: str | None = None
    to_company: str | None = None

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"fax_number": normalize_fax_number(self.fax_number)}
        if self.to_name:
            out["to_name"] = self.to_name
        if self.to_company:
            out["to_company"] = self.to_company
        return out


@dataclass(frozen=True)
class Document:
    content: bytes
    document_type: str = "PDF"

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "document_content": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class CoverPage:
    from_name: str | None = None
    subject: str | None = None
    message: str | None = None


@dataclass
class SendRequest:
    destinations: list[Destination]
    documents: list[Document]
    # Our own id for this send (e.g. the app's FaxTransmission pk). eFax stores it in client_data,
    # which is what lets find_sent() reconcile a SendUncertain.
    client_reference_id: str | None = None
    cover_page: CoverPage | None = None
    image_resolution: str = "FINE"
    header_line: list[str] | None = None
    custom_csid: str | None = None
    client_data: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        if not self.destinations:
            raise ValueError("a fax needs at least one destination")
        if not self.documents:
            raise ValueError("a fax needs at least one document")
        options: dict[str, Any] = {
            "image_resolution": self.image_resolution,
            "include_cover_page": "true" if self.cover_page else "false",
        }
        if self.cover_page:
            options["cover_page_options"] = {
                k: v
                for k, v in (
                    ("from_name", self.cover_page.from_name),
                    ("subject", self.cover_page.subject),
                    ("message", self.cover_page.message),
                )
                if v
            }
        if self.header_line:
            options["header_options"] = {"line_1": list(self.header_line)}
        if self.custom_csid:
            options["custom_CSID"] = self.custom_csid
        client_data = dict(self.client_data)
        if self.client_reference_id:
            client_data["client_reference_id"] = self.client_reference_id
        payload: dict[str, Any] = {
            "destinations": [d.to_payload() for d in self.destinations],
            "documents": [d.to_payload() for d in self.documents],
            "fax_options": options,
        }
        if client_data:
            payload["client_data"] = client_data
        return payload


@dataclass(frozen=True)
class FaxRef:
    """One accepted destination. POST /faxes answers 201 with one entry per destination.

    Accepted is not delivered: eFax only promises delivery once transmission_status is COMPLETE.
    """

    fax_id: str
    destination_fax_number: str | None
    transaction_id: str
    raw: dict[str, Any]

    @classmethod
    def list_from_response(cls, raw: Any, transaction_id: str) -> list[FaxRef]:
        items = raw if isinstance(raw, list) else [raw]
        refs = [
            cls(
                fax_id=str(item["fax_id"]),
                destination_fax_number=item.get("destination_fax_number"),
                transaction_id=transaction_id,
                raw=item,
            )
            for item in items
            if isinstance(item, dict) and item.get("fax_id")
        ]
        if not refs:
            raise ValueError("eFax accepted the fax but returned no fax_id")
        return refs


# transmission_data.transmission_status (outbound delivery). The /faxes/sent filter enum spells
# INPROGRESS as INPROGESS, and the transmission-detail docs add PENDING RETRY / IN PROGRESS.
DELIVERED = frozenset({"COMPLETE"})
FAILED = frozenset({"ERROR", "CANCELED"})


@dataclass(frozen=True)
class FaxMetadata:
    fax_id: str
    # Delivery outcome for outbound faxes (transmission_data.transmission_status); None inbound.
    # Not to be confused with eFax's `fax_status`, which is image storage (STORED / NOT_STORED).
    transmission_status: str | None
    direction: str | None
    pages: int | None
    error_code: str | None
    error_message: str | None
    raw: dict[str, Any]

    @classmethod
    def from_response(cls, raw: dict[str, Any]) -> FaxMetadata:
        tx = raw.get("transmission_data") if isinstance(raw.get("transmission_data"), dict) else {}
        pages = raw.get("pages")
        return cls(
            fax_id=str(raw.get("fax_id") or ""),
            transmission_status=_pick(tx, "transmission_status") or _pick(raw, "status"),
            direction=_pick(raw, "direction", "fax_direction"),
            pages=int(pages) if pages not in (None, "") else None,
            error_code=_pick(tx, "error_code"),
            error_message=_pick(tx, "error_message"),
            raw=raw,
        )

    @property
    def is_delivered(self) -> bool:
        return (self.transmission_status or "").upper() in DELIVERED

    @property
    def is_failed(self) -> bool:
        return (self.transmission_status or "").upper() in FAILED

    @property
    def client_reference_id(self) -> str | None:
        # `client_tracking_data` on /metadata, `client_data` on /faxes/sent and webhooks.
        for key in ("client_tracking_data", "client_data"):
            data = self.raw.get(key)
            if isinstance(data, dict) and data.get("client_reference_id"):
                return data["client_reference_id"]
        return None


def iso8601(value: datetime) -> str:
    """The list endpoints want yyyy-mm-ddThh:mm:ss.fff±hhmm with an explicit offset."""
    if value.tzinfo is None:
        raise ValueError("pass a timezone-aware datetime")
    return (
        value.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{value.microsecond // 1000:03d}"
        + value.strftime("%z")
    )
