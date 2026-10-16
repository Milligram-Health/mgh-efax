import hashlib
import hmac
import io
import json
from datetime import timedelta

import pytest
from django.conf import settings as dj_settings
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from fakes import FakeTransport, TransportError, make_client, resp
from tests_app.models import Intervention

from mgh_efax.contrib.django import get_client
from mgh_efax.contrib.django.api import faxes_for, send_fax
from mgh_efax.contrib.django.models import InboundFax, InboundState, OutboundFax, OutboundState
from mgh_efax.contrib.django.services import process_inbound, reconcile, refresh, submit, sweep
from mgh_efax.contrib.django.signals import inbound_fax_received, outbound_fax_finished

ACCEPTED = resp(201, [{"fax_id": "F1", "destination_fax_number": "16125550142"}])


@pytest.fixture(autouse=True)
def _clean():
    OutboundFax.objects.all().delete()
    InboundFax.objects.all().delete()
    Intervention.objects.all().delete()


@pytest.fixture
def efax_settings():
    original = dj_settings.EFAX
    yield dj_settings
    dj_settings.EFAX = original


@pytest.fixture
def connect():
    connected = []

    def _connect(signal, fn):
        signal.connect(fn, weak=False)
        connected.append((signal, fn))

    yield _connect
    for signal, fn in connected:
        signal.disconnect(fn)


def _queue(**kw):
    kw.setdefault("to", "612-555-0142")
    kw.setdefault("document", b"%PDF-1.7 intervention")
    return send_fax(**kw)


def _force(fax, **fields):
    OutboundFax.objects.filter(pk=fax.pk).update(**fields)
    fax.refresh_from_db()
    return fax


def test_get_client_region_and_override(efax_settings):
    assert get_client().config.base_url == "https://api.securedocex.us"
    efax_settings.EFAX = {**efax_settings.EFAX, "REGION": "ca"}
    assert get_client().config.base_url == "https://api.securedocex.ca"
    efax_settings.EFAX = {**efax_settings.EFAX, "BASE_URL": "https://alt.example"}
    client = get_client(user_id="u2")
    assert (client.config.base_url, client.config.user_id) == ("https://alt.example", "u2")


def test_send_fax_writes_one_shared_outbox_row_linked_to_any_object():
    iv = Intervention.objects.create(title="x")
    fax = _queue(source=iv, tag="referrals.letter", to_name="Main St", cover_subject="Hi")
    assert fax.state == OutboundState.QUEUED and fax.to_number == "16125550142"
    assert fax.source == iv and list(faxes_for(iv)) == [fax]
    with fax.document.open("rb") as fh:
        assert fh.read() == b"%PDF-1.7 intervention"
    with pytest.raises(ValueError):
        _queue(to="555")


def test_submit_sends_document_and_reference():
    fax = _queue(cover_subject="Intervention")
    t = FakeTransport().on("POST", "/faxes", ACCEPTED)
    fax = submit(fax, client=make_client(t))
    assert (fax.state, fax.efax_fax_id, fax.attempts) == (OutboundState.SUBMITTED, "F1", 1)
    headers, body = t.calls[-1][2], json.loads(t.calls[-1][3])
    assert body["client_data"]["client_reference_id"] == str(fax.reference)
    assert body["fax_options"]["cover_page_options"] == {"subject": "Intervention"}
    assert headers["transaction-id"] == str(fax.reference)


def test_submit_claims_row_so_it_is_never_sent_twice():
    fax = _queue()
    t = FakeTransport().on("POST", "/faxes", ACCEPTED)
    submit(fax, client=make_client(t))
    stale_copy = OutboundFax.objects.get(pk=fax.pk)
    stale_copy.state = OutboundState.QUEUED  # in memory only; the DB says SUBMITTED
    submit(stale_copy, client=make_client(t))
    assert t.paths().count("POST /faxes") == 1


def test_submit_outcomes(connect):
    finished = []
    connect(outbound_fax_finished, lambda sender, fax, **kw: finished.append(fax.state))

    t = FakeTransport().on("POST", "/faxes", TransportError("timeout", sent=True))
    assert submit(_queue(), client=make_client(t)).state == OutboundState.UNCERTAIN

    t2 = FakeTransport().on("POST", "/faxes", resp(400, {"errors": [{"error_code": "110"}]}))
    f2 = submit(_queue(), client=make_client(t2))
    assert (f2.state, f2.error_code) == (OutboundState.FAILED, "110")
    assert finished == [OutboundState.FAILED]

    t3 = FakeTransport().on("POST", "/faxes", resp(429))
    assert submit(_queue(), client=make_client(t3)).state == OutboundState.QUEUED


@pytest.mark.parametrize(
    "status,state",
    [
        ("COMPLETE", OutboundState.DELIVERED),
        ("ERROR", OutboundState.FAILED),
        ("CANCELED", OutboundState.FAILED),
        ("INPROGRESS", OutboundState.SUBMITTED),
    ],
)
def test_refresh(status, state):
    fax = _force(_queue(), state=OutboundState.SUBMITTED, efax_fax_id="F1")
    body = {
        "fax_id": "F1",
        "pages": 2,
        "transmission_data": {"transmission_status": status, "error_code": "E44"},
    }
    t = FakeTransport().on("GET", "/faxes/F1/metadata", resp(200, body))
    fax = refresh(fax, client=make_client(t))
    assert fax.state == state and fax.page_count == 2


def test_reconcile_found_and_given_up():
    fax = _force(_queue(), state=OutboundState.UNCERTAIN)
    ref = {"client_reference_id": str(fax.reference)}
    t = FakeTransport().on(
        "GET", "/faxes/sent", resp(200, {"faxes": [{"fax_id": "F7", "client_data": ref}]})
    )
    assert reconcile(fax, client=make_client(t)).state == OutboundState.SUBMITTED

    other = _force(
        _queue(), state=OutboundState.UNCERTAIN, updated_at=timezone.now() - timedelta(hours=1)
    )
    empty = FakeTransport().on("GET", "/faxes/sent", resp(200, {"faxes": []}))
    assert reconcile(other, client=make_client(empty)).state == OutboundState.QUEUED


def test_sweep_sends_queued_and_marks_stale_sending_uncertain():
    queued = _queue()
    stuck = _force(
        _queue(), state=OutboundState.SENDING, updated_at=timezone.now() - timedelta(hours=1)
    )
    t = (
        FakeTransport()
        .on("POST", "/faxes", ACCEPTED)
        .on("GET", "/faxes/sent", resp(200, {"faxes": []}))
    )
    counts = sweep(client=make_client(t))
    assert counts["submitted"] == 1 and counts["stale"] == 1
    assert OutboundFax.objects.get(pk=queued.pk).state == OutboundState.SUBMITTED
    assert OutboundFax.objects.get(pk=stuck.pk).state == OutboundState.UNCERTAIN


def _signed_post(payload, secret="s3cret"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        "/webhooks/efax/", body, content_type="application/json", headers={"X-HMAC-Signature": sig}
    )


def test_inbound_webhook_records_once_then_processing_stores_and_hands_off(connect):
    payload = {
        "fax_id": "IN1",
        "direction": "INBOUND",
        "notification_id": "N1",
        "pages": 1,
        "sending_fax_number": "16125550142",
        "receiving_fax_number": "16125550199",
    }
    assert _signed_post(payload).status_code == 200
    assert _signed_post(payload).status_code == 200  # eFax redelivery is a no-op
    assert _signed_post(payload, secret="wrong").status_code == 403
    fax = InboundFax.objects.get()
    assert (fax.state, fax.from_number) == (InboundState.RECEIVED, "16125550142")

    handled = []

    def receiver(sender, fax, **kw):
        with fax.image.open("rb") as fh:
            handled.append(fh.read())

    connect(inbound_fax_received, receiver)
    image = {"fax_id": "IN1", "image": "JVBERi1pbmJvdW5k"}  # base64 of %PDF-inbound
    t = FakeTransport().on("GET", "/faxes/IN1/image", resp(200, image))
    fax = process_inbound(fax, client=make_client(t))
    assert fax.state == InboundState.HANDLED and handled == [b"%PDF-inbound"]


def test_inbound_handler_failure_is_kept_for_retry(connect):
    def boom(sender, fax, **kw):
        raise RuntimeError("queue down")

    connect(inbound_fax_received, boom)
    fax = InboundFax.objects.create(efax_fax_id="IN2")
    t = FakeTransport().on("GET", "/faxes/IN2/image", resp(200, {"image": "JVBERg=="}))
    fax = process_inbound(fax, client=make_client(t))
    assert fax.state == InboundState.HANDLER_FAILED and "queue down" in fax.handler_error
    assert fax.image  # stored; the retry won't download again


def test_outbound_status_webhook_finishes_the_row():
    fax = _force(_queue(), state=OutboundState.UNCERTAIN)
    payload = {
        "fax_id": "F9",
        "direction": "OUTBOUND",
        "status": "COMPLETE",
        "client_data": {"client_reference_id": str(fax.reference)},
    }
    assert _signed_post(payload).status_code == 200
    fax.refresh_from_db()
    assert (fax.state, fax.efax_fax_id) == (OutboundState.DELIVERED, "F9")


def test_sweep_command_runs_with_nothing_to_do():
    out = io.StringIO()
    call_command("efax_sweep", stdout=out)
    assert "submitted=0" in out.getvalue()


def test_sending_disabled_suppresses_without_calling_efax(efax_settings, connect):
    finished = []
    connect(outbound_fax_finished, lambda sender, fax, **kw: finished.append(fax.state))
    efax_settings.EFAX = {**efax_settings.EFAX, "SENDING_ENABLED": False}
    t = FakeTransport()
    fax = submit(_queue(), client=make_client(t))
    assert fax.state == OutboundState.SUPPRESSED and fax.is_finished
    assert t.calls == []  # not even a token request
    assert finished == [OutboundState.SUPPRESSED]
    efax_settings.EFAX = {**efax_settings.EFAX, "SENDING_ENABLED": True}
    assert submit(fax, client=make_client(t)).state == OutboundState.SUPPRESSED  # final


def test_redirect_dials_test_number_and_marks_the_cover(efax_settings):
    efax_settings.EFAX = {**efax_settings.EFAX, "REDIRECT_TO": "+1 716 985 9039"}
    fax = _queue(to_name="Main St", cover_subject="Intervention", cover_message="See attached")
    t = FakeTransport().on("POST", "/faxes", ACCEPTED)
    fax = submit(fax, client=make_client(t))
    body = json.loads(t.calls[-1][3])
    assert body["destinations"][0]["fax_number"] == "17169859039"
    cover = body["fax_options"]["cover_page_options"]
    assert cover["subject"] == "[TEST] Intervention"
    assert "Main St *******0142" in cover["message"] and cover["message"].endswith("See attached")
    assert (fax.to_number, fax.dialed_number) == ("16125550142", "17169859039")
    assert fax.was_redirected and fax.state == OutboundState.SUBMITTED


def test_no_redirect_dials_the_real_number():
    t = FakeTransport().on("POST", "/faxes", ACCEPTED)
    fax = submit(_queue(), client=make_client(t))
    assert json.loads(t.calls[-1][3])["destinations"][0]["fax_number"] == "16125550142"
    assert fax.dialed_number == "16125550142" and not fax.was_redirected


def test_invalid_redirect_number_fails_closed(efax_settings):
    efax_settings.EFAX = {**efax_settings.EFAX, "REDIRECT_TO": "555"}
    t = FakeTransport()
    fax = submit(_queue(), client=make_client(t))
    assert fax.state == OutboundState.FAILED and t.paths().count("POST /faxes") == 0
