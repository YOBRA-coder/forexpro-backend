"""
ForexPro M-Pesa (Safaricom Daraja) Payment Integration
────────────────────────────────────────────────────────
Handles the two payment moments the platform needs:
  • REGISTRATION  — one-time access fee, charged once per account
  • SUBSCRIPTION  — monthly recurring plan (trader_pro / trader_elite / provider_basic / provider_pro)

M-Pesa has no native recurring billing like Stripe, so "monthly subscription" here means:
STK push charges the user now, and on success we set subscription_expires_at = now + 30 days.
The frontend/backend simply re-prompts for STK push when the subscription lapses.

SETUP (Safaricom Daraja — https://developer.safaricom.co.ke):
  1. Create an app in the Daraja portal → get Consumer Key & Consumer Secret
  2. For sandbox testing, shortcode 174379 and the standard passkey work out of the box
  3. Set environment variables:
       MPESA_ENV                 sandbox | production   (default: sandbox)
       MPESA_CONSUMER_KEY
       MPESA_CONSUMER_SECRET
       MPESA_SHORTCODE            e.g. 174379 (sandbox) or your paybill/till
       MPESA_PASSKEY
       MPESA_CALLBACK_URL         a PUBLICLY reachable URL, e.g. https://yourapi.com/payments/mpesa/callback
                                   (use ngrok while developing locally — Safaricom cannot call localhost)
  4. Include the router in forexpro_main.py:
       from mpesa import router as mpesa_router
       app.include_router(mpesa_router)

PRICING (adjust freely):
  Registration fee (one-time): KES 250
  trader_pro:    KES 1,200 / month
  trader_elite:  KES 3,500 / month
  provider_basic:KES 1,200 / month
  provider_pro:  KES 3,500 / month
"""
import os, base64, time, json
from datetime import datetime, timedelta
from typing import Optional

import urllib.request, urllib.error
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from database import get_db
from auth import get_current_user

MPESA_ENV       = os.getenv("MPESA_ENV", "sandbox")
BASE_URL        = "https://sandbox.safaricom.co.ke" if MPESA_ENV == "sandbox" else "https://api.safaricom.co.ke"
CONSUMER_KEY    = os.getenv("MPESA_CONSUMER_KEY", "YOUR_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "YOUR_CONSUMER_SECRET")
SHORTCODE       = os.getenv("MPESA_SHORTCODE", "174379")
PASSKEY         = os.getenv("MPESA_PASSKEY", "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919")
CALLBACK_URL    = os.getenv("MPESA_CALLBACK_URL", "https://example.com/payments/mpesa/callback")

REGISTRATION_FEE_KES = float(os.getenv("REGISTRATION_FEE_KES", "250"))
# Only used to convert a KES wallet deposit into the platform's internal USD ledger
# unit (all trading math — margin, pip value — is USD-denominated). Update this to
# roughly track the real KES/USD rate; it is NOT a live FX feed.
USD_KES_RATE = float(os.getenv("USD_KES_RATE", "129"))
MIN_WITHDRAWAL_USD = float(os.getenv("MIN_WITHDRAWAL_USD", "10"))
SUBSCRIPTION_PRICES_KES = {
    "trader_pro":     1,
    "trader_elite":   1,
    "provider_basic": 1,
    "provider_pro":   1,
}

router = APIRouter(prefix="/payments/mpesa", tags=["mpesa"])


def _http_json(url: str, data: Optional[dict] = None, headers: Optional[dict] = None, method: str = "GET") -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def get_access_token() -> str:
    if CONSUMER_KEY.startswith("YOUR_"):
        raise HTTPException(500, "M-Pesa is not configured yet — set MPESA_CONSUMER_KEY/SECRET env vars.")
    creds = base64.b64encode(f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()).decode()
    try:
        data = _http_json(
            f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}"},
        )
        return data["access_token"]
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"M-Pesa auth failed: {e.read().decode()[:300]}")


def _password_and_timestamp():
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    pwd = base64.b64encode(f"{SHORTCODE}{PASSKEY}{ts}".encode()).decode()
    return pwd, ts


def normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "").replace("+", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    if p.startswith("7") or p.startswith("1"):
        p = "254" + p
    return p


class StkPushReq(BaseModel):
    phone: str
    kind: str            # registration | subscription | wallet_deposit
    plan: Optional[str] = None    # required if kind == subscription
    amount_usd: Optional[float] = None  # required if kind == wallet_deposit


class StatusReq(BaseModel):
    checkout_request_id: str


@router.post("/stkpush")
async def stk_push(req: StkPushReq, user=Depends(get_current_user)):
    """Initiate an STK push (M-Pesa 'Lipa na M-Pesa' prompt) to the user's phone."""
    phone = normalize_phone(req.phone)
    if req.kind == "registration":
        amount = REGISTRATION_FEE_KES
        desc = "ForexPro Registration Fee"
    elif req.kind == "subscription":
        if req.plan not in SUBSCRIPTION_PRICES_KES:
            raise HTTPException(400, f"Unknown plan: {req.plan}")
        amount = SUBSCRIPTION_PRICES_KES[req.plan]
        desc = f"ForexPro {req.plan} — monthly"
    elif req.kind == "wallet_deposit":
        if not req.amount_usd or req.amount_usd <= 0:
            raise HTTPException(400, "amount_usd must be a positive number")
        amount = round(req.amount_usd * USD_KES_RATE, 2)
        desc = f"ForexPro wallet deposit (${req.amount_usd:.2f})"
    else:
        raise HTTPException(400, "kind must be 'registration', 'subscription', or 'wallet_deposit'")

    token = get_access_token()
    password, timestamp = _password_and_timestamp()
    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": f"ForexPro-{user['id']}",
        "TransactionDesc": desc,
    }
    try:
        result = _http_json(
            f"{BASE_URL}/mpesa/stkpush/v1/processrequest",
            data=payload,
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"STK push failed: {e.read().decode()[:300]}")

    checkout_id = result.get("CheckoutRequestID")
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO payments (user_id, provider, kind, plan, amount, currency, status,
                                   checkout_request_id, merchant_request_id, phone, raw_response)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (user["id"], "mpesa", req.kind, req.plan, amount, "KES", "pending",
              checkout_id, result.get("MerchantRequestID"), phone, json.dumps(result)))
        if req.kind == "wallet_deposit":
            db.execute("""INSERT INTO wallet_transactions
                (user_id, type, amount_usd, method, status, phone, payment_id)
                VALUES (?,'deposit',?,'mpesa','pending',?,?)""",
                (user["id"], req.amount_usd, phone, cur.lastrowid))
    return {
        "success": True,
        "checkout_request_id": checkout_id,
        "customer_message": result.get("CustomerMessage", "Check your phone to complete payment."),
    }


@router.post("/status")
async def check_status(req: StatusReq, user=Depends(get_current_user)):
    """Frontend polls this while the user completes the STK prompt on their phone."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM payments WHERE checkout_request_id=? AND user_id=?",
            (req.checkout_request_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(404, "Payment record not found")
        return {"status": row["status"], "kind": row["kind"], "plan": row["plan"],
                "amount": row["amount"], "mpesa_receipt": row["mpesa_receipt"]}


@router.post("/callback")
async def mpesa_callback(payload: dict):
    """Safaricom calls this URL asynchronously once the user accepts/rejects/times out the STK prompt."""
    try:
        stk = payload["Body"]["stkCallback"]
        checkout_id = stk["CheckoutRequestID"]
        result_code = stk["ResultCode"]
        receipt = None
        if result_code == 0:
            items = {i["Name"]: i.get("Value") for i in stk.get("CallbackMetadata", {}).get("Item", [])}
            receipt = items.get("MpesaReceiptNumber")

        with get_db() as db:
            row = db.execute("SELECT * FROM payments WHERE checkout_request_id=?", (checkout_id,)).fetchone()
            if not row:
                return {"ResultCode": 0, "ResultDesc": "Accepted (no matching record)"}

            new_status = "success" if result_code == 0 else "failed"
            db.execute(
                "UPDATE payments SET status=?, mpesa_receipt=?, raw_response=?, updated_at=datetime('now') WHERE id=?",
                (new_status, receipt, json.dumps(payload), row["id"])
            )

            if new_status == "success":
                if row["kind"] == "registration":
                    db.execute("UPDATE users SET registration_paid=1 WHERE id=?", (row["user_id"],))
                elif row["kind"] == "subscription":
                    expires = (datetime.now() + timedelta(days=30)).isoformat()
                    role_update = ""
                    plan = row["plan"] or "trader_pro"
                    db_plan = plan if plan in ("trader_pro", "trader_elite", "provider_pro") else "trader_pro"
                    db.execute(
                        "UPDATE users SET plan=?, subscription_status='active', subscription_expires_at=?, mpesa_phone=? WHERE id=?",
                        (db_plan, expires, row["phone"], row["user_id"])
                    )
                    if "provider" in plan:
                        db.execute("UPDATE users SET role='provider' WHERE id=?", (row["user_id"],))
                elif row["kind"] == "wallet_deposit":
                    amount_usd = round(row["amount"] / USD_KES_RATE, 2)
                    db.execute("UPDATE users SET balance = balance + ?, mpesa_phone=? WHERE id=?",
                               (amount_usd, row["phone"], row["user_id"]))
                    db.execute("""UPDATE wallet_transactions SET status='completed', mpesa_receipt=?,
                                  processed_at=datetime('now') WHERE payment_id=?""", (receipt, row["id"]))
                db.execute(
                    "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                    (row["user_id"], "system", "Payment Successful ✅",
                     f"Your M-Pesa payment of KES {row['amount']:.0f} was received. Receipt: {receipt or 'N/A'}")
                )
            else:
                if row["kind"] == "wallet_deposit":
                    db.execute("""UPDATE wallet_transactions SET status='rejected',
                                  admin_note='M-Pesa payment cancelled/timed out', processed_at=datetime('now')
                                  WHERE payment_id=?""", (row["id"],))
                db.execute(
                    "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                    (row["user_id"], "system", "Payment Not Completed ⚠️",
                     "Your M-Pesa payment was cancelled or timed out. You can try again anytime.")
                )
    except Exception as e:
        print(f"[MPESA] callback error: {e}")

    return {"ResultCode": 0, "ResultDesc": "Accepted"}
