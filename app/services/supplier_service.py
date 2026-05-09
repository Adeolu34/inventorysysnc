"""
SupplierService — reads inventory from Excel/CSV or HTTP API.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

REQUIRED_COLUMNS = {"sku", "name", "stock", "price"}


class SupplierService:
    """Provides supplier inventory as a normalised pandas DataFrame."""

    def read_spreadsheet(self, path: Optional[str] = None) -> pd.DataFrame:
        """Read Excel or CSV file, validate required columns, return DataFrame."""
        file_path = Path(path or settings.supplier_spreadsheet_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Supplier spreadsheet not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(file_path, dtype=str, engine="openpyxl")
        elif suffix == ".csv":
            df = pd.read_csv(file_path, dtype=str)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        # Normalise column names
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"Spreadsheet missing required columns: {missing}")

        df = self._coerce_types(df)
        logger.info(f"read_spreadsheet: loaded {len(df)} rows from {file_path}")
        return df

    def fetch_from_api(self) -> pd.DataFrame:
        """HTTP GET to supplier API, normalise response to DataFrame."""
        if not settings.supplier_api_url:
            raise ValueError("SUPPLIER_API_URL is not configured")

        headers: dict[str, str] = {}
        if settings.supplier_api_key:
            headers["Authorization"] = f"Bearer {settings.supplier_api_key}"
            headers["X-API-Key"] = settings.supplier_api_key

        logger.info(f"fetch_from_api: GET {settings.supplier_api_url}")

        with httpx.Client(timeout=30) as client:
            resp = client.get(settings.supplier_api_url, headers=headers)
            resp.raise_for_status()

        data = resp.json()

        # Accept both list-of-records and {items: [...]} shapes
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = data.get("items") or data.get("products") or data.get("data") or []
        else:
            raise ValueError(f"Unexpected API response type: {type(data)}")

        if not records:
            logger.warning("fetch_from_api: API returned 0 products")
            return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

        df = pd.DataFrame(records)
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        # Map common API field aliases
        alias_map = {
            "product_id": "sku",
            "item_id": "sku",
            "title": "name",
            "product_name": "name",
            "qty": "stock",
            "quantity": "stock",
            "unit_price": "price",
            "cost": "price",
        }
        df.rename(columns={k: v for k, v in alias_map.items() if k in df.columns}, inplace=True)

        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"API response missing required fields: {missing}")

        df = self._coerce_types(df)
        logger.info(f"fetch_from_api: received {len(df)} products")
        return df

    def get_inventory(self) -> pd.DataFrame:
        """Try API first; fall back to spreadsheet on any error."""
        if settings.supplier_api_url:
            try:
                return self.fetch_from_api()
            except Exception as exc:
                logger.warning(
                    f"API fetch failed ({exc}), falling back to spreadsheet"
                )

        return self.read_spreadsheet()

    @staticmethod
    def calculate_marketplace_price(
        supplier_price: float,
        markup_pct: Optional[float] = None,
    ) -> float:
        """Apply percentage markup to supplier price, round to 2 dp."""
        if markup_pct is None:
            markup_pct = settings.price_markup_percent
        marked_up = supplier_price * (1 + markup_pct / 100)
        # Round half-up to 2 decimal places
        return math.floor(marked_up * 100 + 0.5) / 100

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
        """Cast stock to int and price to float, drop rows with bad data."""
        df = df.copy()
        df["stock"] = pd.to_numeric(df["stock"], errors="coerce").fillna(0).astype(int)
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        before = len(df)
        df.dropna(subset=["sku", "price"], inplace=True)
        df["sku"] = df["sku"].astype(str).str.strip()
        df.drop_duplicates(subset=["sku"], keep="last", inplace=True)
        dropped = before - len(df)
        if dropped:
            logger.warning(f"_coerce_types: dropped {dropped} invalid rows")
        return df.reset_index(drop=True)
