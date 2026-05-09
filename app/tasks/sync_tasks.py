"""
Celery tasks for inventory synchronisation.

All database access uses psycopg2 synchronously (Celery workers are sync).
We use SQLAlchemy in synchronous mode here via a separate sync engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from celery import shared_task
from sqlalchemy import create_engine, select, update, and_, func
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.tasks.celery_app import celery_app
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Synchronous SQLAlchemy engine for Celery tasks
# ---------------------------------------------------------------------------

def _get_sync_db_url() -> str:
    """Convert asyncpg URL to psycopg2 URL for sync workers."""
    url = settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


_sync_engine = None
_SyncSession: Optional[sessionmaker] = None


def _get_session() -> Session:
    global _sync_engine, _SyncSession
    if _sync_engine is None:
        _sync_engine = create_engine(
            _get_sync_db_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        _SyncSession = sessionmaker(bind=_sync_engine, autoflush=False, autocommit=False)
    return _SyncSession()


# ---------------------------------------------------------------------------
# Task: sync_marketplace_task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.sync_tasks.sync_marketplace_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="sync",
)
def sync_marketplace_task(self, marketplace: str, sync_job_id: int) -> dict:
    """Read supplier data, sync one marketplace, update SyncJob and SyncLog."""
    from app.models import SyncJob, SyncLog, Product, SyncStatusEnum, SyncActionEnum
    from app.services.supplier_service import SupplierService
    from app.automation.marketplace_bot import MarketplaceBot, SyncResult

    logger.info(f"Starting sync for marketplace={marketplace}, job_id={sync_job_id}")

    session = _get_session()
    products_processed = 0
    products_updated = 0
    products_failed = 0
    errors: list[dict] = []

    try:
        # Load supplier inventory
        supplier_svc = SupplierService()
        inventory_df = supplier_svc.get_inventory()
        logger.info(f"Loaded {len(inventory_df)} products from supplier")

        # Build marketplace config
        if marketplace == "marketplace_1":
            mp_config = {
                "name": "marketplace_1",
                "url": settings.marketplace_1_url,
                "user": settings.marketplace_1_user,
                "password": settings.marketplace_1_pass,
            }
        else:
            mp_config = {
                "name": "marketplace_2",
                "url": settings.marketplace_2_url,
                "user": settings.marketplace_2_user,
                "password": settings.marketplace_2_pass,
            }

        # Run Playwright bot synchronously via asyncio.run
        result: SyncResult = asyncio.run(
            _run_bot_async(mp_config, inventory_df)
        )

        products_processed = result.products_processed
        products_updated = result.products_updated
        products_failed = result.products_failed

        # Persist SyncLog entries and update Product rows
        now = datetime.now(timezone.utc)
        for entry in result.log_entries:
            sku = entry["sku"]
            action_str = entry.get("action", "skip")
            try:
                action = SyncActionEnum(action_str)
            except ValueError:
                action = SyncActionEnum.skip

            log = SyncLog(
                sync_job_id=sync_job_id,
                sku=sku,
                marketplace=marketplace,
                action=action,
                old_value=str(entry.get("old_value", "")),
                new_value=str(entry.get("new_value", "")),
                message=entry.get("message", ""),
            )
            session.add(log)

            # Update Product record
            product = session.execute(
                select(Product).where(Product.sku == sku)
            ).scalar_one_or_none()

            if product is None:
                # Upsert from supplier data row
                row = inventory_df[inventory_df["sku"] == sku]
                if not row.empty:
                    row = row.iloc[0]
                    product = Product(
                        sku=sku,
                        name=str(row.get("name", sku)),
                        supplier_price=float(row.get("price", 0)),
                        supplier_stock=int(row.get("stock", 0)),
                    )
                    session.add(product)

            if product and action == SyncActionEnum.stock_update:
                if marketplace == "marketplace_1":
                    product.marketplace_1_stock = int(entry.get("new_value", 0))
                else:
                    product.marketplace_2_stock = int(entry.get("new_value", 0))
                product.last_synced_at = now

            elif product and action == SyncActionEnum.price_update:
                if marketplace == "marketplace_1":
                    product.marketplace_1_price = float(entry.get("new_value", 0))
                else:
                    product.marketplace_2_price = float(entry.get("new_value", 0))
                product.last_synced_at = now

            if action == SyncActionEnum.failed:
                errors.append({"sku": sku, "error": entry.get("message", "")})

        # Update SyncJob
        job = session.get(SyncJob, sync_job_id)
        if job:
            job.products_processed = products_processed
            job.products_updated = products_updated
            job.products_failed = products_failed
            job.completed_at = now
            job.status = (
                SyncStatusEnum.completed
                if products_failed == 0
                else SyncStatusEnum.partial
            )
            if errors:
                job.error_summary = {"errors": errors[:50]}  # cap stored errors

        session.commit()
        logger.info(
            f"Sync completed: marketplace={marketplace}, processed={products_processed}, "
            f"updated={products_updated}, failed={products_failed}"
        )
        return {
            "marketplace": marketplace,
            "sync_job_id": sync_job_id,
            "products_processed": products_processed,
            "products_updated": products_updated,
            "products_failed": products_failed,
        }

    except Exception as exc:
        session.rollback()
        logger.exception(f"sync_marketplace_task failed: {exc}")

        # Mark SyncJob as failed
        try:
            job = session.get(SyncJob, sync_job_id)  # type: ignore[arg-type]
            if job:
                from app.models import SyncStatusEnum
                job.status = SyncStatusEnum.failed
                job.completed_at = datetime.now(timezone.utc)
                job.error_summary = {"error": str(exc)}
                session.commit()
        except Exception:
            pass

        raise self.retry(exc=exc)

    finally:
        session.close()


async def _run_bot_async(mp_config: dict, inventory_df) -> "SyncResult":
    """Bridge: run the async MarketplaceBot from sync Celery context."""
    from app.automation.marketplace_bot import MarketplaceBot

    async with MarketplaceBot() as bot:
        return await bot.run_sync(mp_config, inventory_df)


# ---------------------------------------------------------------------------
# Task: run_full_sync_task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.sync_tasks.run_full_sync_task",
    queue="sync",
)
def run_full_sync_task() -> dict:
    """Create a SyncJob and dispatch sync for each marketplace."""
    from app.models import SyncJob, MarketplaceEnum, SyncStatusEnum

    logger.info("run_full_sync_task: creating umbrella sync job")
    session = _get_session()
    try:
        job = SyncJob(
            marketplace=MarketplaceEnum.all,
            status=SyncStatusEnum.running,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    finally:
        session.close()

    # Dispatch individual marketplace tasks
    for mp in ["marketplace_1", "marketplace_2"]:
        sync_marketplace_task.apply_async(
            args=[mp, job_id],
            queue="sync",
        )

    logger.info(f"run_full_sync_task: dispatched sync for both marketplaces, job_id={job_id}")
    return {"sync_job_id": job_id, "marketplaces": ["marketplace_1", "marketplace_2"]}


# ---------------------------------------------------------------------------
# Task: generate_daily_report_task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.sync_tasks.generate_daily_report_task",
    queue="reports",
)
def generate_daily_report_task() -> dict:
    """Build a pandas Excel report of the last 24h and email it."""
    import tempfile
    import os
    from app.services.report_service import ReportService

    logger.info("generate_daily_report_task: building daily report")
    session = _get_session()
    report_svc = ReportService()

    try:
        summary = report_svc.generate_daily_summary(session)
        with tempfile.NamedTemporaryFile(
            suffix=".xlsx", prefix="daily_report_", delete=False
        ) as tmp:
            report_path = tmp.name

        report_svc.build_excel_report(summary, report_path)
        report_svc.send_report_email(
            to_email=settings.alert_email,
            report_path=report_path,
            summary=summary,
        )
        logger.info(f"Daily report sent to {settings.alert_email}")
        return {"status": "sent", "report_path": report_path}

    except Exception as exc:
        logger.exception(f"generate_daily_report_task failed: {exc}")
        raise

    finally:
        session.close()
        try:
            os.unlink(report_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Task: check_low_stock_alerts_task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.sync_tasks.check_low_stock_alerts_task",
    queue="alerts",
)
def check_low_stock_alerts_task() -> dict:
    """Query products below threshold, create PriceAlert records, send email."""
    from app.models import Product, PriceAlert, AlertTypeEnum
    from app.services.report_service import ReportService

    logger.info("check_low_stock_alerts_task: checking low stock")
    threshold = settings.low_stock_threshold
    session = _get_session()
    report_svc = ReportService()
    new_alerts_count = 0

    try:
        # Find products with stock below threshold on either marketplace
        products = session.execute(
            select(Product).where(
                (Product.marketplace_1_stock <= threshold)
                | (Product.marketplace_2_stock <= threshold)
                | (Product.marketplace_1_stock == 0)
                | (Product.marketplace_2_stock == 0)
            )
        ).scalars().all()

        alert_data: list[dict] = []

        for product in products:
            for mp, stock in [
                ("marketplace_1", product.marketplace_1_stock),
                ("marketplace_2", product.marketplace_2_stock),
            ]:
                if stock is None:
                    continue

                if stock == 0:
                    alert_type = AlertTypeEnum.out_of_stock
                    msg = f"SKU {product.sku} is OUT OF STOCK on {mp}"
                elif stock <= threshold:
                    alert_type = AlertTypeEnum.low_stock
                    msg = f"SKU {product.sku} has only {stock} units on {mp} (threshold={threshold})"
                else:
                    continue

                # Avoid duplicate unresolved alerts
                existing = session.execute(
                    select(PriceAlert).where(
                        and_(
                            PriceAlert.sku == product.sku,
                            PriceAlert.alert_type == alert_type,
                            PriceAlert.resolved == False,  # noqa: E712
                        )
                    )
                ).scalar_one_or_none()

                if existing is None:
                    alert = PriceAlert(
                        sku=product.sku,
                        alert_type=alert_type,
                        message=msg,
                        resolved=False,
                    )
                    session.add(alert)
                    new_alerts_count += 1
                    alert_data.append({
                        "sku": product.sku,
                        "name": product.name,
                        "marketplace": mp,
                        "stock": stock,
                        "alert_type": alert_type.value,
                        "message": msg,
                    })

        session.commit()

        if alert_data and settings.alert_email:
            report_svc.send_low_stock_email(settings.alert_email, alert_data)
            logger.info(f"Sent low-stock alert email: {len(alert_data)} items")

        logger.info(f"check_low_stock_alerts_task: created {new_alerts_count} new alerts")
        return {"new_alerts": new_alerts_count}

    except Exception as exc:
        session.rollback()
        logger.exception(f"check_low_stock_alerts_task failed: {exc}")
        raise

    finally:
        session.close()
