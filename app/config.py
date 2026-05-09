from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/inventory_sync"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Supplier
    supplier_spreadsheet_path: str = "/data/supplier_inventory.xlsx"
    supplier_api_url: Optional[str] = None
    supplier_api_key: Optional[str] = None

    # Marketplace 1
    marketplace_1_url: str = "https://marketplace1.example.com"
    marketplace_1_user: str = "user1"
    marketplace_1_pass: str = "pass1"

    # Marketplace 2
    marketplace_2_url: str = "https://marketplace2.example.com"
    marketplace_2_user: str = "user2"
    marketplace_2_pass: str = "pass2"

    # SMTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email: str = ""

    # Paths
    screenshots_dir: str = "/app/screenshots"

    # Logging
    log_level: str = "INFO"

    # Sync configuration
    sync_interval_minutes: int = 60
    low_stock_threshold: int = 10
    price_markup_percent: float = 15.0

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url


@lru_cache()
def get_settings() -> Settings:
    return Settings()
