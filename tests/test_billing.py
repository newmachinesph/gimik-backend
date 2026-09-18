import copy
import datetime as dt
from unittest.mock import AsyncMock

import pytest

from app.routers import billing as router
from app.services import billing


def customer(*, sandbox=False, expires=None):
    return {"subscriber": {
        "entitlements": {"plus": {
            "product_identifier": "gimik_plus:monthly",
            "expires_date": expires or (dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
        }},
        "subscriptions": {"gimik_plus:monthly": {"store": "play_store", "is_sandbox": sandbox}},
    }}


def test_expiry_and_cancellation():
    data = customer()
    assert billing.verified_expiry(data) is not None
    data["subscriber"]["subscriptions"]["gimik_plus:monthly"]["unsubscribe_detected_at"] = "2026-01-01"
    assert billing.verified_expiry(data) is not None
    data["subscriber"]["subscriptions"]["gimik_plus:monthly"]["refunded_at"] = "2026-01-01"
    assert billing.verified_expiry(data) is None
    assert billing.verified_expiry(customer(expires="2020-01-01T00:00:00Z")) is None


def test_grace_period():
    data = customer(expires="2020-01-01T00:00:00Z")
    data["subscriber"]["entitlements"]["plus"]["grace_period_expires_date"] = "2099-01-01T00:00:00Z"
    assert billing.verified_expiry(data).year == 2099


def test_sandbox_is_explicit():
    assert billing.verified_expiry(customer(sandbox=True)) is None
    assert billing.verified_expiry(customer(sandbox=True), allow_sandbox=True) is not None
    missing = customer()
    del missing["subscriber"]["subscriptions"]["gimik_plus:monthly"]["is_sandbox"]
    assert billing.verified_expiry(missing) is None


@pytest.mark.parametrize("change", ["wrong_product", "test_store", "no_expiry", "no_entitlement"])
def test_unsupported_entitlements_fail_closed(change):
    data = customer()
    if change == "wrong_product":
        data["subscriber"]["entitlements"]["plus"]["product_identifier"] = "unrelated"
    elif change == "test_store":
        data["subscriber"]["subscriptions"]["gimik_plus:monthly"]["store"] = "test_store"
    elif change == "no_expiry":
        data["subscriber"]["entitlements"]["plus"]["expires_date"] = None
    else:
        data["subscriber"]["entitlements"] = {}
    assert billing.verified_expiry(data) is None


async def test_sync_requires_auth(client):
    assert (await client.post("/billing/sync")).status_code == 401


async def test_sync_uses_own_identity_and_never_client_claims(client, make_user, monkeypatch):
    headers = await make_user("Subscriber")
    other = await make_user("Other")
    mine = (await client.get("/auth/me", headers=headers)).json()["id"]
    fetch = AsyncMock(return_value=customer())
    monkeypatch.setattr(billing, "fetch_customer", fetch)
    response = await client.post("/billing/sync", headers=headers,
                                 json={"app_user_id": "another-user", "tier": "plus", "plus_until": "2999-01-01"})
    assert response.status_code == 200
    assert response.json()["tier"] == "plus"
    assert str(fetch.call_args.args[0]) == mine
    assert (await client.get("/auth/me", headers=other)).json()["tier"] == "free"
    fetch.return_value = {"subscriber": {"entitlements": {}, "subscriptions": {}}}
    assert (await client.post("/billing/sync", headers=headers)).json()["tier"] == "free"


async def test_malformed_provider_preserves_existing_access(client, make_plus_user, monkeypatch):
    headers = await make_plus_user("Subscriber")
    monkeypatch.setattr(billing, "fetch_customer", AsyncMock(return_value={"wrong": "schema"}))
    assert (await client.post("/billing/sync", headers=headers)).status_code == 503
    assert (await client.get("/auth/me", headers=headers)).json()["tier"] == "plus"


async def test_disabled_provider_does_not_make_network_calls(client, make_user):
    headers = await make_user("Subscriber")
    assert (await client.post("/billing/sync", headers=headers)).status_code == 503


async def test_webhook_authentication(client, monkeypatch):
    body = {"event": {"type": "TEST"}}
    assert (await client.post("/billing/webhook", json=body)).status_code == 401
    monkeypatch.setattr(router.settings, "revenuecat_webhook_authorization", "Bearer " + "a" * 40)
    assert (await client.post("/billing/webhook", json=body, headers={"Authorization": "bad"})).status_code == 401
    assert (await client.post("/billing/webhook", json=body, headers={"Authorization": "Bearer " + "a" * 40})).status_code == 200


async def test_duplicate_stale_and_transfer_events_requery_current_state(client, make_user, monkeypatch):
    first = await make_user("First")
    second = await make_user("Second")
    first_id = (await client.get("/auth/me", headers=first)).json()["id"]
    second_id = (await client.get("/auth/me", headers=second)).json()["id"]
    empty = {"subscriber": {"entitlements": {}, "subscriptions": {}}}
    payloads = {first_id: customer(), second_id: copy.deepcopy(empty)}

    async def fetch(user_id):
        return payloads[str(user_id)]

    monkeypatch.setattr(billing, "fetch_customer", fetch)
    monkeypatch.setattr(router.settings, "revenuecat_webhook_authorization", "Bearer " + "a" * 40)
    headers = {"Authorization": "Bearer " + "a" * 40}
    body = {"event": {"type": "INITIAL_PURCHASE", "app_user_id": first_id, "expiration_at_ms": 99999999999999}}
    for _ in range(2):
        assert (await client.post("/billing/webhook", headers=headers, json=body)).status_code == 200
    assert (await client.get("/auth/me", headers=first)).json()["tier"] == "plus"
    payloads[first_id], payloads[second_id] = empty, customer()
    transfer = {"event": {"type": "TRANSFER", "transferred_from": [first_id], "transferred_to": [second_id]}}
    assert (await client.post("/billing/webhook", headers=headers, json=transfer)).status_code == 200
    # An old purchase notification cannot restore a transferred/refunded entitlement.
    assert (await client.post("/billing/webhook", headers=headers, json=body)).status_code == 200
    assert (await client.get("/auth/me", headers=first)).json()["tier"] == "free"
    assert (await client.get("/auth/me", headers=second)).json()["tier"] == "plus"
