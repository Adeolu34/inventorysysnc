"""
ReportService — generates Excel reports and sends email summaries.
"""

from __future__ import annotations

import io
import smtplib
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import (
    Alignment,
    Font,
    PatternFill,
    Side,
    Border,
)
from openpyxl.utils import get_column_letter
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class ReportService:

    # ------------------------------------------------------------------
    # Data aggregation
    # ------------------------------------------------------------------

    def generate_daily_summary(self, db_session: Session) -> dict:
        """Aggregate sync stats from the last 24 hours."""
        from app.models import SyncJob, SyncLog, PriceAlert, SyncStatusEnum, SyncActionEnum

        since = datetime.now(timezone.utc) - timedelta(hours=24)

        # Jobs summary
        jobs_q = db_session.execute(
            select(
                SyncJob.status,
                func.count(SyncJob.id).label("count"),
                func.sum(SyncJob.products_processed).label("total_processed"),
                func.sum(SyncJob.products_updated).label("total_updated"),
                func.sum(SyncJob.products_failed).label("total_failed"),
            )
            .where(SyncJob.started_at >= since)
            .group_by(SyncJob.status)
        ).all()

        jobs_summary: list[dict] = [
            {
                "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                "count": row.count or 0,
                "total_processed": row.total_processed or 0,
                "total_updated": row.total_updated or 0,
                "total_failed": row.total_failed or 0,
            }
            for row in jobs_q
        ]

        # Logs by action
        logs_q = db_session.execute(
            select(
                SyncLog.action,
                SyncLog.marketplace,
                func.count(SyncLog.id).label("count"),
            )
            .where(SyncLog.created_at >= since)
            .group_by(SyncLog.action, SyncLog.marketplace)
        ).all()

        logs_summary: list[dict] = [
            {
                "action": row.action.value if hasattr(row.action, "value") else str(row.action),
                "marketplace": row.marketplace,
                "count": row.count or 0,
            }
            for row in logs_q
        ]

        # Active alerts
        alerts_q = db_session.execute(
            select(func.count(PriceAlert.id)).where(PriceAlert.resolved == False)  # noqa: E712
        ).scalar_one()

        # Raw log records for Excel detail sheet
        logs_detail = db_session.execute(
            select(SyncLog).where(SyncLog.created_at >= since).order_by(SyncLog.created_at.desc()).limit(5000)
        ).scalars().all()

        detail_records = [
            {
                "id": log.id,
                "sync_job_id": log.sync_job_id,
                "sku": log.sku,
                "marketplace": log.marketplace,
                "action": log.action.value if hasattr(log.action, "value") else str(log.action),
                "old_value": log.old_value,
                "new_value": log.new_value,
                "message": log.message,
                "created_at": log.created_at.isoformat() if log.created_at else "",
            }
            for log in logs_detail
        ]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period_hours": 24,
            "jobs_summary": jobs_summary,
            "logs_summary": logs_summary,
            "active_alerts": alerts_q,
            "detail_records": detail_records,
        }

    # ------------------------------------------------------------------
    # Excel report
    # ------------------------------------------------------------------

    def build_excel_report(self, data: dict, output_path: str) -> None:
        """Build a multi-sheet Excel workbook with conditional formatting."""
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            # Sheet 1: Summary
            summary_rows = []
            for item in data.get("jobs_summary", []):
                summary_rows.append({
                    "Status": item["status"],
                    "Jobs": item["count"],
                    "Products Processed": item["total_processed"],
                    "Products Updated": item["total_updated"],
                    "Products Failed": item["total_failed"],
                })
            df_summary = pd.DataFrame(summary_rows) if summary_rows else pd.DataFrame(
                columns=["Status", "Jobs", "Products Processed", "Products Updated", "Products Failed"]
            )
            df_summary.to_excel(writer, sheet_name="Summary", index=False)

            # Sheet 2: Actions by Marketplace
            logs_rows = []
            for item in data.get("logs_summary", []):
                logs_rows.append({
                    "Marketplace": item["marketplace"],
                    "Action": item["action"],
                    "Count": item["count"],
                })
            df_logs = pd.DataFrame(logs_rows) if logs_rows else pd.DataFrame(
                columns=["Marketplace", "Action", "Count"]
            )
            df_logs.to_excel(writer, sheet_name="Actions by Marketplace", index=False)

            # Sheet 3: Detail Log
            detail = data.get("detail_records", [])
            df_detail = pd.DataFrame(detail) if detail else pd.DataFrame(
                columns=["id", "sync_job_id", "sku", "marketplace", "action", "old_value", "new_value", "message", "created_at"]
            )
            df_detail.to_excel(writer, sheet_name="Detail Log", index=False)

            # Sheet 4: Metadata
            df_meta = pd.DataFrame([
                {"Key": "Report Generated At", "Value": data.get("generated_at", "")},
                {"Key": "Period (Hours)", "Value": data.get("period_hours", 24)},
                {"Key": "Active Unresolved Alerts", "Value": data.get("active_alerts", 0)},
            ])
            df_meta.to_excel(writer, sheet_name="Metadata", index=False)

        # Apply conditional formatting via openpyxl
        self._apply_excel_formatting(output_path)

    def _apply_excel_formatting(self, path: str) -> None:
        """Apply header styles, column widths, and conditional fills."""
        wb = load_workbook(path)

        header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        red_fill = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
        green_fill = PatternFill(start_color="CCFFCC", end_color="CCFFCC", fill_type="solid")
        yellow_fill = PatternFill(start_color="FFFACC", end_color="FFFACC", fill_type="solid")

        thin = Side(style="thin")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for ws in wb.worksheets:
            # Style headers
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = border

            # Auto-width columns
            for col_idx, col in enumerate(ws.columns, 1):
                max_len = max(
                    (len(str(cell.value or "")) for cell in col), default=10
                )
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, 60)

            # Conditional formatting on "Status" or "Action" columns
            header_row = [cell.value for cell in ws[1]]
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.border = border
                    if cell.column - 1 < len(header_row):
                        col_header = header_row[cell.column - 1]
                        cell_val = str(cell.value or "").lower()
                        if col_header in ("Status", "Action"):
                            if any(k in cell_val for k in ("fail", "error", "out_of_stock")):
                                cell.fill = red_fill
                            elif any(k in cell_val for k in ("complet", "updat", "success")):
                                cell.fill = green_fill
                            elif any(k in cell_val for k in ("partial", "skip", "low_stock")):
                                cell.fill = yellow_fill

            ws.freeze_panes = "A2"

        wb.save(path)

    # ------------------------------------------------------------------
    # Email delivery
    # ------------------------------------------------------------------

    def send_report_email(self, to_email: str, report_path: str, summary: dict) -> None:
        """Send daily Excel report via SMTP with attachment."""
        if not to_email:
            logger.warning("send_report_email: no recipient configured")
            return

        generated_at = summary.get("generated_at", "")
        active_alerts = summary.get("active_alerts", 0)

        total_processed = sum(r.get("total_processed", 0) for r in summary.get("jobs_summary", []))
        total_updated = sum(r.get("total_updated", 0) for r in summary.get("jobs_summary", []))
        total_failed = sum(r.get("total_failed", 0) for r in summary.get("jobs_summary", []))

        subject = f"[Inventory Sync] Daily Report — {generated_at[:10]}"
        html_body = f"""
        <html><body>
        <h2>Inventory Sync Daily Report</h2>
        <p><strong>Generated:</strong> {generated_at}</p>
        <table border="1" cellpadding="5" cellspacing="0">
          <tr><th>Metric</th><th>Value</th></tr>
          <tr><td>Products Processed (24h)</td><td>{total_processed}</td></tr>
          <tr><td>Products Updated (24h)</td><td>{total_updated}</td></tr>
          <tr><td>Products Failed (24h)</td><td>{total_failed}</td></tr>
          <tr><td>Active Unresolved Alerts</td><td>{active_alerts}</td></tr>
        </table>
        <p>See attached Excel report for full details.</p>
        </body></html>
        """
        self._send_email(to_email, subject, html_body, attachment_path=report_path)

    def send_low_stock_email(self, to_email: str, alert_data: list[dict]) -> None:
        """Send low-stock / out-of-stock alert email."""
        if not to_email:
            return

        rows_html = "".join(
            f"<tr><td>{a['sku']}</td><td>{a.get('name','')}</td><td>{a['marketplace']}</td>"
            f"<td>{a['stock']}</td><td>{a['alert_type']}</td></tr>"
            for a in alert_data
        )
        subject = f"[Inventory Sync] Low Stock Alert — {len(alert_data)} item(s)"
        html_body = f"""
        <html><body>
        <h2>Low Stock / Out of Stock Alert</h2>
        <table border="1" cellpadding="5" cellspacing="0">
          <tr><th>SKU</th><th>Name</th><th>Marketplace</th><th>Stock</th><th>Alert Type</th></tr>
          {rows_html}
        </table>
        </body></html>
        """
        self._send_email(to_email, subject, html_body)

    def _send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        attachment_path: str | None = None,
    ) -> None:
        msg = MIMEMultipart("mixed")
        msg["From"] = settings.smtp_user
        msg["To"] = to_email
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        if attachment_path and Path(attachment_path).exists():
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{Path(attachment_path).name}"',
            )
            msg.attach(part)

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.ehlo()
                server.starttls()
                if settings.smtp_user and settings.smtp_pass:
                    server.login(settings.smtp_user, settings.smtp_pass)
                server.sendmail(settings.smtp_user, to_email, msg.as_string())
            logger.info(f"Email sent to {to_email}: {subject}")
        except Exception as exc:
            logger.error(f"Failed to send email to {to_email}: {exc}")
            raise
