import json
import secrets
import csv
import io
from collections import defaultdict
from datetime import date
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from payos import APIError, PayOS, WebhookError
from payos.types import CreatePaymentLinkRequest

from auth_routes import get_current_user
from config import settings
from database import CreditLedger, Payment, User, get_db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _now_vn() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))


def _new_order_code() -> int:
    # Keep orderCode numeric and unique enough for payment provider reconciliation.
    ts = int(datetime.utcnow().timestamp())
    suffix = secrets.randbelow(9000) + 1000
    return int(f"{ts}{suffix}")


def _to_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "model_dump_camel_case"):
        return value.model_dump_camel_case()
    return {}


def _is_payos_configured() -> bool:
    return bool(settings.PAYOS_CLIENT_ID and settings.PAYOS_API_KEY and settings.PAYOS_CHECKSUM_KEY)


def _payos_client() -> PayOS:
    return PayOS(
        client_id=settings.PAYOS_CLIENT_ID,
        api_key=settings.PAYOS_API_KEY,
        checksum_key=settings.PAYOS_CHECKSUM_KEY,
    )


def _admin_email_set() -> set[str]:
    return {
        email.strip().lower()
        for email in (settings.ADMIN_EMAILS or "").split(",")
        if email.strip()
    }


def _require_admin(user: User):
    allowed = _admin_email_set()
    if not allowed or user.email.lower() not in allowed:
        raise HTTPException(status_code=403, detail="Admin access required")


def _parse_yyyy_mm_dd(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}. Expected YYYY-MM-DD")


def _topup_packages() -> list[dict]:
    # MVP packages for VND top-up. Adjust credits to match your pricing policy.
    return [
        {"code": "vnd_50000", "amount_vnd": 50000, "credits": 500, "label": "50,000 VND"},
        {"code": "vnd_100000", "amount_vnd": 100000, "credits": 1050, "label": "100,000 VND"},
        {"code": "vnd_200000", "amount_vnd": 200000, "credits": 2150, "label": "200,000 VND"},
    ]


def _find_package(package_code: str) -> Optional[dict]:
    for pkg in _topup_packages():
        if pkg["code"] == package_code:
            return pkg
    return None


def _finalize_payment_success(payment: Payment, db: Session, raw_callback: dict | None = None) -> bool:
    """Idempotently mark payment as succeeded and credit user balance."""
    if payment.status == "succeeded":
        return False

    user = db.query(User).filter(User.id == payment.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    balance_before = user.credits_total
    user.credits_one_time += payment.credits
    user.update_total_credits()

    ledger = CreditLedger(
        user_id=user.id,
        delta_credits=payment.credits,
        balance_before=balance_before,
        balance_after=user.credits_total,
        entry_type="topup",
        reference_type="payment",
        reference_id=payment.id,
        note=f"PayOS top-up {payment.amount_vnd} VND",
    )
    db.add(ledger)

    payment.status = "succeeded"
    if raw_callback is not None:
        payment.raw_callback = json.dumps(raw_callback, ensure_ascii=True)
    payment.paid_at = datetime.utcnow()
    db.commit()
    return True


class CreatePaymentIntentRequest(BaseModel):
    package_code: str
    idempotency_key: Optional[str] = None


@router.get("/billing/v1/packages")
def get_topup_packages(user: User = Depends(get_current_user)):
    return {"provider": "payos", "currency": "VND", "packages": _topup_packages()}


@router.post("/billing/v1/payment-intents")
def create_payment_intent(
    request: Request,
    payload: CreatePaymentIntentRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _is_payos_configured():
        raise HTTPException(status_code=503, detail="PayOS is not configured on server")

    package = _find_package(payload.package_code)
    if not package:
        raise HTTPException(status_code=400, detail="Invalid package_code")

    # Multi-tab guard: reuse only near-simultaneous requests (same user/package)
    # to prevent stale/processed checkout links from being reused.
    recent_cutoff = datetime.utcnow() - timedelta(seconds=8)
    recent_pending = (
        db.query(Payment)
        .filter(
            Payment.user_id == user.id,
            Payment.status == "pending",
            Payment.amount_vnd == package["amount_vnd"],
            Payment.credits == package["credits"],
            Payment.created_at >= recent_cutoff,
        )
        .order_by(Payment.created_at.desc())
        .first()
    )
    if recent_pending and recent_pending.checkout_url:
        should_reuse = False
        try:
            client = _payos_client()
            check_resp = client.payment_requests.get(
                int(recent_pending.provider_txn_ref)
                if str(recent_pending.provider_txn_ref).isdigit()
                else recent_pending.provider_txn_ref
            )
            check_dict = _to_dict(check_resp)
            check_data = check_dict.get("data", check_dict)
            check_status = str(
                check_data.get("status")
                or check_data.get("paymentStatus")
                or ""
            ).upper()
            should_reuse = check_status in {"PENDING", "PROCESSING"}
        except Exception:
            # If provider check fails, prefer creating a fresh checkout link.
            should_reuse = False

        if should_reuse:
            return {
                "payment_id": recent_pending.id,
                "provider": recent_pending.provider,
                "status": recent_pending.status,
                "checkout_url": recent_pending.checkout_url,
                "amount_vnd": recent_pending.amount_vnd,
                "credits": recent_pending.credits,
                "expires_at": None,
                "reused": True,
            }

        recent_pending.status = "cancelled"
        db.commit()

    if payload.idempotency_key:
        existing = (
            db.query(Payment)
            .filter(Payment.user_id == user.id, Payment.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing:
            return {
                "payment_id": existing.id,
                "provider": existing.provider,
                "status": existing.status,
                "checkout_url": existing.checkout_url,
            }

    order_code = _new_order_code()
    create_date = _now_vn()
    expire_date = create_date + timedelta(minutes=settings.PAYOS_EXPIRE_MINUTES)

    try:
        client = _payos_client()
        create_resp = client.payment_requests.create(
            payment_data=CreatePaymentLinkRequest(
                order_code=order_code,
                amount=package["amount_vnd"],
                description=f"Top up {package['credits']} credits",
                cancel_url=settings.PAYOS_CANCEL_URL,
                return_url=settings.PAYOS_RETURN_URL,
            )
        )
        create_resp_dict = _to_dict(create_resp)
        data = create_resp_dict.get("data", create_resp_dict)
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"PayOS error {e.error_code}: {e.error_desc}")

    checkout_url = data.get("checkoutUrl") or data.get("checkout_url")
    payment_link_id = data.get("paymentLinkId") or data.get("payment_link_id")

    if not checkout_url:
        raise HTTPException(status_code=502, detail="PayOS response missing checkout URL")

    payment = Payment(
        user_id=user.id,
        provider="payos",
        provider_txn_ref=str(order_code),
        provider_transaction_id=payment_link_id,
        amount_vnd=package["amount_vnd"],
        credits=package["credits"],
        currency="VND",
        status="pending",
        checkout_url=checkout_url,
        description=f"Top-up package {package['code']}",
        idempotency_key=payload.idempotency_key,
        raw_request=json.dumps(create_resp_dict, ensure_ascii=True),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    return {
        "payment_id": payment.id,
        "provider": payment.provider,
        "status": payment.status,
        "checkout_url": payment.checkout_url,
        "amount_vnd": payment.amount_vnd,
        "credits": payment.credits,
        "expires_at": expire_date.isoformat(),
    }


@router.post("/billing/v1/payos/webhook")
async def payos_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        raw_body = await request.body()
        client = _payos_client()
        webhook_obj = client.webhooks.verify(raw_body)
        webhook_dict = _to_dict(webhook_obj)
        data = webhook_dict.get("data", webhook_dict)

        order_code = data.get("orderCode") or data.get("order_code")
        if order_code is None:
            raise HTTPException(status_code=400, detail="Missing orderCode in webhook")

        payment = db.query(Payment).filter(Payment.provider_txn_ref == str(order_code)).first()
        if not payment:
            raise HTTPException(status_code=404, detail="Order not found")

        # Idempotent ack for retries after order was already settled.
        if payment.status == "succeeded":
            return {"error": 0, "message": "already_confirmed"}

        paid_amount = int(data.get("amount", 0) or 0)
        if paid_amount != payment.amount_vnd:
            payment.status = "failed"
            payment.raw_callback = json.dumps(webhook_dict, ensure_ascii=True)
            db.commit()
            return {"error": 1, "message": "invalid_amount"}

        _finalize_payment_success(payment, db, raw_callback=webhook_dict)
        return {"error": 0, "message": "ok"}
    except WebhookError as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {str(e)}")
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"PayOS error {e.error_code}: {e.error_desc}")


@router.get("/billing/v1/payos/return", response_class=HTMLResponse)
def payos_return(request: Request, orderCode: Optional[str] = None, db: Session = Depends(get_db)):
    payment = None
    if orderCode:
        payment = db.query(Payment).filter(Payment.provider_txn_ref == str(orderCode)).first()

    # Local development often cannot receive provider webhooks directly.
    # Reconcile order status on return callback so credits can be updated promptly.
    if payment and payment.status != "succeeded" and orderCode:
        try:
            client = _payos_client()
            payos_resp = client.payment_requests.get(int(orderCode) if str(orderCode).isdigit() else orderCode)
            payos_dict = _to_dict(payos_resp)
            payos_data = payos_dict.get("data", payos_dict)

            provider_status = str(
                payos_data.get("status")
                or payos_data.get("paymentStatus")
                or ""
            ).upper()
            provider_amount = int(payos_data.get("amount", 0) or 0)
            is_paid = provider_status in {"PAID", "SUCCEEDED", "SUCCESS"}

            if is_paid and provider_amount == int(payment.amount_vnd or 0):
                _finalize_payment_success(payment, db, raw_callback=payos_dict)
        except Exception:
            # Keep return page responsive even if provider reconciliation fails.
            pass

    payment_status = payment.status if payment else "pending"
    message = "Thanh toán thành công. Hệ thống sẽ cập nhật credit ngay khi webhook được xác nhận."
    if payment_status == "succeeded":
        message = "Thanh toán thành công và credit đã được cộng vào tài khoản của bạn."

    return templates.TemplateResponse(
        "payment_success.html",
        {
            "request": request,
            "order_code": orderCode,
            "payment_status": payment_status,
            "message": message,
        },
    )


@router.get("/billing/v1/payos/cancel", response_class=HTMLResponse)
def payos_cancel(request: Request, orderCode: Optional[str] = None, db: Session = Depends(get_db)):
    if orderCode:
        payment = db.query(Payment).filter(Payment.provider_txn_ref == str(orderCode)).first()
        if payment and payment.status == "pending":
            payment.status = "cancelled"
            db.commit()

    return templates.TemplateResponse(
        "payment_cancel.html",
        {
            "request": request,
            "order_code": orderCode,
            "message": "Bạn đã hủy thanh toán. Bạn có thể quay lại bảng giá để thử lại bất cứ lúc nào.",
        },
    )


@router.get("/billing/v1/transactions")
def list_transactions(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payments = (
        db.query(Payment)
        .filter(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "items": [
            {
                "payment_id": p.id,
                "provider": p.provider,
                "provider_txn_ref": p.provider_txn_ref,
                "status": p.status,
                "amount_vnd": p.amount_vnd,
                "credits": p.credits,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            }
            for p in payments
        ]
    }


@router.get("/admin/revenue", response_class=HTMLResponse)
def admin_revenue_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    return templates.TemplateResponse("admin_revenue.html", {"request": request})


@router.get("/billing/v1/admin/revenue")
def admin_revenue_data(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    group_by: str = Query(default="day"),
    limit: int = Query(default=100, ge=10, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)

    if group_by not in ("day", "month"):
        raise HTTPException(status_code=400, detail="group_by must be 'day' or 'month'")

    start_dt = None
    end_dt = None
    if start_date:
        d = _parse_yyyy_mm_dd(start_date, "start_date")
        start_dt = datetime(d.year, d.month, d.day)
    if end_date:
        d = _parse_yyyy_mm_dd(end_date, "end_date")
        end_dt = datetime(d.year, d.month, d.day, 23, 59, 59)
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    query = (
        db.query(Payment, User.email)
        .join(User, User.id == Payment.user_id)
        .filter(Payment.status == "succeeded")
    )
    if start_dt:
        query = query.filter(Payment.paid_at >= start_dt)
    if end_dt:
        query = query.filter(Payment.paid_at <= end_dt)

    rows = query.order_by(Payment.paid_at.desc()).all()

    total_revenue_vnd = 0
    total_credits = 0
    time_series_map = defaultdict(lambda: {"revenue_vnd": 0, "transactions": 0, "credits": 0})
    accounts_map = defaultdict(lambda: {"revenue_vnd": 0, "transactions": 0, "credits": 0})
    recent_transactions = []

    for payment, email in rows:
        total_revenue_vnd += int(payment.amount_vnd or 0)
        total_credits += int(payment.credits or 0)

        paid_time = payment.paid_at or payment.created_at
        if not paid_time:
            continue

        period = paid_time.strftime("%Y-%m") if group_by == "month" else paid_time.strftime("%Y-%m-%d")
        time_series_map[period]["revenue_vnd"] += int(payment.amount_vnd or 0)
        time_series_map[period]["transactions"] += 1
        time_series_map[period]["credits"] += int(payment.credits or 0)

        accounts_map[email]["revenue_vnd"] += int(payment.amount_vnd or 0)
        accounts_map[email]["transactions"] += 1
        accounts_map[email]["credits"] += int(payment.credits or 0)

        if len(recent_transactions) < limit:
            recent_transactions.append(
                {
                    "payment_id": payment.id,
                    "email": email,
                    "provider": payment.provider,
                    "amount_vnd": payment.amount_vnd,
                    "credits": payment.credits,
                    "paid_at": paid_time.isoformat() if paid_time else None,
                }
            )

    time_series = [
        {"period": period, **values}
        for period, values in sorted(time_series_map.items(), key=lambda x: x[0], reverse=True)
    ]

    accounts = [
        {"email": email, **values}
        for email, values in sorted(accounts_map.items(), key=lambda x: x[1]["revenue_vnd"], reverse=True)
    ]

    return {
        "summary": {
            "total_revenue_vnd": total_revenue_vnd,
            "total_transactions": len(rows),
            "total_credits": total_credits,
            "group_by": group_by,
            "start_date": start_date,
            "end_date": end_date,
        },
        "time_series": time_series,
        "accounts": accounts,
        "recent_transactions": recent_transactions,
    }


@router.get("/billing/v1/admin/revenue/export.csv")
def admin_revenue_export_csv(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)

    start_dt = None
    end_dt = None
    if start_date:
        d = _parse_yyyy_mm_dd(start_date, "start_date")
        start_dt = datetime(d.year, d.month, d.day)
    if end_date:
        d = _parse_yyyy_mm_dd(end_date, "end_date")
        end_dt = datetime(d.year, d.month, d.day, 23, 59, 59)
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    query = (
        db.query(Payment, User.email)
        .join(User, User.id == Payment.user_id)
        .filter(Payment.status == "succeeded")
    )
    if start_dt:
        query = query.filter(Payment.paid_at >= start_dt)
    if end_dt:
        query = query.filter(Payment.paid_at <= end_dt)

    rows = query.order_by(Payment.paid_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "paid_at",
        "email",
        "provider",
        "amount_vnd",
        "credits",
        "payment_id",
        "provider_txn_ref",
        "provider_transaction_id",
    ])

    for payment, email in rows:
        paid_time = payment.paid_at or payment.created_at
        writer.writerow([
            paid_time.isoformat() if paid_time else "",
            email,
            payment.provider,
            int(payment.amount_vnd or 0),
            int(payment.credits or 0),
            payment.id,
            payment.provider_txn_ref,
            payment.provider_transaction_id or "",
        ])

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"revenue_export_{timestamp}.csv"
    csv_bytes = output.getvalue().encode("utf-8-sig")

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
