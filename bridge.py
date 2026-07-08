"""
MT5 Bridge — connects a real MetaTrader 5 terminal to ForexPro.

How it works:
  1. A user generates a "bridge token" from their Profile page (a random
     secret, separate from their normal login — an EA can't do an
     interactive JWT login flow).
  2. They install the ForexProEA.mq5 (download it from /bridge/ea/download)
     in their MT5 terminal, paste in the backend URL + bridge token.
  3. The EA polls GET /bridge/pending-orders every few seconds. Any
     copy_trades marked execution_mode='mt5' and status='pending_bridge'
     come back as simple pipe-delimited lines (MQL5 has no JSON library,
     so we deliberately keep this wire format trivial to parse with
     StringSplit rather than shipping a JSON parser inside the EA).
  4. The EA places the real order in MT5, then calls
     POST /bridge/report-fill with the real ticket + fill price.
  5. When MT5 closes the position (TP/SL/manual), the EA calls
     POST /bridge/report-close with the real P&L — this is authoritative
     and is NOT touched by the simulated settlement engine in
     forexpro_main.py (which explicitly skips execution_mode='mt5' trades).

Everything here uses simple query-string parameters (not JSON bodies) on
purpose — building a URL with query params is far easier from MQL5 than
constructing/parsing JSON.
"""
import os
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from database import get_db, generate_bridge_token, recompute_provider_stats
from auth import get_current_user

router = APIRouter(prefix="/bridge", tags=["mt5-bridge"])

HEARTBEAT_STALE_SECONDS = 90


def get_bridge_user(token: str = Query(..., description="Per-user bridge token from Profile")):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE bridge_token=?", (token,)).fetchone()
        if not user:
            raise HTTPException(401, "Invalid or unknown bridge token")
        return dict(user)


# ── Setup (JWT-authenticated — called from the web app, not the EA) ──────────
@router.post("/token/generate")
def generate_token(user=Depends(get_current_user)):
    token = generate_bridge_token()
    with get_db() as db:
        db.execute("UPDATE users SET bridge_token=? WHERE id=?", (token, user["id"]))
    return {"bridge_token": token}


@router.get("/status")
def bridge_status(user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute(
            "SELECT bridge_token, bridge_connected_at FROM users WHERE id=?",
            (user["id"],)).fetchone()
    connected = False
    if row and row["bridge_connected_at"]:
        try:
            connected = datetime.fromisoformat(row["bridge_connected_at"]) > \
                datetime.now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
        except Exception:
            connected = False
    return {
        "has_token": bool(row and row["bridge_token"]),
        "bridge_token": row["bridge_token"] if row else None,
        "connected": connected,
        "last_seen": row["bridge_connected_at"] if row else None,
    }


@router.get("/ea/download")
def download_ea():
    """Serves the ForexPro Expert Advisor source (.mq5) — compile it in MetaEditor
    (F7) inside your MT5 terminal, then attach it to any chart."""
    path = os.path.join(os.path.dirname(__file__), "mt5_ea", "ForexProEA.mq5")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        raise HTTPException(404, "EA source not found on server")
    return Response(
        content=content, media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=ForexProEA.mq5"},
    )


# ── EA-facing endpoints (bridge-token authenticated) ─────────────────────────
@router.get("/pending-orders", response_class=PlainTextResponse)
def pending_orders(user=Depends(get_bridge_user)):
    with get_db() as db:
        db.execute("UPDATE users SET bridge_connected_at=datetime('now') WHERE id=?", (user["id"],))
        rows = db.execute("""
            SELECT ct.id, s.pair, s.direction, ct.lot_size, ct.entry_price, ct.stop_loss, ct.take_profit
            FROM copy_trades ct JOIN signals s ON ct.signal_id = s.id
            WHERE ct.follower_id=? AND ct.execution_mode='mt5' AND ct.status='pending_bridge'
            ORDER BY ct.id ASC
        """, (user["id"],)).fetchall()
        lines = [
            f"{r['id']}|{r['pair']}|{r['direction']}|{r['lot_size']}|{r['entry_price']}|{r['stop_loss']}|{r['take_profit']}"
            for r in rows
        ]
        if rows:
            ids = [r["id"] for r in rows]
            db.execute(
                f"UPDATE copy_trades SET status='sent_to_bridge' WHERE id IN ({','.join('?' * len(ids))})",
                ids)
    return "\n".join(lines)


@router.post("/heartbeat")
def heartbeat(user=Depends(get_bridge_user)):
    with get_db() as db:
        db.execute("UPDATE users SET bridge_connected_at=datetime('now') WHERE id=?", (user["id"],))
    return {"ok": True}


@router.post("/report-fill")
def report_fill(
    copy_trade_id: int, status: str, ticket: str = "",
    fill_price: float = 0, error_msg: str = "",
    user=Depends(get_bridge_user),
):
    with get_db() as db:
        ct = db.execute("SELECT * FROM copy_trades WHERE id=? AND follower_id=?",
                         (copy_trade_id, user["id"])).fetchone()
        if not ct:
            raise HTTPException(404, "Copy trade not found")
        if status == "filled":
            db.execute("""UPDATE copy_trades SET status='open', mt5_ticket=?, entry_price=?
                          WHERE id=?""", (ticket, fill_price or ct["entry_price"], copy_trade_id))
        else:
            db.execute("""UPDATE copy_trades SET status='failed', fail_reason=? WHERE id=?""",
                       (error_msg or "EA reported failure", copy_trade_id))
            # The order never actually opened on the broker — give the reserved margin back.
            if ct["margin_used"]:
                db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (ct["margin_used"], user["id"]))
    return {"ok": True}


@router.post("/report-close")
def report_close(
    copy_trade_id: int, close_price: float, pnl_usd: float, pnl_pips: float,
    result: str, ticket: str = "",
    user=Depends(get_bridge_user),
):
    with get_db() as db:
        ct = db.execute("SELECT * FROM copy_trades WHERE id=? AND follower_id=?",
                         (copy_trade_id, user["id"])).fetchone()
        if not ct:
            raise HTTPException(404, "Copy trade not found")
        db.execute("""UPDATE copy_trades SET status='closed', result=?, pnl_pips=?, pnl_usd=?,
                      close_price=?, mt5_ticket=COALESCE(NULLIF(?,''), mt5_ticket),
                      closed_at=datetime('now') WHERE id=?""",
                   (result, pnl_pips, pnl_usd, close_price, ticket, copy_trade_id))
        # Real MT5 P&L is authoritative here — release the reserved margin and apply it.
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                   (float(ct["margin_used"] or 0) + pnl_usd, user["id"]))
        db.execute("""INSERT INTO trade_journal
            (user_id,pair,direction,entry_price,exit_price,lot_size,pnl_usd,pnl_pips,notes,setup)
            SELECT ?, s.pair, s.direction, ?, ?, ?, ?, ?, 'Auto-logged from MT5 copy trade', 'Auto (MT5 Copy Trade)'
            FROM signals s WHERE s.id=?""",
            (user["id"], ct["entry_price"], close_price, ct["lot_size"], pnl_usd, pnl_pips, ct["signal_id"]))
        db.execute("""INSERT INTO notifications (user_id,type,title,message)
                      VALUES (?,?,?,?)""",
                   (user["id"], "trade_closed", f"MT5 trade {result.upper()}",
                    f"Closed at {close_price} · {pnl_pips:+.1f} pips · ${pnl_usd:+.2f} (real account)"))
        if ct["provider_id"]:
            recompute_provider_stats(db, ct["provider_id"])
    return {"ok": True}
