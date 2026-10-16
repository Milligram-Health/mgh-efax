"""One eFax account per tenant: ACCOUNT_RESOLVER and WEBHOOK_SECRET_RESOLVER."""

import hashlib
import hmac
import json

import pytest
from django.conf import settings as dj_settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from fakes import FakeTransport, make_client, resp

from mgh_efax.contrib.django.api import send_fax
from mgh_efax.contrib.django.models import InboundFax, InboundState, OutboundFax, OutboundState
from mgh_efax.contrib.django.services import (
    poll_received,
    process_inbound,
    reconcile,
    refresh,
    submit,
    sweep,
)
from mgh_efax.contrib.django.signals import outbound_fax_finished

ACCEPTED = resp(201, [{"fax_id": "F1", "destination_fax_number": "16125550142"}])


@pytest.fixture(autouse=True)
def _clean():
    OutboundFax.objects.all().delete()
    InboundFax.objects.all().delete()


@pytest.fixture
def efax(monkeypatch):
    """Set EFAX keys for one test; the global APP_ID/API_KEY/USER_ID are removed, since a
    resolver makes them unnecessary."""
    original = dj_settings.EFAX

    def _set(**overrides):
        base = {k: v for k, v in original.items() if k not in ("APP_ID", "API_KEY", "USER_ID")}
        dj_settings.EFAX = {**base, **overrides}

    yield _set
    dj_settings.EFAX = original


class Accounts:
    """A resolver over two tenants, each with its own fake eFax account."""

    def __init__(self):
        self.transports = {"org-a": FakeTransport(), "org-b": FakeTransport()}
        self.clients = {k: make_client(t) for k, t in self.transports.items()}
        self.calls = []
        self.disabled = set()

    def __call__(self, row, purpose):
        key = getattr(row, "account_key", None) or {"app-a": "org-a", "app-b": "org-b"}.get(
            getattr(row, "app_id", "")
        )
        self.calls.append((key, purpose))
        if key in self.disabled or key not in self.clients:
            return None
        return self.clients[key]


def _queue(account_key):
    return send_fax(to="612-555-0142", document=b"%PDF", account_key=account_key)


def test_submit_uses_the_rows_own_account(efax):
    accounts = Accounts()
    accounts.transports["org-b"].on("POST", "/faxes", ACCEPTED)
    efax(ACCOUNT_RESOLVER=accounts)
    fax = submit(_queue("org-b"))
    assert fax.state == OutboundState.SUBMITTED and fax.account_key == "org-b"
    assert accounts.calls == [("org-b", "send")]
    assert accounts.transports["org-b"].paths().count("POST /faxes") == 1
    assert accounts.transports["org-a"].calls == []


def test_resolver_none_suppresses_and_dials_nothing(efax):
    finished = []

    def receiver(sender, fax, **kw):
        finished.append(fax.state)

    outbound_fax_finished.connect(receiver, weak=False)
    try:
        accounts = Accounts()
        accounts.disabled.add("org-a")
        efax(ACCOUNT_RESOLVER=accounts)
        fax = submit(_queue("org-a"))
    finally:
        outbound_fax_finished.disconnect(receiver)
    assert fax.state == OutboundState.SUPPRESSED
    assert fax.error == "sending disabled for this account"
    assert finished == [OutboundState.SUPPRESSED]
    assert accounts.transports["org-a"].calls == []


def test_sending_enabled_false_wins_before_the_resolver(efax):
    accounts = Accounts()
    efax(ACCOUNT_RESOLVER=accounts, SENDING_ENABLED=False)
    fax = submit(_queue("org-a"))
    assert fax.state == OutboundState.SUPPRESSED and accounts.calls == []


def test_resolver_error_requeues_without_dialling(efax):
    def broken(row, purpose):
        raise RuntimeError("could not decrypt")

    efax(ACCOUNT_RESOLVER=broken)
    fax = submit(_queue("org-a"))
    assert fax.state == OutboundState.QUEUED and "RuntimeError" in fax.error
    assert "decrypt" not in fax.error  # exception text can carry secrets


def test_track_with_no_account_leaves_rows_untouched(efax):
    accounts = Accounts()
    accounts.disabled.add("org-a")
    efax(ACCOUNT_RESOLVER=accounts)
    submitted = _queue("org-a")
    OutboundFax.objects.filter(pk=submitted.pk).update(
        state=OutboundState.SUBMITTED, efax_fax_id="F1"
    )
    submitted.refresh_from_db()
    assert refresh(submitted).state == OutboundState.SUBMITTED

    uncertain = _queue("org-a")
    OutboundFax.objects.filter(pk=uncertain.pk).update(state=OutboundState.UNCERTAIN)
    uncertain.refresh_from_db()
    assert reconcile(uncertain).state == OutboundState.UNCERTAIN
    assert ("org-a", "track") in accounts.calls
    assert accounts.transports["org-a"].calls == []


def test_inbound_with_no_account_is_retried_not_lost(efax):
    accounts = Accounts()
    efax(ACCOUNT_RESOLVER=accounts)
    fax = InboundFax.objects.create(efax_fax_id="IN9", app_id="app-unknown")
    fax = process_inbound(fax)
    assert fax.state == InboundState.RECEIVED and fax.attempts == 1
    assert "no eFax account" in fax.handler_error
    fax = process_inbound(fax)
    assert fax.attempts == 2  # still in the retry path


def test_inbound_downloads_with_its_own_account(efax):
    accounts = Accounts()
    accounts.transports["org-b"].on("GET", "/faxes/IN1/image", resp(200, {"image": "JVBERg=="}))
    efax(ACCOUNT_RESOLVER=accounts)
    fax = process_inbound(InboundFax.objects.create(efax_fax_id="IN1", app_id="app-b"))
    assert fax.state == InboundState.HANDLED and accounts.calls == [("org-b", "inbound")]


def test_sweep_resolves_each_row_separately(efax):
    accounts = Accounts()
    accounts.transports["org-a"].on("POST", "/faxes", ACCEPTED)
    accounts.transports["org-b"].on("POST", "/faxes", ACCEPTED)
    efax(ACCOUNT_RESOLVER=accounts)
    _queue("org-a")
    _queue("org-b")
    assert sweep()["submitted"] == 2
    assert sorted(k for k, p in accounts.calls if p == "send") == ["org-a", "org-b"]
    for t in accounts.transports.values():
        assert t.paths().count("POST /faxes") == 1


def test_poll_inbound_with_resolver_is_rejected(efax):
    efax(ACCOUNT_RESOLVER=Accounts(), POLL_INBOUND=True)
    with pytest.raises(ImproperlyConfigured):
        poll_received()
    with pytest.raises(ImproperlyConfigured):
        sweep()


def test_resolver_required_row():
    from mgh_efax.contrib.django import get_client

    original = dj_settings.EFAX
    dj_settings.EFAX = {**original, "ACCOUNT_RESOLVER": Accounts()}
    try:
        with pytest.raises(ImproperlyConfigured):
            get_client()
    finally:
        dj_settings.EFAX = original


# ---- webhook ------------------------------------------------------------------------------------

SECRETS = {"app-a": "secret-a", "app-b": "secret-b"}


def _post(payload, secret, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        "/webhooks/efax/", body, content_type="application/json", headers={"X-HMAC-Signature": sig}
    )


@pytest.fixture
def per_account_secrets(efax):
    efax(WEBHOOK_SECRET="", WEBHOOK_SECRET_RESOLVER=lambda p: SECRETS.get(p.get("app_id"), ""))


def _inbound(app_id, fax_id):
    return {"fax_id": fax_id, "direction": "INBOUND", "app_id": app_id, "pages": 1}


def test_each_account_verifies_only_with_its_own_secret(per_account_secrets):
    assert _post(_inbound("app-a", "A1"), "secret-a").status_code == 200
    assert _post(_inbound("app-b", "B1"), "secret-b").status_code == 200
    assert _post(_inbound("app-a", "A2"), "secret-b").status_code == 403  # other tenant's secret
    assert _post(_inbound("app-a", "A3"), "wrong").status_code == 403
    assert {f.efax_fax_id: f.app_id for f in InboundFax.objects.all()} == {
        "A1": "app-a",
        "B1": "app-b",
    }


def test_unknown_account_and_unparseable_body_are_rejected(per_account_secrets):
    assert _post(_inbound("app-x", "X1"), "").status_code == 403  # resolver returns ""
    assert _post(None, "secret-a", raw=b"not json").status_code == 403
    assert _post(None, "secret-a", raw=b"[1, 2]").status_code == 403
    assert not InboundFax.objects.exists()


def test_single_secret_mode_still_records_app_id(efax):
    efax(WEBHOOK_SECRET="s3cret")
    assert _post(_inbound("app-a", "S1"), "s3cret").status_code == 200
    assert InboundFax.objects.get().app_id == "app-a"
