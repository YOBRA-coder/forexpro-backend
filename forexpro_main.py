"""
ForexPro API — Complete Backend
Routes: /auth, /signals, /copy, /providers, /education, /journal, /prices, /ws
DB: SQLite (forexpro.db)
"""
from fastapi import FastAPI, Query, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import json, time, asyncio
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[WARN] python-dotenv not installed — relying on real environment variables only. "
          "Run: pip install python-dotenv --break-system-packages")

from database import get_db, init_db, hash_password, verify_password, is_subscription_active, recompute_provider_stats, plan_limits, effective_plan
from signals import (get_ohlcv, add_indicators, build_signal, get_live_quote,
                     PAIR_CONFIG, TF_MAP, detect_support_resistance,
                     detect_trendline, build_markers, pip_value_usd, compute_margin_usd, run_backtest,
                     compute_risk_based_lot, _low_liquidity_window)
from payments import router as payments_router
from mpesa import router as mpesa_router
from bridge import router as bridge_router
import pandas as pd
import time
       

app = FastAPI(title="ForexPro API", version="4.0.0", docs_url="/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from auth import create_token, decode_token, get_current_user, get_optional_user, security

# ── Pydantic Models ───────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    email: str; username: str; password: str

class LoginReq(BaseModel):
    email: str; password: str

class GenerateSignalReq(BaseModel):
    pair: str = "EURUSD"; timeframe: str = "H1"

class BulkSignalReq(BaseModel):
    pairs: List[str] = ["EURUSD","GBPUSD","USDJPY","XAUUSD"]
    timeframes: List[str] = ["H1","H4"]
    min_confidence: int = 0
    direction_filter: str = "ALL"

class SubscribeReq(BaseModel):
    provider_id: int; risk_pct: float = 2.0; max_lot: float = 0.05
    min_confidence: int = 65; auto_copy: bool = True; auto_execute: bool = False
    pairs_filter: List[str] = []

class UpdateProgressReq(BaseModel):
    course_id: int; lesson_idx: int; completed: bool = False; score: int = 0

class JournalEntryReq(BaseModel):
    pair: str; direction: str; entry_price: float; exit_price: float
    lot_size: float; pnl_usd: float; pnl_pips: float
    notes: str = ""; emotion: str = "calm"; setup: str = ""

class UpdateProfileReq(BaseModel):
    bio: str = ""; broker: str = ""; mt5_login: str = ""; mt5_server: str = ""

class SettingsReq(BaseModel):
    email_alerts_enabled: Optional[bool] = None
    default_lot_size: Optional[float] = None
    default_risk_pct: Optional[float] = None

class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str

class ProviderRegisterReq(BaseModel):
    display_name: str; description: str = ""; monthly_fee: float = 0

class ProviderUpdateReq(BaseModel):
    display_name: Optional[str] = None; description: Optional[str] = None
    monthly_fee: Optional[float] = None

class CopySignalReq(BaseModel):
    lot_size: float = 0.01; risk_pct: float = 2.0; execute_live: bool = False

class QuickTradeReq(BaseModel):
    pair: str
    direction: str  # BUY | SELL
    lot_size: float = 0.01
    sl_pips: float = 30
    tp_pips: float = 60
    execute_live: bool = False

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    asyncio.create_task(price_broadcaster_loop())
    asyncio.create_task(auto_signal_loop())
    asyncio.create_task(settlement_loop())

app.include_router(payments_router)
app.include_router(mpesa_router)
app.include_router(bridge_router)
# ── Auth Routes ───────────────────────────────────────────────────────────────
@app.post("/auth/register")
def register(req: RegisterReq):
    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE email=? OR username=?",
                              (req.email, req.username)).fetchone()
        if existing: raise HTTPException(400, "Email or username already taken")
        cursor = db.execute(
            "INSERT INTO users (email,username,password) VALUES (?,?,?)",
            (req.email, req.username, hash_password(req.password)))
        user_id = cursor.lastrowid
        db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user_id,))
        token = create_token(user_id, req.username)
        return {"token": token, "user": {"id": user_id, "username": req.username,
                "email": req.email, "role": "trader", "plan": "free",
                "registration_paid": 0, "subscription_status": "inactive",
                "subscription_active": True}}

@app.post("/auth/login")
def login(req: LoginReq):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (req.email,)).fetchone()
        if not user or not verify_password(req.password, user["password"]):
            raise HTTPException(401, "Invalid email or password")
        db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
        token = create_token(user["id"], user["username"])
        return {"token": token, "user": {
            "id": user["id"], "username": user["username"],
            "email": user["email"], "role": user["role"],
            "plan": user["plan"], "balance": user["balance"],
            "equity": user["equity"], "broker": user["broker"],
            "mt5_login": user["mt5_login"], "mt5_server": user["mt5_server"],
            "registration_paid": user["registration_paid"] or 0,
            "subscription_status": user["subscription_status"] or "inactive",
            "subscription_expires_at": user["subscription_expires_at"],
            "subscription_active": is_subscription_active(dict(user)),
        }}

@app.get("/auth/me")
def get_me(user=Depends(get_current_user)):
    with get_db() as db:
        equity = sync_equity(db, user["id"])
        notifs = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                            (user["id"],)).fetchone()[0]
        subs = db.execute("SELECT COUNT(*) FROM subscriptions WHERE follower_id=? AND is_active=1",
                          (user["id"],)).fetchone()[0]
        fresh = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        _maybe_notify_subscription_expiry(db, dict(fresh))
        new_token = create_token(user["id"], user["username"])  # sliding session — see component.jsx
        return {**{k:v for k,v in dict(fresh).items() if k!="password"},
                "equity": equity, "token": new_token,
                "unread_notifications": notifs, "active_subscriptions": subs,
                "subscription_active": is_subscription_active(dict(fresh))}

def _maybe_notify_subscription_expiry(db, user_row):
    """Warn once when a paid plan is within 3 days of expiring, and once more when
    it actually lapses — so a downgrade to free-tier limits never comes as a
    surprise mid-session."""
    exp = user_row.get("subscription_expires_at")
    if not exp or user_row.get("plan") == "free":
        return
    try:
        expires_at = datetime.fromisoformat(exp.replace("Z", ""))
    except Exception:
        return
    days_left = (expires_at - datetime.utcnow()).total_seconds() / 86400
    uid = user_row["id"]
    if 0 <= days_left <= 3:
        dupe = db.execute("""SELECT id FROM notifications WHERE user_id=? AND type='billing'
                              AND title LIKE 'Subscription expiring%'
                              AND created_at > datetime('now','-2 days')""", (uid,)).fetchone()
        if not dupe:
            db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
                (uid, "billing", "Subscription expiring soon",
                 f"Your {user_row['plan']} plan expires in {max(int(days_left),0)} day(s). "
                 f"Renew in Billing to keep your current limits and features."))
    elif days_left < 0:
        dupe = db.execute("""SELECT id FROM notifications WHERE user_id=? AND type='billing'
                              AND title = 'Subscription expired'
                              AND created_at > datetime('now','-2 days')""", (uid,)).fetchone()
        if not dupe:
            db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
                (uid, "billing", "Subscription expired",
                 "Your plan has expired and your account reverted to Free-tier limits. Renew anytime in Billing."))

@app.put("/auth/profile")
def update_profile(req: UpdateProfileReq, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("UPDATE users SET bio=?,broker=?,mt5_login=?,mt5_server=? WHERE id=?",
                   (req.bio, req.broker, req.mt5_login, req.mt5_server, user["id"]))
        fresh = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"success": True, "user": {k: v for k, v in dict(fresh).items() if k != "password"}}

@app.put("/auth/settings")
def update_settings(req: SettingsReq, user=Depends(get_current_user)):
    with get_db() as db:
        if req.email_alerts_enabled is not None:
            db.execute("UPDATE users SET email_alerts_enabled=? WHERE id=?",
                       (int(req.email_alerts_enabled), user["id"]))
        if req.default_lot_size is not None:
            db.execute("UPDATE users SET default_lot_size=? WHERE id=?", (req.default_lot_size, user["id"]))
        if req.default_risk_pct is not None:
            db.execute("UPDATE users SET default_risk_pct=? WHERE id=?", (req.default_risk_pct, user["id"]))
        fresh = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"success": True, "user": {k: v for k, v in dict(fresh).items() if k != "password"}}

@app.post("/auth/change-password")
def change_password(req: ChangePasswordReq, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
        if not verify_password(req.current_password, row["password"]):
            raise HTTPException(400, "Current password is incorrect")
        if len(req.new_password) < 8:
            raise HTTPException(400, "New password must be at least 8 characters")
        db.execute("UPDATE users SET password=? WHERE id=?", (hash_password(req.new_password), user["id"]))
    return {"success": True}

# ── Signal Routes ─────────────────────────────────────────────────────────────
@app.post("/signals/generate")
def generate_signal(req: GenerateSignalReq, user=Depends(get_current_user)):
    if req.pair not in PAIR_CONFIG: raise HTTPException(400, "Unknown pair")
    if req.timeframe not in TF_MAP: raise HTTPException(400, "Unknown timeframe")

    limits = plan_limits(effective_plan(user))
    if limits["signals_per_day"] is not None:
        with get_db() as db:
            used = db.execute(
                "SELECT COUNT(*) c FROM signals WHERE provider_id=? AND date(created_at)=date('now')",
                (user["id"],)).fetchone()["c"]
        if used >= limits["signals_per_day"]:
            raise HTTPException(403, f"Free plan is limited to {limits['signals_per_day']} signals/day — "
                                      f"you've used all {used}. Upgrade to Pro for unlimited signals.")

    df = get_ohlcv(req.pair, req.timeframe, 250)
    df = add_indicators(df)
    sig = build_signal(req.pair, req.timeframe, df, provider_id=user["id"])

    if sig["direction"] == "NO_TRADE":
        # Nothing tradeable — don't burn the user's daily signal quota on a
        # non-actionable result, and don't try to persist it (stop_loss/take_profit
        # are NOT NULL on the signals table, correctly, since a real signal always
        # has them — a NO_TRADE isn't a signal to track, it's just an answer).
        sig.pop("ohlcv", None)
        return sig

    ohlcv = sig.pop("ohlcv", None)
    chart_data = {
        "ohlcv": ohlcv,
        "markers": sig.get("markers", []),
        "support_resistance": sig.get("support_resistance", []),
        "trendline": sig.get("trendline"),
    }

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO signals (provider_id,pair,timeframe,direction,strength,confidence,
            entry_price,stop_loss,take_profit,sl_pips,tp_pips,risk_reward,rsi,macd,
            ema20,ema50,bb_upper,bb_lower,stoch_k,atr,candle_pattern,chart_pattern,
            entry_time,ai_analysis,expires_at,chart_data)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (user["id"], sig["pair"], sig["timeframe"], sig["direction"], sig["strength"],
              sig["confidence"], sig["entry_price"], sig["stop_loss"], sig["take_profit"],
              sig["sl_pips"], sig["tp_pips"], sig["risk_reward"], sig["rsi"], sig["macd"],
              sig["ema20"], sig["ema50"], sig["bb_upper"], sig["bb_lower"], sig["stoch_k"],
              sig["atr"], sig["candle_pattern"], sig["chart_pattern"], sig["entry_time"],
              sig["ai_analysis"], sig["expires_at"], json.dumps(chart_data)))
        sig_id = cur.lastrowid
        
        # Distribute to subscribers — auto_copy=1 opens immediately (reserving margin),
        # auto_copy=0 creates a pending_approval row the follower must approve/decline
        # on the Copy Trading page (previously: nothing happened at all for manual
        # subscribers, which is why "manual" looked broken).
        subs = db.execute(
            "SELECT * FROM subscriptions WHERE provider_id=? AND is_active=1",
            (user["id"],)).fetchall()
        copies_created = 0
        for sub in subs:
            if sig["confidence"] < sub["min_confidence"]:
                continue
            pf = json.loads(sub["pairs_filter"] or "[]")
            if pf and sig["pair"] not in pf:
                continue

            if not sub["auto_copy"]:
                # Manual mode: suggest it, don't spend the follower's balance yet.
                db.execute("""INSERT INTO copy_trades
                    (follower_id,provider_id,signal_id,lot_size,risk_pct,entry_price,
                     stop_loss,take_profit,status,execution_mode)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (sub["follower_id"], user["id"], sig_id, sub["max_lot"], sub["risk_pct"],
                     sig["entry_price"], sig["stop_loss"], sig["take_profit"], "pending_approval", "simulated"))
                db.execute("""INSERT INTO notifications (user_id,type,title,message)
                    VALUES (?,?,?,?)""",
                    (sub["follower_id"], "signal",
                     f"Review to copy: {sig['pair']} {sig['direction']}",
                     f"{sig['confidence']}% confidence — open Copy Trading to approve or decline."))
                copies_created += 1
                continue

            live = False
            if sub["auto_execute"]:
                follower = db.execute("SELECT bridge_token FROM users WHERE id=?",
                                       (sub["follower_id"],)).fetchone()
                live = bool(follower and follower["bridge_token"])
            exec_mode = "mt5" if live else "simulated"
            status0   = "pending_bridge" if live else "open"
            follower_bal = db.execute("SELECT balance FROM users WHERE id=?", (sub["follower_id"],)).fetchone()["balance"]
            computed_lot = compute_risk_based_lot(follower_bal, sub["risk_pct"], sig["pair"], sig["sl_pips"], sub["max_lot"])
            margin = compute_margin_usd(sig["pair"], computed_lot)
            if follower_bal < margin:
                db.execute("""INSERT INTO notifications (user_id,type,title,message)
                    VALUES (?,?,?,?)""",
                    (sub["follower_id"], "signal", f"Skipped {sig['pair']} — low balance",
                     f"Needed ${margin:.2f} margin but balance is ${follower_bal:.2f}."))
                continue
            db.execute("""INSERT INTO copy_trades
                (follower_id,provider_id,signal_id,lot_size,risk_pct,entry_price,
                 stop_loss,take_profit,status,execution_mode,opened_at,margin_used)
                VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),?)""",
                (sub["follower_id"], user["id"], sig_id,
                 computed_lot, sub["risk_pct"],
                 sig["entry_price"], sig["stop_loss"], sig["take_profit"], status0, exec_mode, margin))
            db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (margin, sub["follower_id"]))
            copies_created += 1
            db.execute("""INSERT INTO notifications (user_id,type,title,message)
                VALUES (?,?,?,?)""",
                (sub["follower_id"], "signal",
                 f"Auto-copied: {sig['pair']} {sig['direction']}",
                 f"Confidence: {sig['confidence']}% | Entry: {sig['entry_price']} | SL: {sig['stop_loss']} | TP: {sig['take_profit']}"))
        
        sig["id"] = sig_id
        sig["copies_distributed"] = copies_created
        sig["ohlcv"] = ohlcv
        broadcast_threadsafe("signals", {"type": "new_signal", "data": sig})
        return sig

@app.post("/signals/bulk")
def bulk_signals(req: BulkSignalReq, user=Depends(get_current_user)):
    limits = plan_limits(effective_plan(user))
    if not limits["bulk_generate"]:
        raise HTTPException(403, "Bulk signal generation requires a Pro plan or above. Upgrade to unlock it.")
    results = []
    seed_base = int(datetime.now().strftime("%Y%m%d%H"))
    for p in req.pairs:
        for tf in req.timeframes:
            if p not in PAIR_CONFIG or tf not in TF_MAP: continue
            try:
                from signals import synthetic_ohlcv
                df = synthetic_ohlcv(p, tf, 300, seed=seed_base + abs(hash(p+tf))%1000)
                df = add_indicators(df)
                sig = build_signal(p, tf, df, provider_id=user["id"])
                ohlcv = sig.pop("ohlcv", None)
                if sig["confidence"] >= req.min_confidence:
                    if req.direction_filter == "ALL" or sig["direction"] == req.direction_filter:
                        results.append({**sig, "ohlcv": ohlcv})
            except Exception as e:
                results.append({"pair": p, "timeframe": tf, "error": str(e)})
    results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    for sig in results[:3]:
        if "error" not in sig:
            broadcast_threadsafe("signals", {"type": "new_signal", "data": sig})
    return {"count": len(results), "signals": results}

def sync_equity(db, user_id: int) -> float:
    """balance + unrealized P&L of every currently-open copy trade, using a fresh
    quote per unique pair. Persists to users.equity so any page reading the cached
    user object still sees a reasonably fresh number."""
    row = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    balance = float(row["balance"]) if row else 0.0
    open_trades = db.execute(
        "SELECT ct.*, s.pair as sig_pair, s.direction as sig_direction "
        "FROM copy_trades ct LEFT JOIN signals s ON ct.signal_id=s.id "
        "WHERE ct.follower_id=? AND ct.status='open'", (user_id,)).fetchall()
    floating = 0.0
    quote_cache = {}
    for t in open_trades:
        pair = t["pair"] or t["sig_pair"]
        if not pair: continue
        if pair not in quote_cache:
            try: quote_cache[pair] = float(get_live_quote(pair)["price"])
            except Exception: quote_cache[pair] = None
        price = quote_cache[pair]
        if price is None: continue
        _, _, pip, _, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])
        is_buy = (t["direction"] or t["sig_direction"]) == "BUY"
        pnl_pips = (price - t["entry_price"]) / pip * (1 if is_buy else -1)
        floating += pip_value_usd(pair, pnl_pips, t["lot_size"])
    equity = round(balance + floating, 2)
    db.execute("UPDATE users SET equity=? WHERE id=?", (equity, user_id))
    return equity

def _expand_signal(row: dict) -> dict:
    d = dict(row)
    raw = d.pop("chart_data", None)
    extra = {"ohlcv": None, "markers": [], "support_resistance": [], "trendline": None}
    if raw:
        try:
            extra.update(json.loads(raw))
        except Exception:
            pass
    return {**d, **extra}

SIGNAL_DELAY_MINUTES = {"free": 15, "trader_pro": 0, "trader_elite": 0, "provider_pro": 0}

@app.get("/signals/latest")
def latest_signals(limit: int = Query(20), user=Depends(get_optional_user)):
    plan = effective_plan(dict(user)) if user else "free"
    delay = SIGNAL_DELAY_MINUTES.get(plan, 15)
    with get_db() as db:
        if delay > 0:
            rows = db.execute("""
                SELECT s.*, u.username as provider_name
                FROM signals s LEFT JOIN users u ON s.provider_id=u.id
                WHERE s.status='active' AND s.created_at <= datetime('now', ?)
                ORDER BY s.created_at DESC LIMIT ?
            """, (f'-{delay} minutes', limit)).fetchall()
        else:
            rows = db.execute("""
                SELECT s.*, u.username as provider_name
                FROM signals s LEFT JOIN users u ON s.provider_id=u.id
                WHERE s.status='active'
                ORDER BY s.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        # Let the free-tier UI show "N real-time signals available on Pro" rather
        # than just silently having fewer results with no explanation.
        realtime_count = 0
        if delay > 0:
            realtime_count = db.execute(
                "SELECT COUNT(*) c FROM signals WHERE status='active' AND created_at > datetime('now', ?)",
                (f'-{delay} minutes',)).fetchone()["c"]
        return {"signals": [_expand_signal(r) for r in rows],
                "plan_delay_minutes": delay, "realtime_signals_locked": realtime_count}

@app.get("/signals/backtest")
def backtest_signal_engine(pair: str = "EURUSD", timeframe: str = "H1", bars: int = 1000,
                            user=Depends(get_current_user)):
    if pair not in PAIR_CONFIG: raise HTTPException(400, "Unknown pair")
    if timeframe not in TF_MAP: raise HTTPException(400, "Unknown timeframe")
    bars = max(200, min(bars, 3000))  # keep this fast enough to run synchronously
    return run_backtest(pair, timeframe, bars)

@app.get("/signals/history")
def signal_history(pair: str = "EURUSD", timeframe: str = "H1", period: str = "1M",
                   user=Depends(get_optional_user)):
    n = {"1M":180,"3M":360,"6M":540,"1Y":720}.get(period, 180)
    from signals import synthetic_ohlcv
    df = synthetic_ohlcv(pair, timeframe, n+250, seed=42+abs(hash(pair))%100)
    df = add_indicators(df)
    signals = []; step = max(1, n//35)
    for i in range(len(df)-step, 250+step, -step):
        try:
            sig = build_signal(pair, timeframe, df.iloc[:i])
            sig.pop("ohlcv", None)
            signals.append(sig)
        except: pass
    signals.sort(key=lambda x: str(x.get("expires_at","")))
    wins = sum(1 for s in signals if s["confidence"] > 62)
    return {"pair": pair, "timeframe": timeframe, "period": period,
            "count": len(signals), "estimated_winrate": round(wins/max(len(signals),1)*100,1),
            "signals": signals}

# NOTE: this dynamic route MUST come after every other static /signals/* route
# (e.g. /signals/history above) — FastAPI/Starlette matches routes in
# registration order, so a /signals/{signal_id} declared earlier will swallow
# /signals/history requests and try (and fail) to parse "history" as an int.
@app.get("/signals/{signal_id}")
def get_signal(signal_id: int, user=Depends(get_optional_user)):
    with get_db() as db:
        row = db.execute("""
            SELECT s.*, u.username as provider_name
            FROM signals s LEFT JOIN users u ON s.provider_id=u.id
            WHERE s.id=?
        """, (signal_id,)).fetchone()
        if not row: raise HTTPException(404, "Signal not found")
        return _expand_signal(row)

# ── Providers & Copy Trading ──────────────────────────────────────────────────
@app.post("/providers/register")
def register_provider(req: ProviderRegisterReq, user=Depends(get_current_user)):
    """Self-service 'become a provider' — requires the Provider Pro plan (this is
    the paid side of the marketplace: providers earn from followers/revenue share,
    so it isn't included in the free/trader plans)."""
    limits = plan_limits(effective_plan(user))
    if not limits["can_be_provider"]:
        raise HTTPException(403, "Becoming a provider requires the Provider Pro plan. Upgrade in Billing to unlock it.")
    with get_db() as db:
        existing = db.execute("SELECT id FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        if existing:
            raise HTTPException(400, "You're already registered as a provider")
        db.execute("""INSERT INTO providers
            (user_id,display_name,description,win_rate,total_signals,total_pips,
             avg_rr,monthly_pips,followers_count,monthly_fee,is_verified,is_active)
            VALUES (?,?,?,0,0,0,0,0,0,?,1,1)""",
            (user["id"], req.display_name, req.description, req.monthly_fee))
        recompute_provider_stats(db, user["id"])
        db.execute("UPDATE users SET role='provider' WHERE id=?", (user["id"],))
        row = db.execute("SELECT * FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        return dict(row)

@app.get("/providers/me")
def my_provider_profile(user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT * FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        if not row:
            raise HTTPException(404, "You haven't registered as a provider yet")
        recompute_provider_stats(db, user["id"])
        row = db.execute("SELECT * FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        return dict(row)

@app.get("/providers/me/followers")
def my_followers(user=Depends(get_current_user)):
    """Lets a provider see who's following them, their copy settings, and their
    trade stats on this provider's signals specifically."""
    with get_db() as db:
        prow = db.execute("SELECT id FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        if not prow:
            raise HTTPException(404, "You haven't registered as a provider yet")
        subs = db.execute("""
            SELECT s.*, u.username, u.plan
            FROM subscriptions s JOIN users u ON s.follower_id=u.id
            WHERE s.provider_id=? AND s.is_active=1
            ORDER BY s.created_at DESC
        """, (user["id"],)).fetchall()
        followers = []
        for s in subs:
            stats = db.execute("""
                SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) pnl,
                       SUM(CASE WHEN status='closed' AND pnl_usd>0 THEN 1 ELSE 0 END) wins,
                       SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed
                FROM copy_trades WHERE follower_id=? AND provider_id=?
            """, (s["follower_id"], user["id"])).fetchone()
            followers.append({
                "follower_id": s["follower_id"], "username": s["username"], "plan": s["plan"],
                "auto_copy": bool(s["auto_copy"]), "min_confidence": s["min_confidence"],
                "risk_pct": s["risk_pct"], "max_lot": s["max_lot"],
                "subscribed_since": s["created_at"],
                "trades_copied": stats["c"], "trades_closed": stats["closed"],
                "wins": stats["wins"] or 0,
                "pnl_from_your_signals": round(stats["pnl"] or 0, 2),
            })
        return {"count": len(followers), "followers": followers}

@app.put("/providers/me")
def update_provider_profile(req: ProviderUpdateReq, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT id FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        if not row:
            raise HTTPException(404, "You haven't registered as a provider yet")
        fields, vals = [], []
        for col, val in [("display_name", req.display_name), ("description", req.description),
                          ("monthly_fee", req.monthly_fee)]:
            if val is not None:
                fields.append(f"{col}=?"); vals.append(val)
        if fields:
            vals.append(user["id"])
            db.execute(f"UPDATE providers SET {', '.join(fields)} WHERE user_id=?", vals)
        row = db.execute("SELECT * FROM providers WHERE user_id=?", (user["id"],)).fetchone()
        return dict(row)

@app.post("/signals/{signal_id}/copy")
def copy_signal_manually(signal_id: int, req: CopySignalReq, user=Depends(get_current_user)):
    """One-off manual copy of a single signal — no subscription required."""
    with get_db() as db:
        sig = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if sig["provider_id"] == user["id"]:
            raise HTTPException(400, "You can't copy your own signal")
        if sig["status"] != "active":
            raise HTTPException(400, "This signal has already closed")
        dup = db.execute(
            "SELECT id FROM copy_trades WHERE follower_id=? AND signal_id=?",
            (user["id"], signal_id)).fetchone()
        if dup:
            raise HTTPException(400, "You've already copied this signal")

        limits = plan_limits(effective_plan(user))
        if limits["copies_per_day"] is not None:
            used = db.execute(
                "SELECT COUNT(*) c FROM copy_trades WHERE follower_id=? AND date(opened_at)=date('now')",
                (user["id"],)).fetchone()["c"]
            if used >= limits["copies_per_day"]:
                raise HTTPException(403, f"Free plan is limited to {limits['copies_per_day']} manual signal "
                                          f"copies/day — you've used all {used}. Upgrade to Pro for unlimited copies.")

        live = False
        if req.execute_live:
            u = db.execute("SELECT bridge_token FROM users WHERE id=?", (user["id"],)).fetchone()
            if not u or not u["bridge_token"]:
                raise HTTPException(400, "Connect your MT5 bridge in Profile first (Profile > MT5 Auto-Trading)")
            closed = _low_liquidity_window()
            if closed:
                raise HTTPException(400, f"Can't place a live order right now — {closed.lower()}. "
                                          f"Simulated copy is still available.")
            live = True

        fresh_balance = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
        computed_lot = compute_risk_based_lot(fresh_balance, req.risk_pct, sig["pair"], sig["sl_pips"], req.lot_size)
        margin = compute_margin_usd(sig["pair"], computed_lot)
        if fresh_balance < margin:
            raise HTTPException(400, f"Insufficient balance — this trade needs ${margin:.2f} margin, "
                                      f"you have ${fresh_balance:.2f}. Reduce lot size or top up.")

        exec_mode = "mt5" if live else "simulated"
        status0   = "pending_bridge" if live else "open"
        cur = db.execute("""INSERT INTO copy_trades
            (follower_id,provider_id,signal_id,lot_size,risk_pct,entry_price,
             stop_loss,take_profit,status,execution_mode,opened_at,margin_used)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),?)""",
            (user["id"], sig["provider_id"], signal_id, computed_lot, req.risk_pct,
             sig["entry_price"], sig["stop_loss"], sig["take_profit"], status0, exec_mode, margin))
        # Reserve the margin immediately — it's released back (plus/minus P&L) on close
        db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (margin, user["id"]))
        return {"copied": True, "copy_trade_id": cur.lastrowid, "pair": sig["pair"],
                "direction": sig["direction"], "execution_mode": exec_mode, "margin_reserved": margin,
                "lot_size": computed_lot}

FOREX_ONLY_PAIRS = {"EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURGBP","EURJPY","GBPJPY"}

@app.post("/trades/quick")
def place_quick_trade(req: QuickTradeReq, user=Depends(get_current_user)):
    """Lets a user place a trade straight from the price chart instead of copying
    a generated signal — same execution/margin machinery as a copied signal."""
    if req.pair not in PAIR_CONFIG: raise HTTPException(400, "Unknown pair")
    if req.pair not in FOREX_ONLY_PAIRS:
        raise HTTPException(400, "Direct trade placement is limited to forex currency pairs")
    if req.direction not in ("BUY", "SELL"): raise HTTPException(400, "direction must be BUY or SELL")
    quote = get_live_quote(req.pair)
    entry = float(quote["ask"] if req.direction == "BUY" else quote["bid"])
    _, _, pip, _, _ = PAIR_CONFIG[req.pair]
    sl = entry - req.sl_pips * pip if req.direction == "BUY" else entry + req.sl_pips * pip
    tp = entry + req.tp_pips * pip if req.direction == "BUY" else entry - req.tp_pips * pip

    with get_db() as db:
        live = False
        if req.execute_live:
            u = db.execute("SELECT bridge_token FROM users WHERE id=?", (user["id"],)).fetchone()
            if not u or not u["bridge_token"]:
                raise HTTPException(400, "Connect your MT5 bridge in Profile first")
            live = True
        margin = compute_margin_usd(req.pair, req.lot_size)
        balance = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
        if balance < margin:
            raise HTTPException(400, f"Insufficient balance — needs ${margin:.2f} margin, you have ${balance:.2f}")
        exec_mode = "mt5" if live else "simulated"
        status0 = "pending_bridge" if live else "open"
        cur = db.execute("""INSERT INTO copy_trades
            (follower_id,provider_id,signal_id,lot_size,risk_pct,entry_price,
             stop_loss,take_profit,status,execution_mode,opened_at,margin_used,pair,direction)
            VALUES (?,NULL,NULL,?,?,?,?,?,?,?,datetime('now'),?,?,?)""",
            (user["id"], req.lot_size, 2.0, round(entry, 5), round(sl, 5), round(tp, 5), status0, exec_mode,
             margin, req.pair, req.direction))
        db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (margin, user["id"]))
        return {"placed": True, "copy_trade_id": cur.lastrowid, "entry_price": round(entry, 5),
                "stop_loss": round(sl, 5), "take_profit": round(tp, 5), "margin_reserved": margin,
                "execution_mode": exec_mode, "pair": req.pair, "direction": req.direction}

@app.post("/copy/trades/{trade_id}/close")
def close_trade_manually(trade_id: int, user=Depends(get_current_user)):
    """Manual close at current market price — for quick trades placed straight from
    the chart (these have no signal to auto-settle) and for closing any open
    position early instead of waiting for TP/SL."""
    with get_db() as db:
        t = db.execute("""SELECT ct.*, COALESCE(ct.pair, s.pair) as pair0, COALESCE(ct.direction, s.direction) as direction0
                           FROM copy_trades ct LEFT JOIN signals s ON ct.signal_id=s.id
                           WHERE ct.id=? AND ct.follower_id=? AND ct.status='open'""",
                        (trade_id, user["id"])).fetchone()
        if not t: raise HTTPException(404, "Open trade not found")
        pair, direction = t["pair0"], t["direction0"]
        if not pair: raise HTTPException(400, "Can't determine this trade's pair")
        quote = get_live_quote(pair)
        close_price = float(quote["bid"] if direction == "BUY" else quote["ask"])
        _, _, pip, _, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])
        pnl_pips = (close_price - t["entry_price"]) / pip * (1 if direction == "BUY" else -1)
        pnl_usd = pip_value_usd(pair, pnl_pips, t["lot_size"])
        result = "win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "breakeven")
        db.execute("""UPDATE copy_trades SET status='closed', result=?, pnl_pips=?, pnl_usd=?,
                      close_price=?, closed_at=datetime('now') WHERE id=?""",
                   (result, round(pnl_pips, 1), pnl_usd, close_price, trade_id))
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                   (float(t["margin_used"] or 0) + pnl_usd, user["id"]))
        db.execute("""INSERT INTO trade_journal
            (user_id,pair,direction,entry_price,exit_price,lot_size,pnl_usd,pnl_pips,notes,setup)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], pair, direction, t["entry_price"], close_price, t["lot_size"], pnl_usd, pnl_pips,
             "Manually closed", "Auto (Copy Trade)" if t["signal_id"] else "Auto (Quick Trade)"))
        return {"closed": True, "close_price": close_price, "pnl_usd": round(pnl_usd, 2), "pnl_pips": round(pnl_pips, 1)}

@app.post("/copy/trades/{trade_id}/approve")
def approve_pending_copy(trade_id: int, user=Depends(get_current_user)):
    """Follower approves a manual-mode copy suggestion — this is the moment margin
    actually gets reserved, matching the manual-copy flow on the Signals page."""
    with get_db() as db:
        t = db.execute("SELECT * FROM copy_trades WHERE id=? AND follower_id=?", (trade_id, user["id"])).fetchone()
        if not t: raise HTTPException(404, "Pending trade not found")
        if t["status"] != "pending_approval": raise HTTPException(400, "This trade is no longer pending")
        sig = db.execute("SELECT pair, status FROM signals WHERE id=?", (t["signal_id"],)).fetchone()
        if sig and sig["status"] != "active":
            db.execute("UPDATE copy_trades SET status='failed', fail_reason='Signal closed before approval' WHERE id=?", (trade_id,))
            raise HTTPException(400, "This signal already closed — can't approve it anymore")
        pair = sig["pair"] if sig else "EURUSD"
        margin = compute_margin_usd(pair, t["lot_size"])
        balance = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
        if balance < margin:
            raise HTTPException(400, f"Insufficient balance — needs ${margin:.2f} margin, you have ${balance:.2f}")
        db.execute("UPDATE copy_trades SET status='open', margin_used=?, opened_at=datetime('now') WHERE id=?", (margin, trade_id))
        db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (margin, user["id"]))
        return {"approved": True, "margin_reserved": margin}

@app.post("/copy/trades/{trade_id}/decline")
def decline_pending_copy(trade_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        t = db.execute("SELECT id FROM copy_trades WHERE id=? AND follower_id=? AND status='pending_approval'",
                        (trade_id, user["id"])).fetchone()
        if not t: raise HTTPException(404, "Pending trade not found")
        db.execute("UPDATE copy_trades SET status='declined' WHERE id=?", (trade_id,))
        return {"declined": True}

@app.get("/providers")
def list_providers(user=Depends(get_optional_user)):
    with get_db() as db:
        rows = db.execute("""
            SELECT p.*, u.username, u.email, u.avatar, u.bio
            FROM providers p JOIN users u ON p.user_id=u.id
            WHERE p.is_active=1
            ORDER BY p.win_rate DESC
        """).fetchall()
        return {"providers": [dict(r) for r in rows]}

@app.get("/providers/{provider_id}")
def get_provider(provider_id: int, user=Depends(get_optional_user)):
    with get_db() as db:
        p = db.execute("""
            SELECT p.*, u.username, u.email, u.bio, u.created_at as member_since
            FROM providers p JOIN users u ON p.user_id=u.id WHERE p.id=?
        """, (provider_id,)).fetchone()
        if not p: raise HTTPException(404)
        signals = db.execute(
            "SELECT * FROM signals WHERE provider_id=? ORDER BY created_at DESC LIMIT 20",
            (p["user_id"],)).fetchall()
        return {**dict(p), "recent_signals": [dict(s) for s in signals]}

@app.post("/copy/subscribe")
def subscribe(req: SubscribeReq, user=Depends(get_current_user)):
    with get_db() as db:
        provider_row = db.execute(
            "SELECT id FROM providers WHERE user_id=? AND is_active=1", (req.provider_id,)).fetchone()
        if not provider_row:
            raise HTTPException(404, "That user isn't an active signal provider")
        if req.provider_id == user["id"]:
            raise HTTPException(400, "You can't subscribe to your own provider profile")

        # Check not already subscribed
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE follower_id=? AND provider_id=? AND is_active=1",
            (user["id"], req.provider_id)).fetchone()
        if existing: raise HTTPException(400, "Already subscribed to this provider")

        limits = plan_limits(effective_plan(user))
        if limits["max_subscriptions"] is not None:
            active_count = db.execute(
                "SELECT COUNT(*) c FROM subscriptions WHERE follower_id=? AND is_active=1",
                (user["id"],)).fetchone()["c"]
            if active_count >= limits["max_subscriptions"]:
                raise HTTPException(403, f"Your plan allows following up to {limits['max_subscriptions']} "
                                          f"provider(s) at once. Upgrade to follow more.")

        db.execute("""INSERT INTO subscriptions 
            (follower_id,provider_id,risk_pct,max_lot,min_confidence,auto_copy,auto_execute,pairs_filter)
            VALUES (?,?,?,?,?,?,?,?)""",
            (user["id"], req.provider_id, req.risk_pct, req.max_lot,
             req.min_confidence, int(req.auto_copy), int(req.auto_execute), json.dumps(req.pairs_filter)))
        
        # Update provider follower count
        db.execute("UPDATE providers SET followers_count=followers_count+1 WHERE user_id=?",
                   (req.provider_id,))
        
        mode_msg = "You are now copying trades automatically." if req.auto_copy else \
                   "Manual mode — you'll get a notification to review and approve each signal before it opens."
        db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
                   (user["id"], "copy", "Copy Trading Activated", mode_msg))
        return {"success": True, "message": mode_msg}

@app.delete("/copy/unsubscribe/{provider_id}")
def unsubscribe(provider_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("UPDATE subscriptions SET is_active=0 WHERE follower_id=? AND provider_id=?",
                   (user["id"], provider_id))
        db.execute("UPDATE providers SET followers_count=MAX(0,followers_count-1) WHERE user_id=?",
                   (provider_id,))
        return {"success": True}

@app.get("/copy/my-trades")
def my_copy_trades(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("""
            SELECT ct.*, COALESCE(u.username, 'ForexPro AI') as provider_name,
                   s.pair as sig_pair, s.timeframe,
                   s.direction as sig_direction, s.ai_analysis, s.candle_pattern
            FROM copy_trades ct
            LEFT JOIN users u ON ct.provider_id=u.id
            LEFT JOIN signals s ON ct.signal_id=s.id
            WHERE ct.follower_id=?
            ORDER BY ct.created_at DESC LIMIT 50
        """, (user["id"],)).fetchall()
        trades = []
        for r in rows:
            d = dict(r)
            # ct.pair/ct.direction (set directly on quick trades) win when present,
            # otherwise fall back to the linked signal's pair/direction.
            d["pair"] = d.get("pair") or d.pop("sig_pair", None)
            d["direction"] = d.get("direction") or d.pop("sig_direction", None)
            trades.append(d)

        closed = [t for t in trades if t["status"] == "closed"]
        total_pnl = sum(t["pnl_usd"] or 0 for t in trades)
        wins = sum(1 for t in closed if (t["pnl_usd"] or 0) > 0)
        losses = sum(1 for t in closed if (t["pnl_usd"] or 0) <= 0)
        return {"trades": trades,
                "stats": {"total": len(trades), "open": len(trades) - len(closed),
                          "wins": wins, "losses": losses, "total_pnl_usd": round(total_pnl, 2)}}

@app.get("/copy/subscriptions")
def my_subscriptions(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("""
            SELECT s.*, p.display_name, p.win_rate, p.total_pips, p.monthly_pips,
                   p.followers_count, p.is_verified, u.username
            FROM subscriptions s
            JOIN providers p ON s.provider_id=p.user_id
            JOIN users u ON p.user_id=u.id
            WHERE s.follower_id=? AND s.is_active=1
        """, (user["id"],)).fetchall()
        return {"subscriptions": [dict(r) for r in rows]}

@app.put("/copy/subscription/{provider_id}")
def update_subscription(provider_id: int, req: SubscribeReq, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("""UPDATE subscriptions SET risk_pct=?,max_lot=?,min_confidence=?,
                      auto_copy=?,auto_execute=?,pairs_filter=? WHERE follower_id=? AND provider_id=?""",
                   (req.risk_pct, req.max_lot, req.min_confidence, int(req.auto_copy),
                    int(req.auto_execute), json.dumps(req.pairs_filter), user["id"], provider_id))
        return {"success": True}

# ── Prices ────────────────────────────────────────────────────────────────────
@app.get("/prices/live")
def live_prices(pairs: str = Query("EURUSD,GBPUSD,USDJPY,AUDUSD,XAUUSD,BTCUSD")):
    pair_list = [p.strip() for p in pairs.split(",") if p.strip() in PAIR_CONFIG]
    prices = [get_live_quote(p) for p in pair_list]
    return {"prices": prices, "updated_at": datetime.now().isoformat()}

@app.get("/prices/chart")
def price_chart(pair: str = "EURUSD", timeframe: str = "H1", candles: int = 500):
    if pair not in PAIR_CONFIG: raise HTTPException(400, "Unknown pair")
    if timeframe not in TF_MAP: raise HTTPException(400, "Unknown timeframe")
    df = get_ohlcv(pair, timeframe, candles + 250)
    df = add_indicators(df)
    tail = df.tail(candles)
    _, _, pip_sz, _, _ = PAIR_CONFIG[pair]
    records = []
    for ts, row in tail.iterrows():
        rng = max(float(row["high"]) - float(row["low"]), pip_sz * 0.1)
        # No real broker tick-volume feed — approximate relative activity from
        # candle range + body size so the chart still has a meaningful volume pane.
        volume = int(rng / pip_sz * 37 + abs(float(row["close"]) - float(row["open"])) / pip_sz * 20)
        records.append({
            "time": int(pd.Timestamp(ts).timestamp()), "open": round(float(row["open"]),5), # type: ignore
            "high": round(float(row["high"]),5), "low": round(float(row["low"]),5),
            "close": round(float(row["close"]),5), "ema20": round(float(row["ema20"]),5),
            "ema50": round(float(row["ema50"]),5), "bb_up": round(float(row["bb_up"]),5),
            "bb_low": round(float(row["bb_low"]),5), "rsi": round(float(row["rsi"]),2),
            "macd_h": round(float(row["macd_h"]),6), "stoch_k": round(float(row["stoch_k"]),2),
            "volume": max(volume, 1),
        })
    direction = "BUY" if float(tail["ema20"].iloc[-1]) > float(tail["ema50"].iloc[-1]) else "SELL"
    return {
        "pair": pair, "timeframe": timeframe, "candles": records,
        "support_resistance": detect_support_resistance(df),
        "trendline": detect_trendline(df),
        "markers": build_markers(df, direction),
        "source": "live" if "live" in str(df.index[0]) else "simulated",
    }

# ── Education ─────────────────────────────────────────────────────────────────
@app.get("/education/courses")
def list_courses(user=Depends(get_optional_user)):
    with get_db() as db:
        courses = db.execute("SELECT id,title,description,category,level,created_at FROM education_courses").fetchall()
        result = []
        for c in courses:
            cd = dict(c)
            if user:
                prog = db.execute("SELECT * FROM user_progress WHERE user_id=? AND course_id=?",
                                  (user["id"], c["id"])).fetchone()
                cd["progress"] = dict(prog) if prog else {"lesson_idx":0,"completed":0,"score":0}
            # Count lessons
            full = db.execute("SELECT lessons FROM education_courses WHERE id=?", (c["id"],)).fetchone()
            try: cd["lesson_count"] = len(json.loads(full["lessons"]))
            except: cd["lesson_count"] = 0
            result.append(cd)
        return {"courses": result}

@app.get("/education/courses/{course_id}")
def get_course(course_id: int, user=Depends(get_optional_user)):
    with get_db() as db:
        course = db.execute("SELECT * FROM education_courses WHERE id=?", (course_id,)).fetchone()
        if not course: raise HTTPException(404, "Course not found")
        cd = dict(course)
        try: cd["lessons"] = json.loads(cd["lessons"])
        except: cd["lessons"] = []
        if user:
            prog = db.execute("SELECT * FROM user_progress WHERE user_id=? AND course_id=?",
                              (user["id"], course_id)).fetchone()
            cd["progress"] = dict(prog) if prog else {"lesson_idx":0,"completed":0,"score":0}
        return cd

@app.post("/education/progress")
def update_progress(req: UpdateProgressReq, user=Depends(get_current_user)):
    with get_db() as db:
        existing = db.execute("SELECT id FROM user_progress WHERE user_id=? AND course_id=?",
                              (user["id"], req.course_id)).fetchone()
        if existing:
            db.execute("""UPDATE user_progress SET lesson_idx=?,completed=?,score=?,
                          updated_at=datetime('now') WHERE user_id=? AND course_id=?""",
                       (req.lesson_idx, int(req.completed), req.score, user["id"], req.course_id))
        else:
            db.execute("""INSERT INTO user_progress (user_id,course_id,lesson_idx,completed,score)
                          VALUES (?,?,?,?,?)""",
                       (user["id"], req.course_id, req.lesson_idx, int(req.completed), req.score))
        if req.completed:
            db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
                       (user["id"], "education", "Course Completed!",
                        f"You completed a course with score {req.score}%!"))
        return {"success": True}

@app.get("/education/my-progress")
def my_progress(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("""
            SELECT up.*, ec.title, ec.category, ec.level
            FROM user_progress up JOIN education_courses ec ON up.course_id=ec.id
            WHERE up.user_id=?
        """, (user["id"],)).fetchall()
        completed = sum(1 for r in rows if r["completed"])
        return {"progress": [dict(r) for r in rows],
                "stats": {"enrolled": len(rows), "completed": completed,
                          "in_progress": len(rows)-completed}}

# ── Trade Journal ─────────────────────────────────────────────────────────────
@app.post("/journal")
def add_journal(req: JournalEntryReq, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("""INSERT INTO trade_journal 
            (user_id,pair,direction,entry_price,exit_price,lot_size,pnl_usd,pnl_pips,notes,emotion,setup)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], req.pair, req.direction, req.entry_price, req.exit_price,
             req.lot_size, req.pnl_usd, req.pnl_pips, req.notes, req.emotion, req.setup))
        return {"success": True}

@app.get("/journal")
def get_journal(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM trade_journal WHERE user_id=? ORDER BY traded_at DESC LIMIT 100",
            (user["id"],)).fetchall()
        trades = [dict(r) for r in rows]
        total_pnl = sum(t["pnl_usd"] or 0 for t in trades)
        wins = sum(1 for t in trades if (t["pnl_usd"] or 0) > 0)
        best = max((t["pnl_usd"] or 0 for t in trades), default=0)
        worst = min((t["pnl_usd"] or 0 for t in trades), default=0)
        return {"trades": trades,
                "stats": {"total": len(trades), "wins": wins, "losses": len(trades)-wins,
                          "win_rate": round(wins/max(len(trades),1)*100,1),
                          "total_pnl": round(total_pnl,2),
                          "best_trade": round(best,2), "worst_trade": round(worst,2)}}

# ── Notifications ─────────────────────────────────────────────────────────────
# ── Wallet ────────────────────────────────────────────────────────────────────
class WithdrawReq(BaseModel):
    amount_usd: float
    phone: str

@app.get("/wallet/summary")
def wallet_summary(user=Depends(get_current_user)):
    with get_db() as db:
        fresh = db.execute("SELECT balance, equity FROM users WHERE id=?", (user["id"],)).fetchone()
        pending_withdrawals = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) FROM wallet_transactions WHERE user_id=? AND type='withdrawal' AND status='pending'",
            (user["id"],)).fetchone()[0]
        return {"balance": fresh["balance"], "equity": fresh["equity"],
                "pending_withdrawals_usd": pending_withdrawals}

@app.get("/wallet/transactions")
def wallet_transactions(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (user["id"],)).fetchall()
        return {"transactions": [dict(r) for r in rows]}

@app.post("/wallet/withdraw/request")
def request_withdrawal(req: WithdrawReq, user=Depends(get_current_user)):
    """Queues a withdrawal request and reserves the funds immediately (so the same
    balance can't be withdrawn twice or spent on a trade while pending). Actual
    payout (M-Pesa B2C or bank transfer) is NOT automated — Safaricom B2C requires
    a separate business registration/approval beyond what STK push (receiving
    payments) needs, so this creates a request an admin fulfills manually and
    marks complete via /wallet/withdrawals/{id}/approve."""
    if req.amount_usd <= 0:
        raise HTTPException(400, "Amount must be positive")
    with get_db() as db:
        balance = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
        if balance < req.amount_usd:
            raise HTTPException(400, f"Insufficient balance — you have ${balance:.2f}")
        db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (req.amount_usd, user["id"]))
        cur = db.execute("""INSERT INTO wallet_transactions
            (user_id, type, amount_usd, method, status, phone)
            VALUES (?,'withdrawal',?,'mpesa','pending',?)""",
            (user["id"], req.amount_usd, req.phone))
        db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
            (user["id"], "billing", "Withdrawal requested",
             f"${req.amount_usd:.2f} reserved and queued for payout to {req.phone}. "
             f"This is processed manually — you'll get a notification once it's sent."))
        return {"requested": True, "transaction_id": cur.lastrowid}

@app.post("/wallet/withdrawals/{tx_id}/approve")
def approve_withdrawal(tx_id: int, mpesa_receipt: str = "", user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    with get_db() as db:
        tx = db.execute("SELECT * FROM wallet_transactions WHERE id=? AND type='withdrawal' AND status='pending'",
                        (tx_id,)).fetchone()
        if not tx: raise HTTPException(404, "Pending withdrawal not found")
        db.execute("""UPDATE wallet_transactions SET status='completed', mpesa_receipt=?,
                      processed_at=datetime('now') WHERE id=?""", (mpesa_receipt, tx_id))
        db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
            (tx["user_id"], "billing", "Withdrawal sent ✅",
             f"${tx['amount_usd']:.2f} has been sent to {tx['phone']}." +
             (f" M-Pesa ref: {mpesa_receipt}" if mpesa_receipt else "")))
        return {"approved": True}

@app.post("/wallet/withdrawals/{tx_id}/reject")
def reject_withdrawal(tx_id: int, reason: str = "", user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    with get_db() as db:
        tx = db.execute("SELECT * FROM wallet_transactions WHERE id=? AND type='withdrawal' AND status='pending'",
                        (tx_id,)).fetchone()
        if not tx: raise HTTPException(404, "Pending withdrawal not found")
        db.execute("""UPDATE wallet_transactions SET status='rejected', admin_note=?,
                      processed_at=datetime('now') WHERE id=?""", (reason, tx_id))
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (tx["amount_usd"], tx["user_id"]))
        db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
            (tx["user_id"], "billing", "Withdrawal declined",
             f"${tx['amount_usd']:.2f} was returned to your balance. Reason: {reason or 'Not specified'}"))
        return {"rejected": True}

@app.get("/notifications")
def get_notifications(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (user["id"],)).fetchall()
        return {"notifications": [dict(r) for r in rows]}

@app.post("/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (notif_id, user["id"]))
    return {"success": True}

@app.post("/notifications/read-all")
def mark_all_notifications_read(user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    return {"success": True}

# ── Dashboard Stats ───────────────────────────────────────────────────────────
@app.get("/account/usage")
def account_usage(user=Depends(get_current_user)):
    """Current plan limits + today's usage, so the frontend can show progress
    ('3/5 signals used today') and gate buttons before the user even hits a 403."""
    plan = effective_plan(user)
    limits = plan_limits(plan)
    with get_db() as db:
        signals_today = db.execute(
            "SELECT COUNT(*) c FROM signals WHERE provider_id=? AND date(created_at)=date('now')",
            (user["id"],)).fetchone()["c"]
        copies_today = db.execute(
            "SELECT COUNT(*) c FROM copy_trades WHERE follower_id=? AND date(opened_at)=date('now')",
            (user["id"],)).fetchone()["c"]
        active_subs = db.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE follower_id=? AND is_active=1",
            (user["id"],)).fetchone()["c"]
        is_provider = db.execute(
            "SELECT id FROM providers WHERE user_id=?", (user["id"],)).fetchone() is not None
    return {
        "plan": user.get("plan", "free"), "effective_plan": plan, "limits": limits,
        "usage": {
            "signals_today": signals_today, "copies_today": copies_today,
            "active_subscriptions": active_subs,
        },
        "is_provider": is_provider,
    }

@app.get("/dashboard/stats")
def dashboard_stats(user=Depends(get_current_user)):
    with get_db() as db:
        uid = user["id"]
        subs = db.execute("SELECT COUNT(*) FROM subscriptions WHERE follower_id=? AND is_active=1",(uid,)).fetchone()[0]
        copies = db.execute("SELECT COUNT(*) FROM copy_trades WHERE follower_id=?",(uid,)).fetchone()[0]
        pnl = db.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM copy_trades WHERE follower_id=?",(uid,)).fetchone()[0]
        sig_count = db.execute("SELECT COUNT(*) FROM signals WHERE provider_id=?",(uid,)).fetchone()[0]
        notifs = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",(uid,)).fetchone()[0]
        prog = db.execute("SELECT COUNT(*) FROM user_progress WHERE user_id=? AND completed=1",(uid,)).fetchone()[0]
        return {
            "balance": user["balance"], "equity": user["equity"],
            "active_subscriptions": subs, "copy_trades": copies,
            "total_pnl_usd": round(float(pnl),2), "signals_generated": sig_count,
            "unread_notifications": notifs, "courses_completed": prog,
        }

# ── Real-time WebSocket Architecture ───────────────────────────────────────────
# Channels:
#   /ws/prices?pairs=EURUSD,GBPUSD   -> tick-level price updates, ~1.5s cadence, diffs only
#   /ws/candles?pair=EURUSD&timeframe=H1 -> live-forming candle updates + candle_closed events
#   /ws/signals                      -> broadcast of every newly generated signal (manual or auto)
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

class ConnectionManager:
    def __init__(self):
        self.channels: dict[str, set] = {"prices": set(), "signals": set(), "candles": set()}

    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        self.channels.setdefault(channel, set()).add(ws)

    def disconnect(self, channel: str, ws: WebSocket):
        self.channels.get(channel, set()).discard(ws)

    async def broadcast(self, channel: str, message: dict):
        dead = []
        for ws in list(self.channels.get(channel, set())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.channels.get(channel, set()).discard(ws)

manager = ConnectionManager()

def broadcast_threadsafe(channel: str, message: dict):
    """Call from sync (threadpool) request handlers to push a message onto a ws channel."""
    if MAIN_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(channel, message), MAIN_LOOP)
    except Exception as e:
        print(f"[WS] broadcast failed: {e}")

DEFAULT_TICK_PAIRS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","XAUUSD","BTCUSD"]

async def price_broadcaster_loop():
    """Background task: pushes fresh quotes to all /ws/prices subscribers every ~1.5s."""
    while True:
        try:
            if manager.channels.get("prices"):
                prices = [get_live_quote(p) for p in DEFAULT_TICK_PAIRS]
                await manager.broadcast("prices", {
                    "type": "prices", "data": prices, "ts": datetime.now().isoformat()
                })
        except Exception as e:
            print(f"[WS] price loop error: {e}")
        await asyncio.sleep(1.5)

@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket, pairs: str = Query(None)):
    await manager.connect("prices", websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignored pings from client
    except WebSocketDisconnect:
        manager.disconnect("prices", websocket)

@app.websocket("/ws/candles")
async def ws_candles(websocket: WebSocket, pair: str = "EURUSD", timeframe: str = "H1"):
    """Dedicated per-connection stream: live-forming candle updates + candle_closed events
    for exactly the pair/timeframe this client is viewing (no cross-talk between viewers)."""
    if pair not in PAIR_CONFIG or timeframe not in TF_MAP:
        await websocket.close(code=4400)
        return
    await websocket.accept()
    last_bar_ts: Optional[str] = None
    try:
        while True:
            try:
                df = get_ohlcv(pair, timeframe, 260)
                df = add_indicators(df)
                last_ts = str(df.index[-1])[:16]
                quote = get_live_quote(pair)
                live_price = quote.get("bid") or quote.get("price")
                row = df.iloc[-1]
                forming = {
                    "time": int(pd.Timestamp(df.index[-1]).timestamp()),
                    "open": round(float(row["open"]), 5),
                    "high": round(max(float(row["high"]), live_price), 5) if live_price else round(float(row["high"]),5),
                    "low":  round(min(float(row["low"]), live_price), 5) if live_price else round(float(row["low"]),5),
                    "close": round(float(live_price if live_price else row["close"]), 5),
                }
                closed = last_bar_ts is not None and last_bar_ts != last_ts
                last_bar_ts = last_ts

                if closed:
                    direction = "BUY" if row["ema20"] > row["ema50"] else "SELL"
                    payload = {
                        "type": "candle_closed", "pair": pair, "timeframe": timeframe,
                        "candle": forming,
                        "markers": build_markers(df, direction),
                        "support_resistance": detect_support_resistance(df),
                        "trendline": detect_trendline(df),
                    }
                else:
                    payload = {
                        "type": "candle_update", "pair": pair, "timeframe": timeframe,
                        "candle": forming, "price": live_price,
                    }
                try:
                    await websocket.send_json(payload)
                except Exception:
                    break  # client gone
            except Exception as e:
                print(f"[WS] candle loop error ({pair} {timeframe}): {e}")
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass

@app.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    await manager.connect("signals", websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect("signals", websocket)

AUTO_SIGNAL_ROTATION = [("EURUSD","H1"), ("GBPUSD","M30"), ("XAUUSD","H1"),
                         ("USDJPY","M15"), ("BTCUSD","H1"), ("GBPJPY","H4")]

async def auto_signal_loop():
    """Generates a fresh AI signal on rotation and broadcasts it, so /ws/signals stays live
    even without a user manually clicking 'generate' — new candles => new signal checks."""
    i = 0
    while True:
        await asyncio.sleep(40)
        try:
            if not manager.channels.get("signals"):
                continue
            pair, tf = AUTO_SIGNAL_ROTATION[i % len(AUTO_SIGNAL_ROTATION)]
            i += 1
            df = get_ohlcv(pair, tf, 260)
            df = add_indicators(df)
            sig = build_signal(pair, tf, df, provider_id=None)
            if sig["confidence"] < 60:
                continue  # only surface actionable auto-signals
            ohlcv = sig.pop("ohlcv", None)
            chart_data = {"ohlcv": ohlcv, "markers": sig.get("markers", []),
                          "support_resistance": sig.get("support_resistance", []),
                          "trendline": sig.get("trendline")}
            with get_db() as db:
                cur = db.execute("""
                    INSERT INTO signals (provider_id,pair,timeframe,direction,strength,confidence,
                    entry_price,stop_loss,take_profit,sl_pips,tp_pips,risk_reward,rsi,macd,
                    ema20,ema50,bb_upper,bb_lower,stoch_k,atr,candle_pattern,chart_pattern,
                    entry_time,ai_analysis,expires_at,chart_data)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (None, sig["pair"], sig["timeframe"], sig["direction"], sig["strength"],
                      sig["confidence"], sig["entry_price"], sig["stop_loss"], sig["take_profit"],
                      sig["sl_pips"], sig["tp_pips"], sig["risk_reward"], sig["rsi"], sig["macd"],
                      sig["ema20"], sig["ema50"], sig["bb_upper"], sig["bb_lower"], sig["stoch_k"],
                      sig["atr"], sig["candle_pattern"], sig["chart_pattern"], sig["entry_time"],
                      sig["ai_analysis"], sig["expires_at"], json.dumps(chart_data)))
                sig["id"] = cur.lastrowid
            sig["ohlcv"] = ohlcv
            sig["provider_name"] = "ForexPro AI"
            await manager.broadcast("signals", {"type": "new_signal", "data": sig})
        except Exception as e:
            print(f"[AutoSignal] error: {e}")

async def settlement_loop():
    """Watches every 'active' signal against live prices and closes it (+ every
    copy_trade riding on it) the moment price hits TP, SL, or the signal expires.
    This is what makes win-rates, pips, and copy-trade P&L actually move instead
    of sitting at zero forever."""
    while True:
        await asyncio.sleep(20)
        try:
            settle_once()
        except Exception as e:
            print(f"[Settlement] error: {e}")

def settle_once():
    """One settlement pass — pulled out of the loop so it can be unit-tested directly."""
    with get_db() as db:
        active = db.execute("SELECT * FROM signals WHERE status='active'").fetchall()
        for sig in active:
            try:
                quote = get_live_quote(sig["pair"])
                price = float(quote["price"])
            except Exception:
                continue

            _, _, pip, _, _ = PAIR_CONFIG.get(sig["pair"], PAIR_CONFIG["EURUSD"])
            result, close_price = None, None
            is_buy = sig["direction"] == "BUY"

            hit_tp = (price >= sig["take_profit"]) if is_buy else (price <= sig["take_profit"])
            hit_sl = (price <= sig["stop_loss"]) if is_buy else (price >= sig["stop_loss"])
            expired = sig["expires_at"] and str(sig["expires_at"]) < datetime.now().isoformat()

            if hit_tp:
                result, close_price = "win", sig["take_profit"]
            elif hit_sl:
                result, close_price = "loss", sig["stop_loss"]
            elif expired:
                diff_pips = (price - sig["entry_price"]) / pip * (1 if is_buy else -1)
                result = "win" if diff_pips > 1 else ("loss" if diff_pips < -1 else "breakeven")
                close_price = price

            if not result:
                continue

            pnl_pips = round((close_price - sig["entry_price"]) / pip * (1 if is_buy else -1), 1)
            db.execute("""UPDATE signals SET status='closed', result=?, pnl_pips=?,
                          close_price=?, closed_at=datetime('now') WHERE id=?""",
                       (result, pnl_pips, close_price, sig["id"]))

            trades = db.execute(
                "SELECT * FROM copy_trades WHERE signal_id=? AND status IN ('pending','open') AND execution_mode != 'mt5'",
                (sig["id"],)).fetchall()
            for t in trades:
                pnl_usd = pip_value_usd(sig["pair"], pnl_pips, t["lot_size"])
                db.execute("""UPDATE copy_trades SET status='closed', result=?, pnl_pips=?,
                              pnl_usd=?, closed_at=datetime('now') WHERE id=?""",
                           (result, pnl_pips, pnl_usd, t["id"]))
                # Release the reserved margin back and apply realized P&L — this is
                # what actually moves the user's balance for a copy trade.
                db.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                           (float(t["margin_used"] or 0) + pnl_usd, t["follower_id"]))
                # Auto-log into the trade journal so followers get a unified record
                # of manual entries + copy trades without doing anything extra.
                db.execute("""INSERT INTO trade_journal
                    (user_id,pair,direction,entry_price,exit_price,lot_size,pnl_usd,pnl_pips,notes,setup)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (t["follower_id"], sig["pair"], sig["direction"], t["entry_price"], close_price,
                     t["lot_size"], pnl_usd, pnl_pips, "Auto-logged from copy trade", "Auto (Copy Trade)"))
                db.execute("""INSERT INTO notifications (user_id,type,title,message)
                              VALUES (?,?,?,?)""",
                           (t["follower_id"], "trade_closed",
                            f"{sig['pair']} {result.upper()}",
                            f"Closed at {close_price} · {pnl_pips:+.1f} pips · ${pnl_usd:+.2f}"))

            if sig["provider_id"]:
                recompute_provider_stats(db, sig["provider_id"])

            broadcast_threadsafe("signals", {"type": "signal_closed", "data": {
                "id": sig["id"], "pair": sig["pair"], "result": result,
                "pnl_pips": pnl_pips, "close_price": close_price,
                "copy_trades_settled": len(trades),
            }})

@app.get("/")
def root():
    return {"api": "ForexPro v4.0", "status": "running",
            "features": ["signals","copy-trading","education","journal",
                         "live-prices-ws","live-candles-ws","live-signals-ws",
                         "mpesa-payments","stripe-payments"],
            "docs": "/docs", "db": "SQLite (forexpro.db)"}
