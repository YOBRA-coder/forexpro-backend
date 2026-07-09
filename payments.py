"""
ForexPro Stripe Payment Integration
─────────────────────────────────────
Handles provider subscription payments via Stripe.

Plans:
  FREE     — 0 providers, limited signals
  PRO      — up to 3 providers, $9.99/mo
  ELITE    — unlimited, $29.99/mo

Provider plans:
  BASIC    — $9.99/mo  (list profile, max 50 followers)
  PRO      — $29.99/mo (verified badge, unlimited followers)

SETUP:
  pip install stripe fastapi

  1. Create account at https://stripe.com
  2. Go to Dashboard → Developers → API Keys
  3. Copy your Secret Key and Publishable Key
  4. Create Products + Prices in Stripe dashboard or run: python payments.py --setup
  5. Set up webhook: stripe listen --forward-to localhost:8766/payments/webhook
  6. Add this file to your project and include the router in forexpro_main.py:
       from payments import router as payments_router
       app.include_router(payments_router)

ENVIRONMENT VARIABLES:
  STRIPE_SECRET_KEY      — sk_test_...
  STRIPE_PUBLISHABLE_KEY — pk_test_...
  STRIPE_WEBHOOK_SECRET  — whsec_...
  FRONTEND_URL           — http://localhost:5173
"""

import os
import stripe
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from database import get_db

# ── Stripe config ─────────────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_YOUR_KEY_HERE")
WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_YOUR_SECRET")
FRONTEND_URL    = os.getenv("FRONTEND_URL", "http://localhost:5173")
PK_KEY          = os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_test_YOUR_KEY_HERE")

# ── Price IDs (created in Stripe dashboard) ───────────────────────────────────
# After running --setup below, paste the generated price IDs here
PRICES = {
    "trader_pro":        os.getenv("PRICE_TRADER_PRO",   "price_trader_pro_9_99"),
    "trader_elite":      os.getenv("PRICE_TRADER_ELITE",  "price_trader_elite_29_99"),
    "provider_basic":    os.getenv("PRICE_PROVIDER_BASIC","price_provider_basic_9_99"),
    "provider_pro":      os.getenv("PRICE_PROVIDER_PRO",  "price_provider_pro_29_99"),
}

router = APIRouter(prefix="/payments", tags=["payments"])

REGISTRATION_FEE_USD = float(os.getenv("REGISTRATION_FEE_USD", "2.00"))

# ── Request models ────────────────────────────────────────────────────────────
class CheckoutReq(BaseModel):
    plan:       str  # trader_pro | trader_elite | provider_basic | provider_pro
    user_id:    int
    user_email: str

class RegistrationCheckoutReq(BaseModel):
    user_id:    int
    user_email: str

class PortalReq(BaseModel):
    customer_id: str

# ── Create Stripe checkout session (monthly subscription) ─────────────────────
@router.post("/checkout")
async def create_checkout(req: CheckoutReq):
    """
    Creates a Stripe Checkout session for a MONTHLY subscription purchase.
    Returns: {checkout_url: str} — redirect user here to pay.
    """
    price_id = PRICES.get(req.plan)
    if not price_id:
        raise HTTPException(400, f"Unknown plan: {req.plan}. Valid: {list(PRICES.keys())}")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=req.user_email,
            success_url=f"{FRONTEND_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/payment/cancelled",
            metadata={
                "user_id":  str(req.user_id),
                "plan":     req.plan,
                "kind":     "subscription",
                "platform": "forexpro",
            },
            subscription_data={
                "metadata": {"user_id": str(req.user_id), "plan": req.plan, "kind": "subscription"}
            },
        )
        with get_db() as db:
            db.execute("""INSERT INTO payments (user_id,provider,kind,plan,amount,currency,
                           status,stripe_session_id) VALUES (?,?,?,?,?,?,?,?)""",
                       (req.user_id, "stripe", "subscription", req.plan, 0, "usd",
                        "pending", session.id))
        return {"checkout_url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))

# ── Create Stripe checkout session (one-time registration fee) ────────────────
@router.post("/checkout/registration")
async def create_registration_checkout(req: RegistrationCheckoutReq):
    """
    Creates a ONE-TIME Stripe payment session for the platform's registration/access fee.
    This is a single mode="payment" charge — not a subscription.
    """
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "ForexPro — One-Time Registration Fee"},
                    "unit_amount": int(REGISTRATION_FEE_USD * 100),
                },
                "quantity": 1,
            }],
            customer_email=req.user_email,
            success_url=f"{FRONTEND_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/payment/cancelled",
            metadata={"user_id": str(req.user_id), "kind": "registration", "platform": "forexpro"},
        )
        with get_db() as db:
            db.execute("""INSERT INTO payments (user_id,provider,kind,amount,currency,
                           status,stripe_session_id) VALUES (?,?,?,?,?,?,?)""",
                       (req.user_id, "stripe", "registration", REGISTRATION_FEE_USD, "usd",
                        "pending", session.id))
        return {"checkout_url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))

# ── Customer portal (manage / cancel subscription) ────────────────────────────
@router.post("/portal")
async def customer_portal(req: PortalReq):
    """
    Creates a Stripe Customer Portal session for managing subscriptions.
    """
    try:
        session = stripe.billing_portal.Session.create(
            customer=req.customer_id,
            return_url=f"{FRONTEND_URL}/profile",
        )
        return {"portal_url": session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))

# ── Stripe Webhook handler ────────────────────────────────────────────────────
@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Handles Stripe webhook events.
    Run: stripe listen --forward-to localhost:8766/payments/webhook
    """
    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(body, stripe_signature, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid webhook signature")
    except Exception as e:
        raise HTTPException(400, str(e))

    etype = event["type"]
    data  = event["data"]["object"]

    if etype == "checkout.session.completed":
        await _on_checkout_complete(data)

    elif etype == "invoice.payment_succeeded":
        await _on_payment_succeeded(data)

    elif etype == "invoice.payment_failed":
        await _on_payment_failed(data)

    elif etype == "customer.subscription.deleted":
        await _on_subscription_cancelled(data)

    elif etype == "customer.subscription.updated":
        await _on_subscription_updated(data)

    return {"status": "ok"}

# ── Webhook event handlers ────────────────────────────────────────────────────
async def _on_checkout_complete(session: dict):
    """User completed checkout — upgrade their plan (or mark registration paid) in DB."""
    user_id     = session.get("metadata", {}).get("user_id")
    kind        = session.get("metadata", {}).get("kind", "subscription")
    plan        = session.get("metadata", {}).get("plan", "")
    customer_id = session.get("customer")
    sub_id      = session.get("subscription")
    session_id  = session.get("id")

    if not user_id:
        print("Webhook: No user_id in metadata")
        return

    with get_db() as db:
        db.execute(
            "UPDATE payments SET status='success', updated_at=datetime('now') WHERE stripe_session_id=?",
            (session_id,)
        )

        if kind == "registration":
            db.execute("UPDATE users SET registration_paid=1, stripe_customer_id=? WHERE id=?",
                       (customer_id, int(user_id)))
            db.execute(
                "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                (int(user_id), "system", "Registration Complete ✅",
                 "Your one-time registration fee has been received. Welcome to ForexPro!")
            )
            print(f"Webhook: User {user_id} completed registration payment")
            return

        # Store the plan id exactly as sold (must match PLAN_LIMITS keys in database.py:
        # 'trader_pro' | 'trader_elite' | 'provider_pro'). Previously this collapsed
        # everything down to just 'elite'/'pro', which don't match any PLAN_LIMITS key —
        # so every paying customer silently kept free-tier usage limits, and a
        # provider_pro purchase lost provider access entirely (mapped to plain 'pro').
        db_plan = plan if plan in ("trader_pro", "trader_elite", "provider_pro") else "trader_pro"
        db_role = "provider" if "provider" in plan else None
        expires = (datetime.utcnow() + timedelta(days=30)).isoformat()

        db.execute(
            "UPDATE users SET plan=?, stripe_customer_id=?, stripe_sub_id=?, "
            "subscription_status='active', subscription_expires_at=? WHERE id=?",
            (db_plan, customer_id, sub_id, expires, int(user_id))
        )
        if db_role:
            db.execute("UPDATE users SET role=? WHERE id=?", (db_role, int(user_id)))

        db.execute(
            "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
            (int(user_id), "system", "Payment Successful",
             f"Your {plan} plan is now active. Thank you!")
        )

    print(f"Webhook: User {user_id} upgraded to {db_plan} (plan={plan})")

async def _on_payment_succeeded(invoice: dict):
    """Recurring payment succeeded — keep subscription active."""
    customer_id = invoice.get("customer")
    amount      = invoice.get("amount_paid", 0) / 100
    print(f"Webhook: Payment succeeded for customer {customer_id} — ${amount:.2f}")

    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        if user:
            expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
            db.execute(
                "UPDATE users SET subscription_status='active', subscription_expires_at=? WHERE id=?",
                (expires, user["id"])
            )
            db.execute(
                "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                (user["id"], "system", "Payment Received",
                 f"Your subscription payment of ${amount:.2f} was successful. Thank you!")
            )

async def _on_payment_failed(invoice: dict):
    """Payment failed — notify user, maybe downgrade plan."""
    customer_id = invoice.get("customer")
    print(f"Webhook: Payment FAILED for customer {customer_id}")

    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        if user:
            db.execute("UPDATE users SET subscription_status='past_due' WHERE id=?", (user["id"],))
            db.execute(
                "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                (user["id"], "system", "Payment Failed ⚠️",
                 "Your subscription payment failed. Please update your payment method to keep access.")
            )

async def _on_subscription_cancelled(sub: dict):
    """Subscription cancelled — downgrade to free."""
    customer_id = sub.get("customer")
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        if user:
            db.execute(
                "UPDATE users SET plan='free', stripe_sub_id=NULL, subscription_status='cancelled' WHERE id=?",
                (user["id"],)
            )
            db.execute(
                "INSERT INTO notifications (user_id, type, title, message) VALUES (?,?,?,?)",
                (user["id"], "system", "Subscription Cancelled",
                 "Your subscription has been cancelled. You've been moved to the free plan.")
            )
    print(f"Webhook: Customer {customer_id} cancelled subscription")

async def _on_subscription_updated(sub: dict):
    """Subscription updated (upgrade/downgrade)."""
    customer_id = sub.get("customer")
    status      = sub.get("status")
    print(f"Webhook: Subscription updated for {customer_id} — status: {status}")

# ── Get subscription status ───────────────────────────────────────────────────
@router.get("/subscription/{user_id}")
async def get_subscription(user_id: int):
    """Check current subscription status for a user."""
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "User not found")

        sub_id      = user["stripe_sub_id"] if "stripe_sub_id" in user.keys() else None
        customer_id = user["stripe_customer_id"] if "stripe_customer_id" in user.keys() else None

        if not sub_id:
            return {"plan": user["plan"], "status": "free", "subscription": None}

        try:
            sub = stripe.Subscription.retrieve(sub_id)
            return {
                "plan":         user["plan"],
                "status":       sub.status,
                "current_period_end": sub.current_period_end,
                "cancel_at_period_end": sub.cancel_at_period_end,
                "customer_id":  customer_id,
                "subscription_id": sub_id,
            }
        except stripe.error.StripeError:
            return {"plan": user["plan"], "status": "unknown"}

# ── Plans info endpoint ───────────────────────────────────────────────────────
from mpesa import SUBSCRIPTION_PRICES_KES, REGISTRATION_FEE_KES

@router.get("/plans")
async def get_plans():
    """Return available plans and pricing in both USD (card/Stripe) and KES (M-Pesa)."""
    return {
        "publishable_key": PK_KEY,
        "registration_fee": {"usd": REGISTRATION_FEE_USD, "kes": REGISTRATION_FEE_KES,
                              "note": "One-time fee charged once per account before full access."},
        "plans": [
            {
                "id":       "free",
                "name":     "Free",
                "price_usd": 0, "price_kes": 0,
                "features": ["5 signals/day", "1 provider copy", "Basic education"],
                "cta":      "Get Started",
            },
            {
                "id":       "trader_pro",
                "name":     "Pro Trader",
                "price_usd": 9.99, "price_kes": SUBSCRIPTION_PRICES_KES["trader_pro"],
                "per":      "month",
                "price_id": PRICES["trader_pro"],
                "features": ["Unlimited signals", "3 provider copies", "All education courses", "Email alerts"],
                "cta":      "Start Pro",
                "popular":  True,
            },
            {
                "id":       "trader_elite",
                "name":     "Elite Trader",
                "price_usd": 29.99, "price_kes": SUBSCRIPTION_PRICES_KES["trader_elite"],
                "per":      "month",
                "price_id": PRICES["trader_elite"],
                "features": ["Unlimited everything", "Priority support", "Telegram alerts", "Backtesting engine"],
                "cta":      "Go Elite",
            },
            {
                "id":       "provider_pro",
                "name":     "Signal Provider",
                "price_usd": 29.99, "price_kes": SUBSCRIPTION_PRICES_KES["provider_pro"],
                "per":      "month",
                "price_id": PRICES["provider_pro"],
                "features": ["Verified badge", "Unlimited followers", "Analytics dashboard", "Revenue sharing"],
                "cta":      "Become Provider",
            },
        ]
    }

# ── CLI: create Stripe products ───────────────────────────────────────────────
def setup_stripe_products():
    """Run once to create Stripe products and prices. Copy the price IDs into PRICES dict."""
    print("Creating Stripe products and prices...")

    plans = [
        ("ForexPro Pro Trader",    "trader_pro",     999),
        ("ForexPro Elite Trader",  "trader_elite",   2999),
        ("ForexPro Provider Basic","provider_basic",  999),
        ("ForexPro Provider Pro",  "provider_pro",   2999),
    ]

    for name, key, amount in plans:
        product = stripe.Product.create(name=name, metadata={"key": key})
        price   = stripe.Price.create(
            product=product.id,
            unit_amount=amount,
            currency="usd",
            recurring={"interval": "month"},
            metadata={"key": key},
        )
        print(f"  {key}: {price.id}")

    print("\nCopy these price IDs into the PRICES dict in payments.py")

if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        setup_stripe_products()
    else:
        print("ForexPro Payment Module")
        print("  python payments.py --setup  — Create Stripe products")
        print("  Import router in forexpro_main.py and add:")
        print("    from payments import router as payments_router")
        print("    app.include_router(payments_router)")