import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from app.api.v1.endpoints import commerce
from app.services import fepy_browser as fepy_browser_module
from app.services.fepy_browser import FepyBrowserError


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
async def test_purchase_controls_are_scoped_to_quantity_panel():
    class FakeButton:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def count(self):
            return 1

    class FakeButtons(FakeButton):
        def filter(self, **_kwargs):
            return self

    class FakePanel:
        def __init__(self, main_button):
            self.main_button = main_button

        def locator(self, selector):
            assert selector == "button"
            return self.main_button

    class FakeQuantity:
        def __init__(self, panel):
            self.panel = panel

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def count(self):
            return 1

        def locator(self, selector):
            assert selector.startswith("xpath=ancestor::")
            return self.panel

    class FakePage:
        def __init__(self, quantity):
            self.quantity = quantity

        def locator(self, selector):
            assert selector == 'main input.num_input[type="number"]'
            return self.quantity

    main_button = FakeButtons()
    quantity = FakeQuantity(FakePanel(main_button))

    browser = fepy_browser_module.FepyBrowser()
    selected_quantity, selected_button = await browser._main_purchase_controls(FakePage(quantity))

    assert selected_quantity is quantity
    assert selected_button is main_button


@pytest.mark.asyncio
async def test_purchase_controls_fail_closed_without_main_quantity_input():
    class MissingQuantity:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            raise TimeoutError

    class FakePage:
        def __init__(self):
            self.selectors = []
            self.url = "https://www.fepy.com/example-product"

        def locator(self, selector):
            self.selectors.append(selector)
            return MissingQuantity()

    page = FakePage()

    with pytest.raises(FepyBrowserError, match="main purchase controls"):
        await fepy_browser_module.FepyBrowser()._main_purchase_controls(page)

    assert page.selectors == ['main input.num_input[type="number"]']


@pytest.mark.asyncio
async def test_cart_snapshot_requires_product_quantity_and_positive_total():
    class FakeBody:
        async def inner_text(self):
            return "Subtotal(2)\nTotal (incl. VAT)\nAED 1,130.14"

    class FakePage:
        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

        async def evaluate(self, _script, product_path):
            assert product_path == "/bosch-drill"
            return {"found": True, "quantity": 2}

    snapshot = await fepy_browser_module.FepyBrowser()._cart_snapshot(
        FakePage(), expected_product_path="/bosch-drill", expected_quantity=2
    )

    assert snapshot["verified"] is True
    assert snapshot["verified_product_path"] == "/bosch-drill"
    assert snapshot["verified_quantity"] == 2


@pytest.mark.asyncio
async def test_add_to_cart_verifies_main_control_mutation_and_cart_page():
    class FakeWaitable:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

    class FakeQuantity:
        def __init__(self):
            self.value = "1"

        async def fill(self, value):
            self.value = value

        async def input_value(self):
            return self.value

        async def is_visible(self):
            return True

    class FakeButton:
        def __init__(self):
            self.clicked = False

        async def scroll_into_view_if_needed(self):
            return None

        async def is_visible(self):
            return True

        async def click(self):
            self.clicked = True

    class FakePage:
        def __init__(self, origin):
            self.url = origin
            self.visited = []

        async def goto(self, url, **_kwargs):
            assert _kwargs == {"wait_until": "commit"}
            self.url = url
            self.visited.append(url)

        def locator(self, selector):
            assert selector == "body"
            return FakeWaitable()

        async def wait_for_timeout(self, _milliseconds):
            return None

    class FakeContext:
        async def storage_state(self):
            return {"cookies": [{"name": "cart", "value": "opaque"}], "origins": []}

    class FakeBrowser(fepy_browser_module.FepyBrowser):
        def __init__(self):
            super().__init__()
            self.page = FakePage(self.origin)
            self.quantity = FakeQuantity()
            self.button = FakeButton()
            self.snapshots = 0

        @asynccontextmanager
        async def _page(self, storage_state=None):
            assert storage_state == {"cookies": [], "origins": []}
            yield self.page, FakeContext()

        async def _main_purchase_controls(self, _page):
            return self.quantity, self.button

        async def _cart_snapshot(self, _page, **kwargs):
            self.snapshots += 1
            if self.snapshots == 1:
                assert kwargs == {"expected_product_path": "/bosch-drill"}
                return {
                    "item_count": 0,
                    "total_including_vat": "0.00",
                    "currency": "AED",
                    "verified": False,
                    "verified_product_path": None,
                    "verified_quantity": None,
                }
            assert kwargs == {"expected_product_path": "/bosch-drill", "expected_quantity": 2}
            return {
                "item_count": 2,
                "total_including_vat": "1130.14",
                "currency": "AED",
                "verified": True,
                "verified_product_path": "/bosch-drill",
                "verified_quantity": 2,
            }

    browser = FakeBrowser()
    snapshot, storage = await browser.add_to_cart("/bosch-drill", 2, {"cookies": [], "origins": []})

    assert browser.quantity.value == "2"
    assert browser.button.clicked is True
    assert browser.page.visited == [
        f"{browser.origin}/shop/cart",
        f"{browser.origin}/bosch-drill",
        f"{browser.origin}/shop/cart",
    ]
    assert snapshot["verified"] is True
    assert storage["cookies"][0]["name"] == "cart"


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
                "verified": True,
                "verified_product_path": "/bosch-drill",
                "verified_quantity": 2,
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
async def test_browser_failure_is_sanitized_and_persisted(client, auth_headers, monkeypatch):
    async def unavailable(_query: str, _limit: int):
        raise FepyBrowserError("Live FEPY catalogue is temporarily unavailable")

    monkeypatch.setattr(commerce.fepy_browser, "search", unavailable)
    created = await client.post(
        "/api/v1/commerce/sessions", headers=auth_headers, json={"channel": "operator"}
    )
    session_id = created.json()["id"]
    idempotency_key = str(uuid.uuid4())

    failed = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/search",
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
        json={"query": "Bosch drill", "limit": 5},
    )

    assert failed.status_code == 502
    assert failed.json() == {"detail": "Live FEPY catalogue is temporarily unavailable"}
    replayed = await client.post(
        f"/api/v1/commerce/sessions/{session_id}/search",
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
        json={"query": "Bosch drill", "limit": 5},
    )
    assert replayed.status_code == 502
    assert replayed.json() == failed.json()
    sessions = await client.get("/api/v1/commerce/sessions", headers=auth_headers)
    stored = next(item for item in sessions.json() if item["id"] == session_id)
    assert stored["last_error"] == "Live FEPY catalogue is temporarily unavailable"
    assert len(stored["actions"]) == 1
    assert stored["actions"][0]["status"] == "failed"
    assert stored["actions"][0]["error_message"] == (
        "Live FEPY catalogue is temporarily unavailable"
    )


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
