import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeTransport, TransportError, make_client, resp

from mgh_efax import (
    AuthError,
    CoverPage,
    Destination,
    Document,
    NotFound,
    RateLimited,
    RequestRejected,
    SendRequest,
    SendUncertain,
)


def _send_request():
    return SendRequest(
        destinations=[Destination("(612) 555-0142", to_name="Main St Pharmacy")],
        documents=[Document(b"%PDF-1.7 fake")],
        client_reference_id="ref-1",
        cover_page=CoverPage(subject="Intervention"),
    )


def test_token_uses_basic_auth_and_is_cached():
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(200, {"fax_id": "F1"}))
    c = make_client(t)
    c.get_metadata("F1")
    c.get_metadata("F1")
    assert t.paths().count("POST /tokens") == 1
    _, _, headers, body = t.calls[0]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"app:secret-key").decode()
    assert body == b"grant_type=client_credentials"
    assert t.calls[1][2]["Authorization"] == "Bearer tok"
    assert t.calls[1][2]["user-id"] == "u1"


def test_token_refreshes_before_expiry():
    now = [0.0]
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(200, {"fax_id": "F1"}))
    c = make_client(t, clock=lambda: now[0])
    c.get_metadata("F1")
    now[0] = 3600 - 30  # inside the 60s skew
    c.get_metadata("F1")
    assert t.paths().count("POST /tokens") == 2


def test_401_refreshes_token_once_then_raises():
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(401), resp(200, {"fax_id": "F1"}))
    assert make_client(t).get_metadata("F1").fax_id == "F1"
    assert t.paths().count("POST /tokens") == 2

    t2 = FakeTransport().on("GET", "/faxes/F1/metadata", resp(401))
    with pytest.raises(AuthError):
        make_client(t2).get_metadata("F1")


def test_bad_credentials():
    t = FakeTransport().on("POST", "/tokens", resp(401))
    with pytest.raises(AuthError):
        make_client(t).get_metadata("F1")


def test_send_fax_payload_and_headers():
    t = FakeTransport().on(
        "POST", "/faxes", resp(201, [{"fax_id": "F9", "destination_fax_number": "16125550142"}])
    )
    ref = make_client(t).send_fax(_send_request(), transaction_id="tx-1")
    assert ref.fax_id == "F9" and ref.transaction_id == "tx-1"
    _, _, headers, body = t.calls[-1]
    assert headers["transaction-id"] == "tx-1"
    payload = json.loads(body)
    assert payload["destinations"] == [{"fax_number": "16125550142", "to_name": "Main St Pharmacy"}]
    assert base64.b64decode(payload["documents"][0]["document_content"]) == b"%PDF-1.7 fake"
    assert payload["documents"][0]["document_type"] == "PDF"
    assert payload["fax_options"]["include_cover_page"] == "true"
    assert payload["fax_options"]["cover_page_options"] == {"subject": "Intervention"}
    assert payload["client_data"] == {"client_reference_id": "ref-1"}


def test_send_is_not_retried_on_5xx_or_timeout():
    t = FakeTransport().on("POST", "/faxes", resp(502))
    with pytest.raises(SendUncertain):
        make_client(t).send_fax(_send_request())
    assert t.paths().count("POST /faxes") == 1

    t2 = FakeTransport().on("POST", "/faxes", TransportError("timeout", sent=True))
    with pytest.raises(SendUncertain):
        make_client(t2).send_fax(_send_request())
    assert t2.paths().count("POST /faxes") == 1


def test_send_retries_when_provably_not_sent():
    t = FakeTransport().on(
        "POST",
        "/faxes",
        resp(429, headers={"retry-after": "1"}),
        TransportError("refused", sent=False),
        resp(201, [{"fax_id": "F9", "destination_fax_number": "16125550142"}]),
    )
    assert make_client(t).send_fax(_send_request()).fax_id == "F9"
    assert t.paths().count("POST /faxes") == 3


def test_reads_retry_5xx_then_give_up():
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(503), resp(200, {"fax_id": "F1"}))
    assert make_client(t).get_metadata("F1").fax_id == "F1"

    t2 = FakeTransport().on("GET", "/faxes/F1/metadata", resp(429))
    with pytest.raises(RateLimited):
        make_client(t2).get_metadata("F1")
    assert t2.paths().count("GET /faxes/F1/metadata") == 4  # 1 + max_retries


def test_error_mapping():
    t = FakeTransport().on("GET", "/faxes/nope/metadata", resp(404))
    with pytest.raises(NotFound):
        make_client(t).get_metadata("nope")
    t2 = FakeTransport().on(
        "POST",
        "/faxes",
        resp(
            400,
            {"transaction_id": "t", "errors": [{"error_code": "110", "developer_message": "x"}]},
        ),
    )
    with pytest.raises(RequestRejected) as exc:
        make_client(t2).send_fax(_send_request())
    assert exc.value.code == "110"
    assert "16125550142" not in str(exc.value)


def test_metadata_parsing_uses_transmission_status_not_fax_status():
    raw = {
        "fax_id": "F1",
        "fax_status": "STORED",  # image storage, not delivery
        "direction": "OUTBOUND",
        "pages": 3,
        "transmission_data": {
            "transmission_status": "ERROR",
            "error_code": "E44",
            "error_message": "No answer",
        },
        "client_tracking_data": {"client_reference_id": "ref-1"},
    }
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(200, raw))
    meta = make_client(t).get_metadata("F1")
    assert (meta.transmission_status, meta.direction, meta.pages) == ("ERROR", "OUTBOUND", 3)
    assert meta.is_failed and not meta.is_delivered
    assert (meta.error_code, meta.error_message) == ("E44", "No answer")
    assert meta.client_reference_id == "ref-1"
    assert meta.raw == raw


def test_download_image_decodes_json_and_asks_for_pdf():
    enc = base64.b64encode(b"%PDF-bytes").decode()
    body = {"fax_id": "F1", "file_name": "F1.pdf", "image": enc}
    t = FakeTransport().on("GET", "/faxes/F1/image", resp(200, body))
    assert make_client(t).download_image("F1") == b"%PDF-bytes"
    assert t.calls[-1][1].endswith("/faxes/F1/image?desired_format=PDF")


def test_send_multi_returns_one_ref_per_destination_and_enforces_limits():
    t = FakeTransport().on("POST", "/faxes", resp(201, [{"fax_id": "A"}, {"fax_id": "B"}]))
    req = _send_request()
    req.destinations.append(Destination("6125550143"))
    refs = make_client(t).send_fax_multi(req)
    assert [r.fax_id for r in refs] == ["A", "B"]
    with pytest.raises(ValueError):
        make_client(FakeTransport()).send_fax(req)  # two destinations
    req.destinations[:] = [Destination("6125550142")] * 51
    with pytest.raises(ValueError):
        make_client(FakeTransport()).send_fax_multi(req)


def test_list_and_pagination_and_find_sent():
    page1 = {"faxes": [{"fax_id": f"F{i}"} for i in range(2)]}
    page2 = {"faxes": [{"fax_id": "F2", "client_data": {"client_reference_id": "ref-x"}}]}
    t = FakeTransport().on("GET", "/faxes/sent", resp(200, page1), resp(200, page2))
    found = make_client(t).find_sent("ref-x", page_size=2)
    assert found and found.fax_id == "F2"
    assert "client_reference_id=ref-x" in t.calls[-1][1]  # filtered server-side too
    offsets = [h["pagination-offset"] for m, u, h, _ in t.calls if "/faxes/sent" in u]
    assert offsets == ["0", "2"]


def test_list_encodes_datetimes():
    t = FakeTransport().on("GET", "/faxes/received", resp(200, []))
    tz = timezone(timedelta(hours=-8))
    make_client(t).list_received(
        min_completed_timestamp=datetime(2021, 6, 9, 9, 17, 32, 625000, tz)
    )
    url = t.calls[-1][1]
    assert "min_completed_timestamp=2021-06-09T09%3A17%3A32.625-0800" in url


def test_config_repr_hides_key():
    assert "secret-key" not in repr(make_client(FakeTransport()).config)


def test_region_selects_sovereign_host_and_base_url_overrides():
    from mgh_efax import DEFAULT_BASE_URL, EFaxConfig

    assert DEFAULT_BASE_URL == "https://api.securedocex.us"
    cfg = dict(app_id="a", api_key="k", user_id="u")
    assert EFaxConfig(**cfg).base_url == "https://api.securedocex.us"
    assert EFaxConfig(**cfg, region="CA").base_url == "https://api.securedocex.ca"
    assert EFaxConfig(**cfg, region="global").base_url == "https://api.securedocex.com"
    assert EFaxConfig(**cfg, region="ca", base_url="https://x.example/v2/").base_url == (
        "https://x.example/v2"
    )
    with pytest.raises(ValueError):
        EFaxConfig(**cfg, region="us/../evil")
    with pytest.raises(ValueError):
        EFaxConfig(**cfg, base_url="http://plain.example")
