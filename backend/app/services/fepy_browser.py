# ruff: noqa: E501 -- embedded browser extraction scripts are kept readable as JavaScript.
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import settings


class FepyBrowserError(RuntimeError):
    pass


class FepyBrowser:
    """Allowlisted FEPY browser adapter with restorable guest-cart state."""

    _semaphore = asyncio.Semaphore(2)

    def __init__(self) -> None:
        self.origin = settings.fepy_shop_origin.rstrip("/")

    def _url(self, path: str) -> str:
        url = urljoin(f"{self.origin}/", path.lstrip("/"))
        parsed = urlsplit(url)
        allowed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname != allowed.hostname
            or parsed.port != allowed.port
            or parsed.username
            or parsed.password
        ):
            raise FepyBrowserError("Only approved FEPY pages can be opened")
        return url

    def _assert_loaded_origin(self, page) -> None:
        self._url(page.url)

    async def _main_purchase_controls(self, page):
        """Resolve the quantity and button from one verified product buy box."""
        quantity_input = page.locator('main input.num_input[type="number"]:visible').first
        try:
            await quantity_input.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise FepyBrowserError("FEPY main purchase controls could not be verified") from exc

        purchase_panel = quantity_input.locator(
            "xpath=ancestor::*[.//button[contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart')]][1]"
        )
        add_button = purchase_panel.locator("button:visible").filter(
            has_text=re.compile(r"^\s*Add to Cart\s*$", re.I)
        )
        try:
            await add_button.first.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise FepyBrowserError("FEPY main purchase controls could not be verified") from exc
        if await add_button.count() != 1:
            raise FepyBrowserError("FEPY main purchase controls could not be verified")
        return quantity_input, add_button.first

    @asynccontextmanager
    async def _page(self, storage_state: dict[str, Any] | None = None):
        if not settings.fepy_commerce_enabled:
            raise FepyBrowserError("FEPY browser commerce is not enabled")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise FepyBrowserError("Browser runtime is unavailable") from exc

        async with self._semaphore:
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(
                        executable_path=settings.chromium_executable_path or None,
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-crash-reporter",
                            "--disable-breakpad",
                        ],
                    )
                except Exception as exc:
                    raise FepyBrowserError("Browser session could not start") from exc
                context = None
                try:
                    context = await browser.new_context(
                        storage_state=storage_state,
                        locale="en-AE",
                        timezone_id="Asia/Dubai",
                        viewport={"width": 1440, "height": 1000},
                    )
                    page = await context.new_page()
                    page.set_default_timeout(settings.fepy_browser_timeout_seconds * 1000)
                    yield page, context
                finally:
                    try:
                        if context is not None:
                            await context.close()
                    finally:
                        await browser.close()

    async def search(self, query: str, limit: int) -> dict[str, Any]:
        if not settings.fepy_commerce_enabled:
            raise FepyBrowserError("FEPY browser commerce is not enabled")
        endpoint = f"{settings.fepy_search_origin.rstrip('/')}/api/search"
        try:
            async with httpx.AsyncClient(
                timeout=settings.fepy_browser_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    endpoint,
                    params={
                        "q": query,
                        "page": 1,
                        "limit": limit,
                        "sortBy": "",
                        "sortOrder": "",
                    },
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FepyBrowserError("Live FEPY catalogue is temporarily unavailable") from exc

        products = []
        for hit in payload.get("hits", [])[:limit]:
            slug = str(hit.get("url") or "").strip("/")
            name = str(hit.get("name") or "").strip()
            if not slug or not name:
                continue
            product_path = f"/{slug}"
            self._url(product_path)
            price = None
            try:
                excluding_vat = Decimal(str(hit.get("price")))
                including_vat = (excluding_vat * Decimal("1.05")).quantize(Decimal("0.01"))
                price = f"AED {including_vat}"
            except (InvalidOperation, TypeError):
                pass
            products.append(
                {
                    "name": name,
                    "product_path": product_path,
                    "price": price,
                    "stock": "in_stock" if hit.get("inStock") == "yes" else "unknown",
                    "delivery": None,
                    "sku": hit.get("sku"),
                }
            )
        return {"query": query, "products": products, "source": "fepy_live_catalogue"}

    async def inspect_product(self, product_path: str) -> dict[str, Any]:
        try:
            async with self._page() as (page, _context):
                await page.goto(self._url(product_path), wait_until="domcontentloaded")
                self._assert_loaded_origin(page)
                await page.locator("h1").first.wait_for(state="visible")
                result = await page.evaluate(
                    """() => {
                      const body = document.body.innerText;
                      const name = document.querySelector('h1')?.textContent?.trim() || '';
                      const vatPrice = body.match(/Incl\\.\\s*5%\\s*VAT\\s*\n?AED\\s?([\\d,]+(?:\\.\\d{2})?)/i)?.[1];
                      const anyPrice = body.match(/AED\\s?([\\d,]+(?:\\.\\d{2})?)/)?.[1];
                      const delivery = body.match(/Delivery by\\s+([^\n]+)/i)?.[1]?.trim() || null;
                      const stock = /\bIn Stock\b/i.test(body) ? 'in_stock' : (/out of stock/i.test(body) ? 'out_of_stock' : 'unknown');
                      return { name, price_including_vat: vatPrice || anyPrice || null, currency: 'AED', stock, delivery };
                    }"""
                )
                result["product_path"] = urlsplit(page.url).path
                result["source"] = "visible_fepy_page"
                return result
        except FepyBrowserError:
            raise
        except Exception as exc:
            raise FepyBrowserError("Live FEPY product page is temporarily unavailable") from exc

    async def add_to_cart(
        self, product_path: str, quantity: int, storage_state: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            async with self._page(storage_state) as (page, context):
                await page.goto(self._url("/shop/cart"), wait_until="domcontentloaded")
                self._assert_loaded_origin(page)
                await page.locator("body").wait_for(state="visible")
                before_snapshot = await self._cart_snapshot(
                    page,
                    expected_product_path=product_path,
                )
                before_quantity = before_snapshot["verified_quantity"] or 0
                await page.goto(self._url(product_path), wait_until="domcontentloaded")
                self._assert_loaded_origin(page)
                await page.locator("h1").first.wait_for(state="visible")
                quantity_input, add_button = await self._main_purchase_controls(page)
                await quantity_input.fill(str(quantity))
                try:
                    selected_quantity = int(await quantity_input.input_value())
                except (TypeError, ValueError) as exc:
                    raise FepyBrowserError("FEPY product quantity could not be verified") from exc
                if selected_quantity != quantity:
                    raise FepyBrowserError("FEPY product quantity could not be verified")
                await add_button.scroll_into_view_if_needed()
                await add_button.click()
                await page.wait_for_timeout(1200)
                await page.goto(self._url("/shop/cart"), wait_until="domcontentloaded")
                self._assert_loaded_origin(page)
                await page.locator("body").wait_for(state="visible")
                await page.wait_for_timeout(500)
                snapshot = await self._cart_snapshot(
                    page,
                    expected_product_path=product_path,
                    expected_quantity=before_quantity + quantity,
                )
                if not snapshot["verified"]:
                    raise FepyBrowserError("FEPY cart contents could not be verified")
                state = await context.storage_state()
                return snapshot, state
        except FepyBrowserError:
            raise
        except Exception as exc:
            raise FepyBrowserError("FEPY cart controls are temporarily unavailable") from exc

    async def review_cart(
        self, storage_state: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            async with self._page(storage_state) as (page, context):
                await page.goto(self._url("/shop/cart"), wait_until="domcontentloaded")
                self._assert_loaded_origin(page)
                await page.wait_for_timeout(500)
                return await self._cart_snapshot(page), await context.storage_state()
        except FepyBrowserError:
            raise
        except Exception as exc:
            raise FepyBrowserError("FEPY cart page is temporarily unavailable") from exc

    async def _cart_snapshot(
        self,
        page,
        *,
        expected_product_path: str | None = None,
        expected_quantity: int | None = None,
    ) -> dict[str, Any]:
        body = await page.locator("body").inner_text()
        total_matches = re.findall(r"Total[^\n]*\n(?:.*\n)?AED\s*([\d,]+(?:\.\d{2})?)", body, re.I)
        count_match = re.search(r"Subtotal\s*\((\d+)\)", body, re.I) or re.search(
            r"Cart\s*(\d+)", body, re.I
        )
        item_count = int(count_match.group(1)) if count_match else None
        total = total_matches[-1] if total_matches else None
        product_verification = {"found": False, "quantity": None}
        if expected_product_path:
            product_verification = await page.evaluate(
                """expectedPath => {
                  const visible = element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const link = Array.from(document.querySelectorAll('main a[href]')).find(item => {
                    try { return new URL(item.href).pathname === expectedPath && visible(item); }
                    catch { return false; }
                  });
                  if (!link) return { found: false, quantity: null };
                  let node = link;
                  for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
                    const input = Array.from(node.querySelectorAll('input[type="number"]')).find(visible);
                    if (input) {
                      const quantity = Number.parseInt(input.value, 10);
                      return { found: true, quantity: Number.isInteger(quantity) ? quantity : null };
                    }
                  }
                  return { found: true, quantity: null };
                }""",
                expected_product_path,
            )
        positive_total = False
        if total is not None:
            try:
                positive_total = Decimal(total.replace(",", "")) > 0
            except InvalidOperation:
                pass
        verified = (
            item_count is not None
            and item_count > 0
            and positive_total
            and (
                expected_product_path is None
                or (
                    product_verification["found"]
                    and product_verification["quantity"] is not None
                    and expected_quantity is not None
                    and product_verification["quantity"] == expected_quantity
                )
            )
        )
        return {
            "item_count": item_count,
            "total_including_vat": total,
            "currency": "AED",
            "cart_url": self._url("/shop/cart"),
            "source": "visible_fepy_page",
            "verified": verified,
            "verified_product_path": expected_product_path
            if product_verification["found"]
            else None,
            "verified_quantity": product_verification["quantity"],
        }


fepy_browser = FepyBrowser()
