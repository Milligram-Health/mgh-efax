import base64
import hashlib
import hmac
import json

import pytest

from mgh_efax import (
    WebhookSignatureError,
    mask_number,
    normalize_fax_number,
    parse_notification,
    verify_signature,
)


@pytest.mark.parametrize(
    "raw", ["(612) 555-0142", "+1 612.555.0142", "6125550142", "1-612-555-0142"]
)
def test_normalize(raw):
    assert normalize_fax_number(raw) == "16125550142"


@pytest.mark.parametrize(
    "raw", ["", "555-0142", "+44 20 7946 0958", "1 012 555 0142", "612 155 0142"]
)
def test_normalize_rejects(raw):
    with pytest.raises(ValueError) as exc:
        normalize_fax_number(raw)
    assert raw == "" or raw not in str(exc.value)  # the full number never reaches a log line


def test_mask():
    assert mask_number("16125550142") == "*******0142"
    assert mask_number(None) == ""


def _sign(body, secret="s3cret", encoding="hex"):
    d = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(d).decode() if encoding == "base64" else d.hex()


def test_verify_signature_ok_and_variants():
    body = b'{"fax_id":"F1"}'
    verify_signature(body, _sign(body), "s3cret")  # eFax default: hex
    verify_signature(body, _sign(body, encoding="base64"), "s3cret", encoding="base64")
    verify_signature(body, "sha256=" + _sign(body), "s3cret")


@pytest.mark.parametrize("sig,secret", [(None, "s3cret"), ("bad", "s3cret"), ("x", "")])
def test_verify_signature_rejects(sig, secret):
    with pytest.raises(WebhookSignatureError):
        verify_signature(b"{}", sig, secret)


def test_verify_rejects_tampered_body():
    sig = _sign(b'{"fax_id":"F1"}')
    with pytest.raises(WebhookSignatureError):
        verify_signature(b'{"fax_id":"F2"}', sig, "s3cret")


def test_parse_inbound_notification():
    payload = {
        "app_id": "A",
        "user_id": "U",
        "notification_id": "N1",
        "direction": "INBOUND",
        "fax_id": "F1",
        "pages": 2,
        "sending_fax_number": "16125550142",
        "receiving_fax_number": "16125550199",
        "resource_url": "https://evil.example/steal",
    }
    n = parse_notification(json.dumps(payload).encode())
    assert (n.fax_id, n.notification_id, n.pages, n.is_inbound) == ("F1", "N1", 2, True)
    assert (n.from_number, n.to_number) == ("16125550142", "16125550199")
    with pytest.raises(ValueError):
        parse_notification(b'{"nothing": 1}')
    with pytest.raises(ValueError):
        parse_notification(b"not json")


def test_parse_outbound_status_notification():
    payload = {
        "fax_id": "F2",
        "direction": "OUTBOUND",
        "status": "ERROR",
        "error_reason": "Busy",
        "client_data": {"client_reference_id": "ref-1"},
    }
    n = parse_notification(json.dumps(payload).encode())
    assert not n.is_inbound
    assert (n.status, n.error, n.client_reference_id) == ("ERROR", "Busy", "ref-1")
