import uuid

import httpx
import pytest

from app.api.v1.endpoints import commerce
from app.services import fepy_browser as fepy_browser_module


@pytest.mark.asyncio
async def test_live_catalogue_search_maps_vat_stock_and_safe_product_path(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, params):
            assert url == "https://search.fepy.com/api/search"
            assert params["q"] == "Bosch drill"
            request = httpx.Request("GET", url, params=params)
            return httpx.Response(
                200,
                request=request,
                json={
                    "hits": [
                        {
                            "name": "Bosch Drill",
                            "url": "bosch-drill",
                            "price": 100,
                            "inStock": "yes",
                            "sku": "BOSCH-1",
                        }
                    ]
                },
            )

    monkeypatch.setattr(fepy_browser_module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    result = await fepy_browser_module.FepyBrowser().search("Bosch drill", 5)

    assert result["source"] == "fepy_live_catalogue"
    assert result["products"] == [
        {
            "name": "Bosch Drill",
            "product_path": "/bosch-drill",
            "price": "AED 105.00",
            "stock": "in_stock",
            "delivery": None,
            "sku": "BOSCH-1",
        }
    ]


@pytest.mark.asyncio
async def test_commerce_session_search_cart_and_confirmation_gate(
    client, auth_headers, monkeypatch
):
    async def fake_search(query: str, limit: int):
        return {
            "query": query,
            "products": [
                {
                    "name": "Bosch Drill",
                    "product_path": "/bosch-drill",
                    "price": "AED 565.07",
                    "delivery": "Sunday",
                }
            ][:limit],
            "source": "visible_fepy_page",
        }

    async def fake_add(product_path: str, quantity: int, storage_state):
        assert product_path == "/bosch-drill"
        assert quantity == 2
        return (
            {
                "item_count": 2,
                "total_including_vat": "1130.14",
                "currency": "AED",
                "source": "visible_fepy_page",
            },
            {"cookies": [{"name": "cart", "value": "opaque"}], "origins": []},
        )

    monkeypatch.setattr(commerce.fepy_browser, "search", fake_search)
    monkeypatch.setattr(commerce.fepy_browser, "add_to_cart", fake_add)

    created = await client.post(
        "/api/v1/commerce/sessions",
        headers=auth_headers,
        json={"channel": "web_voice"},
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    searched = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/search",
        headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"query": "Bosch drill", "limit": 5},
    )
    assert searched.status_code == 200
    assert searched.json()["actions"][0]["result_summary"]["products"][0]["name"] == "Bosch Drill"

    cart = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/cart/items",
        headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"product_path": "/bosch-drill", "quantity": 2},
    )
    assert cart.status_code == 200
    assert cart.json()["cart_snapshot"]["total_including_vat"] == "1130.14"

    checkout = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/checkout",
        headers=auth_headers,
        json={
            "payment_method": "cod",
            "customer": {
                "first_name": "Test",
                "last_name": "Customer",
                "phone": "+971501234567",
                "email": "customer@example.com",
                "address_line_1": "Building 1, Street 2",
                "city": "Abu Dhabi",
                "emirate": "Abu Dhabi",
            },
        },
    )
    assert checkout.status_code == 200
    assert checkout.json()["status"] == "awaiting_confirmation"
    assert "customer" not in checkout.text
    assert "Building 1" not in checkout.text

    rejected = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/confirm",
        headers=auth_headers,
        json={"confirmation_text": "yes"},
    )
    assert rejected.status_code == 422

    confirmed = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/confirm",
        headers=auth_headers,
        json={"confirmation_text": "Confirm order"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_commerce_rejects_card_fields_and_cross_tenant_product_urls(client, auth_headers):
    created = await client.post(
        "/api/v1/commerce/sessions", headers=auth_headers, json={"channel": "operator"}
    )
    session_id = created.json()["id"]

    card = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/checkout",
        headers=auth_headers,
        json={
            "payment_method": "hosted_card",
            "customer": {
                "first_name": "Test",
                "last_name": "Customer",
                "phone": "+971501234567",
                "email": "customer@example.com",
                "address_line_1": "Building 1, Street 2",
                "city": "Dubai",
                "emirate": "Dubai",
                "card_number": "4111111111111111",
            },
        },
    )
    assert card.status_code == 422

    with pytest.raises(Exception, match="Only approved FEPY"):
        commerce.fepy_browser._url("https://example.com/product")
