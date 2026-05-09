"""
MarketplaceService — Playwright-based interaction with a marketplace portal.

Each instance is bound to one marketplace configuration.  A Playwright
`page` object must be passed in by the caller (the MarketplaceBot owns
the browser lifecycle).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from app.config import get_settings
from app.utils.logger import get_logger
from app.utils.retry import async_retry

logger = get_logger(__name__)
settings = get_settings()


class MarketplaceService:
    def __init__(
        self,
        marketplace_name: str,
        url: str,
        credentials: dict,
    ) -> None:
        self.marketplace_name = marketplace_name
        self.base_url = url.rstrip("/")
        self.credentials = credentials  # {"user": ..., "password": ...}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @async_retry(max_attempts=3, base_delay=2.0)
    async def login(self, page: Page) -> None:
        """Navigate to login page and authenticate."""
        login_url = f"{self.base_url}/login"
        logger.info(f"[{self.marketplace_name}] Navigating to login: {login_url}")

        await page.goto(login_url, wait_until="networkidle", timeout=30_000)

        # Fill credentials — selectors are generic; override per-marketplace subclass
        await page.fill('input[name="username"], input[name="email"], #username', self.credentials["user"])
        await page.fill('input[name="password"], #password', self.credentials["password"])
        await page.click('button[type="submit"], input[type="submit"], .login-btn')

        # Wait for redirect away from login page
        await page.wait_for_url(
            lambda url: "/login" not in url,
            timeout=15_000,
        )
        logger.info(f"[{self.marketplace_name}] Login successful")

    async def logout(self, page: Page) -> None:
        """Attempt a safe logout; swallow errors."""
        try:
            logout_url = f"{self.base_url}/logout"
            await page.goto(logout_url, timeout=10_000)
            logger.info(f"[{self.marketplace_name}] Logged out")
        except Exception as exc:
            logger.warning(f"[{self.marketplace_name}] Logout warning: {exc}")

    # ------------------------------------------------------------------
    # Product navigation helpers
    # ------------------------------------------------------------------

    async def _navigate_to_product(self, page: Page, sku: str) -> bool:
        """Navigate to the product detail / edit page. Returns True on success."""
        # Try search-based navigation
        search_url = f"{self.base_url}/products/search?q={sku}&sku={sku}"
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
            # Click the first result that matches the SKU
            sku_link = page.locator(f'a[data-sku="{sku}"], a:has-text("{sku}")').first
            if await sku_link.count() > 0:
                await sku_link.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                return True
        except PlaywrightTimeoutError:
            pass

        # Fallback: direct URL pattern
        direct_url = f"{self.base_url}/products/{sku}"
        try:
            await page.goto(direct_url, wait_until="domcontentloaded", timeout=20_000)
            return True
        except Exception as exc:
            logger.warning(f"[{self.marketplace_name}] Could not navigate to product {sku}: {exc}")
            return False

    async def _navigate_to_product_edit(self, page: Page, sku: str) -> bool:
        """Navigate to the product edit form."""
        if not await self._navigate_to_product(page, sku):
            return False
        try:
            edit_btn = page.locator('a.edit-product, button.edit, [data-action="edit"]').first
            if await edit_btn.count() > 0:
                await edit_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
            return True
        except Exception as exc:
            logger.warning(f"[{self.marketplace_name}] Edit navigation issue for {sku}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Read / write operations
    # ------------------------------------------------------------------

    @async_retry(max_attempts=3, base_delay=1.5)
    async def get_current_stock(self, page: Page, sku: str) -> Optional[int]:
        """Navigate to product page and read the current stock value."""
        if not await self._navigate_to_product(page, sku):
            return None

        stock_selectors = [
            '[data-field="stock"]',
            '[data-field="quantity"]',
            '#stock_quantity',
            'input[name="stock"]',
            '.stock-value',
            'td.stock',
        ]
        for selector in stock_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    raw = await locator.input_value() or await locator.inner_text()
                    digits = re.sub(r"[^\d]", "", raw)
                    if digits:
                        stock = int(digits)
                        logger.debug(f"[{self.marketplace_name}] SKU={sku} current_stock={stock}")
                        return stock
            except Exception:
                continue

        logger.warning(f"[{self.marketplace_name}] Could not read stock for SKU={sku}")
        return None

    @async_retry(max_attempts=3, base_delay=1.5)
    async def update_stock(self, page: Page, sku: str, new_stock: int) -> bool:
        """Navigate to product edit and update the stock field."""
        if not await self._navigate_to_product_edit(page, sku):
            return False

        stock_selectors = [
            'input[name="stock"]',
            'input[name="quantity"]',
            '#stock_quantity',
            'input[data-field="stock"]',
        ]
        for selector in stock_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    await locator.triple_click()
                    await locator.fill(str(new_stock))
                    await self._save_form(page)
                    logger.info(
                        f"[{self.marketplace_name}] Updated stock for SKU={sku}: {new_stock}"
                    )
                    return True
            except Exception as exc:
                logger.debug(f"Selector {selector} failed: {exc}")
                continue

        logger.warning(f"[{self.marketplace_name}] Could not update stock for SKU={sku}")
        return False

    @async_retry(max_attempts=3, base_delay=1.5)
    async def update_price(self, page: Page, sku: str, new_price: float) -> bool:
        """Navigate to product edit and update the price field."""
        if not await self._navigate_to_product_edit(page, sku):
            return False

        price_selectors = [
            'input[name="price"]',
            'input[name="regular_price"]',
            '#price',
            'input[data-field="price"]',
        ]
        for selector in price_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    await locator.triple_click()
                    await locator.fill(f"{new_price:.2f}")
                    await self._save_form(page)
                    logger.info(
                        f"[{self.marketplace_name}] Updated price for SKU={sku}: {new_price}"
                    )
                    return True
            except Exception as exc:
                logger.debug(f"Selector {selector} failed: {exc}")
                continue

        logger.warning(f"[{self.marketplace_name}] Could not update price for SKU={sku}")
        return False

    async def _save_form(self, page: Page) -> None:
        """Click save/submit button and wait for confirmation."""
        save_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button.save',
            'button.btn-primary',
            '#save-product',
        ]
        for selector in save_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Full product sync
    # ------------------------------------------------------------------

    async def sync_product(self, page: Page, product_row: dict) -> dict:
        """
        Full sync for one product.

        Returns:
            {sku, stock_updated, price_updated, error}
        """
        sku = str(product_row.get("sku", ""))
        supplier_stock = int(product_row.get("stock", 0))
        supplier_price = float(product_row.get("price", 0.0))
        markup_pct = settings.price_markup_percent

        from app.services.supplier_service import SupplierService
        marketplace_price = SupplierService.calculate_marketplace_price(supplier_price, markup_pct)

        result = {
            "sku": sku,
            "stock_updated": False,
            "price_updated": False,
            "error": None,
            "old_stock": None,
            "new_stock": supplier_stock,
            "old_price": None,
            "new_price": marketplace_price,
        }

        try:
            # Read current stock
            current_stock = await self.get_current_stock(page, sku)
            result["old_stock"] = current_stock

            if current_stock != supplier_stock:
                result["stock_updated"] = await self.update_stock(page, sku, supplier_stock)

            # Read current price (generic — re-navigate to product)
            if await self._navigate_to_product(page, sku):
                price_selectors = ['[data-field="price"]', '#price', '.price-value']
                for sel in price_selectors:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            raw = await loc.input_value() or await loc.inner_text()
                            digits = re.sub(r"[^\d.]", "", raw)
                            result["old_price"] = float(digits) if digits else None
                            break
                    except Exception:
                        continue

            if result["old_price"] != marketplace_price:
                result["price_updated"] = await self.update_price(page, sku, marketplace_price)

        except Exception as exc:
            result["error"] = str(exc)
            logger.error(f"[{self.marketplace_name}] sync_product failed for SKU={sku}: {exc}")

        return result
