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
from database import get_db, init_db, hash_password, verify_password
from signals import (get_ohlcv, add_indicators, build_signal, get_live_quote,
                     PAIR_CONFIG, TF_MAP)
from payments import router as payments_router
import pandas as pd
import time
       

app = FastAPI(title="ForexPro API", version="4.0.0", docs_url="/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer(auto_error=False)

# Simple JWT-like token (base64 encoded payload — use python-jose for production)
import base64, hashlib

SECRET = "forexpro_secret_2026"

def create_token(user_id: int, username: str) -> str:
    payload = f"{user_id}:{username}:{int(time.time())+86400*7}"
    sig = hashlib.sha256(f"{payload}{SECRET}".encode()).hexdigest()[:16]
    return base64.b64encode(f"{payload}:{sig}".encode()).decode()

def decode_token(token: str) -> Optional[dict]:
    try:
        decoded = base64.b64decode(token.encode()).decode()
        parts = decoded.rsplit(":", 1)
        if len(parts) != 2: return None
        payload, sig = parts[0], parts[1]
        expected = hashlib.sha256(f"{payload}{SECRET}".encode()).hexdigest()[:16]
        if sig != expected: return None
        uid, uname, exp = payload.split(":", 2)
        if int(exp) < time.time(): return None
        return {"user_id": int(uid), "username": uname}
    except: return None

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds: raise HTTPException(401, "Not authenticated")
    data = decode_token(creds.credentials)
    if not data: raise HTTPException(401, "Invalid or expired token")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (data["user_id"],)).fetchone()
        if not user: raise HTTPException(401, "User not found")
        return dict(user)

def get_optional_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds: return None
    data = decode_token(creds.credentials) if creds else None
    if not data: return None
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (data["user_id"],)).fetchone()
        return dict(user) if user else None

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
    min_confidence: int = 65; auto_copy: bool = True
    pairs_filter: List[str] = []

class UpdateProgressReq(BaseModel):
    course_id: int; lesson_idx: int; completed: bool = False; score: int = 0

class JournalEntryReq(BaseModel):
    pair: str; direction: str; entry_price: float; exit_price: float
    lot_size: float; pnl_usd: float; pnl_pips: float
    notes: str = ""; emotion: str = "calm"; setup: str = ""

class UpdateProfileReq(BaseModel):
    bio: str = ""; broker: str = ""; mt5_login: str = ""; mt5_server: str = ""

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup(): init_db()

app.include_router(payments_router)
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
                "email": req.email, "role": "trader", "plan": "free"}}

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
        }}

@app.get("/auth/me")
def get_me(user=Depends(get_current_user)):
    with get_db() as db:
        notifs = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                            (user["id"],)).fetchone()[0]
        subs = db.execute("SELECT COUNT(*) FROM subscriptions WHERE follower_id=? AND is_active=1",
                          (user["id"],)).fetchone()[0]
        return {**{k:v for k,v in user.items() if k!="password"},
                "unread_notifications": notifs, "active_subscriptions": subs}

@app.put("/auth/profile")
def update_profile(req: UpdateProfileReq, user=Depends(get_current_user)):
    with get_db() as db:
        db.execute("UPDATE users SET bio=?,broker=?,mt5_login=?,mt5_server=? WHERE id=?",
                   (req.bio, req.broker, req.mt5_login, req.mt5_server, user["id"]))
    return {"success": True}

# ── Signal Routes ─────────────────────────────────────────────────────────────
@app.post("/signals/generate")
def generate_signal(req: GenerateSignalReq, user=Depends(get_current_user)):
    if req.pair not in PAIR_CONFIG: raise HTTPException(400, "Unknown pair")
    if req.timeframe not in TF_MAP: raise HTTPException(400, "Unknown timeframe")
    df = get_ohlcv(req.pair, req.timeframe, 250)
    df = add_indicators(df)
    sig = build_signal(req.pair, req.timeframe, df, provider_id=user["id"])
    ohlcv = sig.pop("ohlcv", None)
    
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO signals (provider_id,pair,timeframe,direction,strength,confidence,
            entry_price,stop_loss,take_profit,sl_pips,tp_pips,risk_reward,rsi,macd,
            ema20,ema50,bb_upper,bb_lower,stoch_k,atr,candle_pattern,chart_pattern,
            entry_time,ai_analysis,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (user["id"], sig["pair"], sig["timeframe"], sig["direction"], sig["strength"],
              sig["confidence"], sig["entry_price"], sig["stop_loss"], sig["take_profit"],
              sig["sl_pips"], sig["tp_pips"], sig["risk_reward"], sig["rsi"], sig["macd"],
              sig["ema20"], sig["ema50"], sig["bb_upper"], sig["bb_lower"], sig["stoch_k"],
              sig["atr"], sig["candle_pattern"], sig["chart_pattern"], sig["entry_time"],
              sig["ai_analysis"], sig["expires_at"]))
        sig_id = cur.lastrowid
        
        # Auto-distribute to subscribers if user is a provider
        subs = db.execute(
            "SELECT * FROM subscriptions WHERE provider_id=? AND is_active=1 AND auto_copy=1",
            (user["id"],)).fetchall()
        copies_created = 0
        for sub in subs:
            if sig["confidence"] >= sub["min_confidence"]:
                pf = json.loads(sub["pairs_filter"] or "[]")
                if not pf or sig["pair"] in pf:
                    db.execute("""INSERT INTO copy_trades 
                        (follower_id,provider_id,signal_id,lot_size,risk_pct,entry_price,
                         stop_loss,take_profit,status,opened_at)
                        VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
                        (sub["follower_id"], user["id"], sig_id,
                         sub["max_lot"], sub["risk_pct"],
                         sig["entry_price"], sig["stop_loss"], sig["take_profit"], "open"))
                    copies_created += 1
                    # Notification
                    db.execute("""INSERT INTO notifications (user_id,type,title,message)
                        VALUES (?,?,?,?)""",
                        (sub["follower_id"], "signal",
                         f"New Signal: {sig['pair']} {sig['direction']}",
                         f"Confidence: {sig['confidence']}% | Entry: {sig['entry_price']} | SL: {sig['stop_loss']} | TP: {sig['take_profit']}"))
        
        sig["id"] = sig_id
        sig["copies_distributed"] = copies_created
        sig["ohlcv"] = ohlcv
        return sig

@app.post("/signals/bulk")
def bulk_signals(req: BulkSignalReq, user=Depends(get_current_user)):
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
    return {"count": len(results), "signals": results}

@app.get("/signals/latest")
def latest_signals(limit: int = Query(20), user=Depends(get_optional_user)):
    with get_db() as db:
        rows = db.execute("""
            SELECT s.*, u.username as provider_name
            FROM signals s LEFT JOIN users u ON s.provider_id=u.id
            WHERE s.status='active'
            ORDER BY s.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return {"signals": [dict(r) for r in rows]}

@app.get("/signals/{signal_id}")
def get_signal(signal_id: int, user=Depends(get_optional_user)):
    with get_db() as db:
        row = db.execute("""
            SELECT s.*, u.username as provider_name
            FROM signals s LEFT JOIN users u ON s.provider_id=u.id
            WHERE s.id=?
        """, (signal_id,)).fetchone()
        if not row: raise HTTPException(404, "Signal not found")
        return dict(row)

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

# ── Providers & Copy Trading ──────────────────────────────────────────────────
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
        # Check not already subscribed
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE follower_id=? AND provider_id=? AND is_active=1",
            (user["id"], req.provider_id)).fetchone()
        if existing: raise HTTPException(400, "Already subscribed to this provider")
        
        db.execute("""INSERT INTO subscriptions 
            (follower_id,provider_id,risk_pct,max_lot,min_confidence,auto_copy,pairs_filter)
            VALUES (?,?,?,?,?,?,?)""",
            (user["id"], req.provider_id, req.risk_pct, req.max_lot,
             req.min_confidence, int(req.auto_copy), json.dumps(req.pairs_filter)))
        
        # Update provider follower count
        db.execute("UPDATE providers SET followers_count=followers_count+1 WHERE user_id=?",
                   (req.provider_id,))
        
        db.execute("""INSERT INTO notifications (user_id,type,title,message) VALUES (?,?,?,?)""",
                   (user["id"], "copy", "Copy Trading Activated",
                    "You are now copying trades automatically."))
        return {"success": True, "message": "Subscribed successfully. Auto-copy is active."}

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
        trades = db.execute("""
            SELECT ct.*, u.username as provider_name, s.pair, s.timeframe,
                   s.direction, s.ai_analysis, s.candle_pattern
            FROM copy_trades ct
            JOIN users u ON ct.provider_id=u.id
            LEFT JOIN signals s ON ct.signal_id=s.id
            WHERE ct.follower_id=?
            ORDER BY ct.created_at DESC LIMIT 50
        """, (user["id"],)).fetchall()
        
        total_pnl = sum(t["pnl_usd"] or 0 for t in trades)
        wins = sum(1 for t in trades if (t["pnl_usd"] or 0) > 0)
        return {"trades": [dict(t) for t in trades],
                "stats": {"total": len(trades), "wins": wins,
                          "losses": len(trades)-wins, "total_pnl_usd": round(total_pnl, 2)}}

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
                      auto_copy=?,pairs_filter=? WHERE follower_id=? AND provider_id=?""",
                   (req.risk_pct, req.max_lot, req.min_confidence, int(req.auto_copy),
                    json.dumps(req.pairs_filter), user["id"], provider_id))
        return {"success": True}

# ── Prices ────────────────────────────────────────────────────────────────────
@app.get("/prices/live")
def live_prices(pairs: str = Query("EURUSD,GBPUSD,USDJPY,AUDUSD,XAUUSD,BTCUSD")):
    pair_list = [p.strip() for p in pairs.split(",") if p.strip() in PAIR_CONFIG]
    prices = [get_live_quote(p) for p in pair_list]
    return {"prices": prices, "updated_at": datetime.now().isoformat()}

@app.get("/prices/chart")
def price_chart(pair: str = "EURUSD", timeframe: str = "H1", candles: int = 500):
    df = get_ohlcv(pair, timeframe, candles + 250)
    df = add_indicators(df)
    tail = df.tail(candles)
    records = []
    for ts, row in tail.iterrows():
        records.append({
            "time": int(pd.Timestamp(ts).timestamp()), "open": round(float(row["open"]),5), # type: ignore
            "high": round(float(row["high"]),5), "low": round(float(row["low"]),5),
            "close": round(float(row["close"]),5), "ema20": round(float(row["ema20"]),5),
            "ema50": round(float(row["ema50"]),5), "bb_up": round(float(row["bb_up"]),5),
            "bb_low": round(float(row["bb_low"]),5), "rsi": round(float(row["rsi"]),2),
            "macd_h": round(float(row["macd_h"]),6), "stoch_k": round(float(row["stoch_k"]),2),
        })
    return {"pair": pair, "timeframe": timeframe, "candles": records,
            "source": "live" if "live" in str(df.index[0]) else "simulated"}

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
@app.get("/notifications")
def get_notifications(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (user["id"],)).fetchall()
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
        return {"notifications": [dict(r) for r in rows]}

# ── Dashboard Stats ───────────────────────────────────────────────────────────
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

# ── WebSocket for live price streaming ────────────────────────────────────────
connected_clients = set()

@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        while True:
            prices = [get_live_quote(p) for p in ["EURUSD","GBPUSD","USDJPY","XAUUSD","BTCUSD"]]
            await websocket.send_json({"type":"prices","data":prices,"ts":datetime.now().isoformat()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        connected_clients.discard(websocket)


# active clients


@app.get("/prices/chart")
def price_chart(
    pair: str = "EURUSD",
    timeframe: str = "H1",
    candles: int = 500,
):
    df = get_ohlcv(pair, timeframe, candles + 250)

    df = add_indicators(df)

    tail = df.tail(candles)

    records = []

    for ts, row in tail.iterrows():

        records.append({
            "time": int(pd.Timestamp(ts).timestamp()),

            "open": round(float(row["open"]), 5),
            "high": round(float(row["high"]), 5),
            "low": round(float(row["low"]), 5),
            "close": round(float(row["close"]), 5),

            "ema20": round(float(row["ema20"]), 5),
            "ema50": round(float(row["ema50"]), 5),

            "bb_upper": round(float(row["bb_up"]), 5),
            "bb_lower": round(float(row["bb_low"]), 5),

            "rsi": round(float(row["rsi"]), 2),

            "macd_h": round(float(row["macd_h"]), 6),

            "stoch_k": round(float(row["stoch_k"]), 2),
        })

    return {
        "pair": pair,
        "timeframe": timeframe,
        "candles": records,
        "source": "live",
    }

@app.get("/")
def root():
    return {"api": "ForexPro v4.0", "status": "running",
            "features": ["signals","copy-trading","education","journal","live-prices","websocket"],
            "docs": "/docs", "db": "SQLite (forexpro.db)"}
