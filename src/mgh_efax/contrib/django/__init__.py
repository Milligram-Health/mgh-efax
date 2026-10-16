"""Django app that ships a standard fax outbox and inbox for the whole project.

Add "mgh_efax.contrib.django" to INSTALLED_APPS and run migrate. Every feature in the project
queues faxes through `mgh_efax.contrib.django.api.send_fax(...)` into the one OutboundFax table,
and every inbound fax lands in InboundFax. Apps react through signals rather than owning tables.

Settings (all read from the host app; the package supplies no credentials):

    EFAX = {
        "APP_ID": ..., "API_KEY": ..., "USER_ID": ...,   # USER_ID = default sending number
        "REGION": "us",                # data-sovereignty region: us (default), ca, global...
        "BASE_URL": None,              # full override of the API host, if ever needed
        "STORAGE": "default",          # STORAGES alias for fax documents (PHI: use encrypted)
        "AUTO_DISPATCH": True,         # queue a Celery send on commit; False = sweep only
        "SENDING_ENABLED": True,       # False: nothing is dialled; rows end "suppressed"
        "REDIRECT_TO": "",             # set: EVERY fax dials this number instead (test envs)
        "POLL_INBOUND": False,         # pull /faxes/received when no webhook is configured
        "WEBHOOK_SECRET": ...,         # only apps that receive webhooks
        # One eFax account per tenant (optional; see README "One account per tenant"):
        "ACCOUNT_RESOLVER": None,      # "pkg.mod.fn": fn(row, purpose) -> EFaxClient | None
        "WEBHOOK_SECRET_RESOLVER": None,  # "pkg.mod.fn": fn(payload: dict) -> str
        "WEBHOOK_SIGNATURE_HEADER": "X-HMAC-Signature",
        "WEBHOOK_ENCODING": "hex",
    }
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...client import DEFAULT_REGION, EFaxClient, EFaxConfig

PURPOSES = ("send", "track", "inbound")


def efax_settings() -> dict[str, Any]:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    conf = getattr(settings, "EFAX", None)
    if not isinstance(conf, dict):
        raise ImproperlyConfigured("settings.EFAX is not configured")
    return conf


def _hook(name: str) -> Callable[..., Any] | None:
    """A settings hook given as a dotted path (or, in tests, the callable itself)."""
    value = efax_settings().get(name)
    if not value:
        return None
    if callable(value):
        return value
    from django.utils.module_loading import import_string

    return import_string(value)


def account_resolver() -> Callable[..., EFaxClient | None] | None:
    return _hook("ACCOUNT_RESOLVER")


def webhook_secret_resolver() -> Callable[[dict[str, Any]], str] | None:
    return _hook("WEBHOOK_SECRET_RESOLVER")


def check_settings() -> None:
    from django.core.exceptions import ImproperlyConfigured

    conf = efax_settings()
    if conf.get("ACCOUNT_RESOLVER") and conf.get("POLL_INBOUND"):
        raise ImproperlyConfigured(
            "EFAX: POLL_INBOUND is not supported with ACCOUNT_RESOLVER (there is no list of "
            "accounts to poll); receive through the webhook instead"
        )


def get_client(
    *, user_id: str | None = None, row: Any = None, purpose: str | None = None
) -> EFaxClient | None:
    """The eFax client for a piece of work.

    Without ACCOUNT_RESOLVER: the project's one account from APP_ID/API_KEY/USER_ID; `user_id`
    picks a sending number other than the default. Never returns None.

    With ACCOUNT_RESOLVER: `resolver(row, purpose)` decides, per row, and may return None
    ("this account may not do that now"). `row` is required; purpose is "send", "track" or
    "inbound".
    """
    from django.core.exceptions import ImproperlyConfigured

    resolver = account_resolver()
    if resolver is not None:
        if row is None:
            raise ImproperlyConfigured(
                "EFAX: with ACCOUNT_RESOLVER set, pass the row being worked on"
            )
        if purpose not in PURPOSES:
            raise ValueError(f"purpose must be one of {PURPOSES}")
        return resolver(row, purpose)

    conf = efax_settings()
    missing = [k for k in ("APP_ID", "API_KEY", "USER_ID") if not conf.get(k)]
    if missing:
        raise ImproperlyConfigured(f"settings.EFAX is missing {', '.join(missing)}")
    return EFaxClient(
        EFaxConfig(
            app_id=conf["APP_ID"],
            api_key=conf["API_KEY"],
            user_id=user_id or conf["USER_ID"],
            region=conf.get("REGION") or DEFAULT_REGION,
            base_url=conf.get("BASE_URL") or None,
            timeout=float(conf.get("TIMEOUT", 30)),
        )
    )
