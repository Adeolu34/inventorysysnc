"""
FastAPI application for Inventory Sync.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, init_db, close_db
from app.models import (
    Product,
    SyncJob,
    SyncLog,
    PriceAlert,
    MarketplaceEnum,
    SyncStatusEnum,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Inventory Sync API")
    await init_db()
    yield
    logger.info("Shutting down Inventory Sync API")
    await close_db()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Inventory Sync API",
    description="Automated marketplace inventory synchronisation",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SyncTriggerRequest(BaseModel):
    marketplace: Optional[str] = None  # "marketplace_1" | "marketplace_2" | None (= all)


class AlertResolveResponse(BaseModel):
    id: int
    resolved: bool


class ProductResponse(BaseModel):
    id: int
    sku: str
    name: str
    supplier_stock: Optional[int]
    marketplace_1_stock: Optional[int]
    marketplace_2_stock: Optional[int]
    supplier_price: Optional[float]
    marketplace_1_price: Optional[float]
    marketplace_2_price: Optional[float]
    last_synced_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class SyncJobResponse(BaseModel):
    id: int
    marketplace: str
    status: str
    products_processed: int
    products_updated: int
    products_failed: int
    started_at: datetime
    completed_at: Optional[datetime]
    error_summary: Optional[dict]

    class Config:
        from_attributes = True


class SyncLogResponse(BaseModel):
    id: int
    sync_job_id: int
    sku: str
    marketplace: str
    action: str
    old_value: Optional[str]
    new_value: Optional[str]
    message: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class AlertResponse(BaseModel):
    id: int
    sku: str
    alert_type: str
    message: str
    resolved: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Sync endpoints
# ---------------------------------------------------------------------------

@app.post("/sync/trigger", summary="Trigger manual sync")
async def trigger_sync(
    request: SyncTriggerRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.tasks.sync_tasks import sync_marketplace_task, run_full_sync_task
    from app.models import SyncJob

    marketplace_param = request.marketplace

    if marketplace_param and marketplace_param not in ("marketplace_1", "marketplace_2"):
        raise HTTPException(status_code=400, detail="Invalid marketplace value")

    if marketplace_param:
        # Create a SyncJob for single marketplace
        job = SyncJob(
            marketplace=MarketplaceEnum(marketplace_param),
            status=SyncStatusEnum.running,
        )
        db.add(job)
        await db.flush()
        await db.refresh(job)
        sync_marketplace_task.apply_async(
            args=[marketplace_param, job.id],
            queue="sync",
        )
        return {"message": f"Sync triggered for {marketplace_param}", "sync_job_id": job.id}
    else:
        task = run_full_sync_task.apply_async(queue="sync")
        return {"message": "Full sync triggered", "celery_task_id": task.id}


@app.get("/sync/jobs", response_model=list[SyncJobResponse], summary="List sync jobs")
async def list_sync_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(SyncJob).order_by(desc(SyncJob.started_at))
    if status:
        try:
            q = q.where(SyncJob.status == SyncStatusEnum(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    jobs = result.scalars().all()
    return [
        SyncJobResponse(
            id=j.id,
            marketplace=j.marketplace.value,
            status=j.status.value,
            products_processed=j.products_processed,
            products_updated=j.products_updated,
            products_failed=j.products_failed,
            started_at=j.started_at,
            completed_at=j.completed_at,
            error_summary=j.error_summary,
        )
        for j in jobs
    ]


@app.get("/sync/jobs/{job_id}", response_model=SyncJobResponse, summary="Get sync job by ID")
async def get_sync_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Sync job not found")
    return SyncJobResponse(
        id=job.id,
        marketplace=job.marketplace.value,
        status=job.status.value,
        products_processed=job.products_processed,
        products_updated=job.products_updated,
        products_failed=job.products_failed,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_summary=job.error_summary,
    )


@app.get("/sync/logs/{job_id}", response_model=list[SyncLogResponse], summary="Get logs for a sync job")
async def get_sync_logs(
    job_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(SyncLog)
        .where(SyncLog.sync_job_id == job_id)
        .order_by(SyncLog.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    logs = result.scalars().all()
    return [
        SyncLogResponse(
            id=log.id,
            sync_job_id=log.sync_job_id,
            sku=log.sku,
            marketplace=log.marketplace,
            action=log.action.value,
            old_value=log.old_value,
            new_value=log.new_value,
            message=log.message,
            created_at=log.created_at,
        )
        for log in logs
    ]


# ---------------------------------------------------------------------------
# Product endpoints
# ---------------------------------------------------------------------------

@app.get("/products", response_model=list[ProductResponse], summary="List products")
async def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    min_stock: Optional[int] = Query(None, description="Filter marketplace_1_stock >= value"),
    max_stock: Optional[int] = Query(None, description="Filter marketplace_1_stock <= value"),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    sku: Optional[str] = Query(None, description="Partial SKU match"),
    db: AsyncSession = Depends(get_db),
):
    q = select(Product).order_by(Product.sku)
    if min_stock is not None:
        q = q.where(
            (Product.marketplace_1_stock >= min_stock)
            | (Product.marketplace_2_stock >= min_stock)
        )
    if max_stock is not None:
        q = q.where(
            (Product.marketplace_1_stock <= max_stock)
            | (Product.marketplace_2_stock <= max_stock)
        )
    if min_price is not None:
        q = q.where(Product.supplier_price >= min_price)
    if max_price is not None:
        q = q.where(Product.supplier_price <= max_price)
    if sku:
        q = q.where(Product.sku.ilike(f"%{sku}%"))

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()


@app.get("/products/{sku}", response_model=ProductResponse, summary="Get product by SKU")
async def get_product(sku: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.sku == sku))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


# ---------------------------------------------------------------------------
# Alert endpoints
# ---------------------------------------------------------------------------

@app.get("/alerts", response_model=list[AlertResponse], summary="List unresolved alerts")
async def list_alerts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    alert_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = select(PriceAlert).where(PriceAlert.resolved == False).order_by(desc(PriceAlert.created_at))  # noqa: E712
    if alert_type:
        q = q.where(PriceAlert.alert_type == alert_type)
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    alerts = result.scalars().all()
    return [
        AlertResponse(
            id=a.id,
            sku=a.sku,
            alert_type=a.alert_type.value,
            message=a.message,
            resolved=a.resolved,
            created_at=a.created_at,
        )
        for a in alerts
    ]


@app.post("/alerts/{alert_id}/resolve", response_model=AlertResolveResponse, summary="Resolve an alert")
async def resolve_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    alert = await db.get(PriceAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.resolved = True
    await db.commit()
    return AlertResolveResponse(id=alert.id, resolved=alert.resolved)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard/stats", summary="Dashboard statistics")
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)

    total_products = (await db.execute(select(func.count(Product.id)))).scalar_one()
    out_of_stock_mp1 = (await db.execute(
        select(func.count(Product.id)).where(Product.marketplace_1_stock == 0)
    )).scalar_one()
    out_of_stock_mp2 = (await db.execute(
        select(func.count(Product.id)).where(Product.marketplace_2_stock == 0)
    )).scalar_one()
    active_alerts = (await db.execute(
        select(func.count(PriceAlert.id)).where(PriceAlert.resolved == False)  # noqa: E712
    )).scalar_one()
    jobs_24h = (await db.execute(
        select(func.count(SyncJob.id)).where(SyncJob.started_at >= last_24h)
    )).scalar_one()
    failed_jobs_24h = (await db.execute(
        select(func.count(SyncJob.id)).where(
            and_(SyncJob.started_at >= last_24h, SyncJob.status == SyncStatusEnum.failed)
        )
    )).scalar_one()

    last_sync = (await db.execute(
        select(func.max(SyncJob.completed_at)).where(SyncJob.status == SyncStatusEnum.completed)
    )).scalar_one()

    return {
        "total_products": total_products,
        "out_of_stock_marketplace_1": out_of_stock_mp1,
        "out_of_stock_marketplace_2": out_of_stock_mp2,
        "active_alerts": active_alerts,
        "sync_jobs_last_24h": jobs_24h,
        "failed_jobs_last_24h": failed_jobs_24h,
        "last_successful_sync": last_sync.isoformat() if last_sync else None,
    }
