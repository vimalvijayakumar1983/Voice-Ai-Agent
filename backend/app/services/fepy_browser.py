# ruff: noqa: E501 -- embedded browser extraction scripts are kept readable as JavaScript.
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

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
        if parsed.scheme != "https" or parsed.hostname != allowed.hostname:
            raise FepyBrowserError("Only approved FEPY pages can be opened")
        return url

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
                context = await browser.new_context(
                    storage_state=storage_state,
                    locale="en-AE",
                    timezone_id="Asia/Dubai",
                )
                page = await context.new_page()
                page.set_default_timeout(settings.fepy_browser_timeout_seconds * 1000)
                try:
                    yield page, context
                finally:
                    await context.close()
                    await browser.close()

    async def search(self, query: str, limit: int) -> dict[str, Any]:
        async with self._page() as (page, _context):
            await page.goto(
                self._url(f"/shop/search?query={quote(query)}"),
                wait_until="domcontentloaded",
            )
            await page.locator("main h4").first.wait_for(state="visible")
            products = await page.evaluate(
                """(limit) => Array.from(document.querySelectorAll('main h4')).slice(0, limit)
                .map((heading) => {
                  const nameLink = heading.closest('a') || heading.parentElement?.closest('a');
                  const href = nameLink?.getAttribute('href') || '';
                  const root = heading.closest('li, article') || heading.parentElement?.parentElement;
                  const text = root?.innerText || '';
                  const price = text.match(/AED\\s?[\\d,]+(?:\\.\\d{2})?/)?.[0] || null;
                  const delivery = text.match(/(?:Get it by|Delivery by)\\s*([^\n]+)/i)?.[1] || null;
                  return { name: heading.textContent?.trim(), product_path: href, price, delivery };
                }).filter((item) => item.name && item.product_path)""",
                limit,
            )
            return {"query": query, "products": products, "source": "visible_fepy_page"}

    async def inspect_product(self, product_path: str) -> dict[str, Any]:
        async with self._page() as (page, _context):
            await page.goto(self._url(product_path), wait_until="domcontentloaded")
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

    async def add_to_cart(
        self, product_path: str, quantity: int, storage_state: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._page(storage_state) as (page, context):
            await page.goto(self._url(product_path), wait_until="domcontentloaded")
            quantity_input = page.get_by_role("spinbutton").last
            if await quantity_input.count():
                await quantity_input.fill(str(quantity))
            await page.get_by_role("button", name="Add to Cart", exact=True).click()
            await page.wait_for_timeout(900)
            state = await context.storage_state()
            return await self._cart_snapshot(page), state

    async def review_cart(
        self, storage_state: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._page(storage_state) as (page, context):
            await page.goto(self._url("/shop/cart"), wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            return await self._cart_snapshot(page), await context.storage_state()

    async def _cart_snapshot(self, page) -> dict[str, Any]:
        body = await page.locator("body").inner_text()
        total_matches = re.findall(r"Total[^\n]*\n(?:.*\n)?AED\s*([\d,]+(?:\.\d{2})?)", body, re.I)
        count_match = re.search(r"Cart\s*(\d+)", body)
        return {
            "item_count": int(count_match.group(1)) if count_match else None,
            "total_including_vat": total_matches[-1] if total_matches else None,
            "currency": "AED",
            "cart_url": self._url("/shop/cart"),
            "source": "visible_fepy_page",
        }


fepy_browser = FepyBrowser()
