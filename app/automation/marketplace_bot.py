"""
MarketplaceBot — owns Playwright browser lifecycle.

Usage (async context manager):
    async with MarketplaceBot() as bot:
        result = await bot.run_sync(mp_config, products_df)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.config import get_settings
from app.services.marketplace_service import MarketplaceService
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


@dataclass
class SyncResult:
    marketplace: str = ""
    products_processed: int = 0
    products_updated: int = 0
    products_failed: int = 0
    log_entries: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class MarketplaceBot:
    """Async context manager — manages Playwright browser lifecycle."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "MarketplaceBot":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        # Mask automation signals
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info("MarketplaceBot: browser started")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("MarketplaceBot: browser closed")

    # ------------------------------------------------------------------
    # Main sync entry point
    # ------------------------------------------------------------------

    async def run_sync(
        self,
        marketplace_config: dict,
        products_df: pd.DataFrame,
    ) -> SyncResult:
        """
        Run a complete sync session for one marketplace.

        marketplace_config: {name, url, user, password}
        products_df: DataFrame with columns [sku, name, stock, price]
        """
        result = SyncResult(
            marketplace=marketplace_config["name"],
            started_at=datetime.now(timezone.utc),
        )

        service = MarketplaceService(
            marketplace_name=marketplace_config["name"],
            url=marketplace_config["url"],
            credentials={
                "user": marketplace_config["user"],
                "password": marketplace_config["password"],
            },
        )

        page: Optional[Page] = None
        is_logged_in = False

        try:
            page = await self._context.new_page()
            page.set_default_timeout(30_000)

            # Login
            await service.login(page=page)
            is_logged_in = True

            # Process products row by row
            for _, row in products_df.iterrows():
                sku = str(row.get("sku", "")).strip()
                if not sku:
                    continue

                result.products_processed += 1

                # CAPTCHA detection
                if await self._detect_captcha(page):
                    logger.warning(
                        f"[{marketplace_config['name']}] CAPTCHA detected for SKU={sku}. "
                        "Skipping product — manual intervention required."
                    )
                    result.log_entries.append({
                        "sku": sku,
                        "action": "skip",
                        "message": "CAPTCHA detected — skipped",
                    })
                    await self._take_screenshot(page, f"captcha_{sku}")
                    result.products_failed += 1
                    continue

                # Session expiry detection + re-login
                if await self._detect_session_expiry(page):
                    logger.warning(
                        f"[{marketplace_config['name']}] Session expired. Re-logging in."
                    )
                    try:
                        await service.login(page=page)
                    except Exception as login_exc:
                        logger.error(f"Re-login failed: {login_exc}")
                        result.errors.append(f"Re-login failed: {login_exc}")
                        break

                try:
                    sync_result = await service.sync_product(page=page, product_row=row.to_dict())
                    self._process_sync_result(sync_result, result)
                except Exception as exc:
                    logger.error(f"[{marketplace_config['name']}] SKU={sku} exception: {exc}")
                    await self._take_screenshot(page, f"error_{sku}")
                    result.products_failed += 1
                    result.log_entries.append({
                        "sku": sku,
                        "action": "failed",
                        "message": str(exc),
                    })

                # Brief polite delay between products
                await asyncio.sleep(0.5)

        except Exception as exc:
            logger.exception(f"[{marketplace_config['name']}] run_sync fatal error: {exc}")
            result.errors.append(str(exc))
            if page:
                await self._take_screenshot(page, "run_sync_fatal")

        finally:
            if page and is_logged_in:
                await service.logout(page=page)
            if page and not page.is_closed():
                await page.close()
            result.completed_at = datetime.now(timezone.utc)

        logger.info(
            f"[{marketplace_config['name']}] run_sync complete: "
            f"processed={result.products_processed}, updated={result.products_updated}, "
            f"failed={result.products_failed}"
        )
        return result

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    async def handle_pagination(self, page: Page) -> bool:
        """
        Click "Next" page button if present.

        Returns True if a next page was navigated to, False if last page.
        """
        next_selectors = [
            'a[rel="next"]',
            'a.next',
            'button.next',
            '[aria-label="Next page"]',
            '.pagination .next:not(.disabled)',
            'a:has-text("Next")',
            'button:has-text("Next")',
        ]
        for selector in next_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    logger.debug(f"Paginated to next page via selector: {selector}")
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _detect_captcha(self, page: Page) -> bool:
        """Heuristic CAPTCHA detection — looks for common indicators."""
        captcha_indicators = [
            'iframe[src*="recaptcha"]',
            'iframe[src*="hcaptcha"]',
            '.g-recaptcha',
            '#captcha',
            '[data-sitekey]',
            'input[name="captcha"]',
        ]
        for selector in captcha_indicators:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:
                pass

        # Text-based detection
        try:
            body_text = (await page.inner_text("body")).lower()
            if any(k in body_text for k in ["captcha", "are you human", "verify you are"]):
                return True
        except Exception:
            pass

        return False

    async def _detect_session_expiry(self, page: Page) -> bool:
        """Detect if current page is a login/session-expired page."""
        try:
            current_url = page.url
            if "/login" in current_url or "/signin" in current_url:
                return True
            body_text = (await page.inner_text("body")).lower()
            if any(k in body_text for k in ["session expired", "please log in", "you have been logged out"]):
                return True
        except Exception:
            pass
        return False

    async def _take_screenshot(self, page: Page, label: str) -> None:
        """Save a timestamped screenshot for debugging."""
        try:
            screenshots_dir = Path(settings.screenshots_dir)
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            path = str(screenshots_dir / f"{label}_{ts}.png")
            await page.screenshot(path=path, full_page=True)
            logger.info(f"Screenshot saved: {path}")
        except Exception as exc:
            logger.warning(f"Screenshot failed: {exc}")

    @staticmethod
    def _process_sync_result(sync_result: dict, result: SyncResult) -> None:
        """Map MarketplaceService.sync_product result into SyncResult."""
        sku = sync_result.get("sku", "")
        error = sync_result.get("error")

        if error:
            result.products_failed += 1
            result.log_entries.append({
                "sku": sku,
                "action": "failed",
                "message": error,
            })
            return

        updated = False

        if sync_result.get("stock_updated"):
            updated = True
            result.log_entries.append({
                "sku": sku,
                "action": "stock_update",
                "old_value": sync_result.get("old_stock"),
                "new_value": sync_result.get("new_stock"),
                "message": "Stock updated",
            })

        if sync_result.get("price_updated"):
            updated = True
            result.log_entries.append({
                "sku": sku,
                "action": "price_update",
                "old_value": sync_result.get("old_price"),
                "new_value": sync_result.get("new_price"),
                "message": "Price updated",
            })

        if updated:
            result.products_updated += 1
        else:
            result.log_entries.append({
                "sku": sku,
                "action": "skip",
                "message": "No changes needed",
            })
