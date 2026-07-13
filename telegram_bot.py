"""
ForexPro Telegram Bot
─────────────────────
Sends signal alerts to Telegram when new signals are generated.
Users can subscribe/unsubscribe, set filters, and get live prices.

SETUP:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Set TELEGRAM_BOT_TOKEN below (or in .env)
  3. pip install python-telegram-bot==20.7 aiohttp
  4. python telegram_bot.py

FEATURES:
  /start          — Welcome + subscribe
  /signals        — Latest signals from API
  /prices         — Live price quotes
  /subscribe      — Enable auto-alerts
  /unsubscribe    — Disable alerts
  /filter EURUSD H1 — Set pair + TF filter
  /settings       — View your settings
  /help           — Command list

Integration with main API:
  - Polls /signals/latest every 60s
  - New signals → sends formatted message to all subscribers
  - Connects as provider user (uses API auth token)
"""

import asyncio
import aiohttp
import json
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)

# ── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
API_BASE           = os.getenv("API_BASE", "http://localhost:8766")
API_EMAIL          = os.getenv("API_EMAIL", "provider@forexpro.com")
API_PASSWORD       = os.getenv("API_PASSWORD", "demo123")
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "60"))  # seconds

# ── In-memory subscriber store (use DB for production) ────────────────────────
subscribers: dict[int, dict] = {}   # chat_id → {token, filter_pair, filter_tf, active}
api_token   = ""
last_sig_id = ""

# ── API helpers ───────────────────────────────────────────────────────────────
async def api_login() -> str:
    async with aiohttp.ClientSession() as s:
        r = await s.post(f"{API_BASE}/auth/login",
                         json={"email": API_EMAIL, "password": API_PASSWORD})
        d = await r.json()
        return d.get("token", "")

async def api_get(path: str, token: str = "") -> dict:
    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"{API_BASE}{path}", headers=hdrs)
        return await r.json()

# ── Signal formatter ──────────────────────────────────────────────────────────
def format_signal(s: dict) -> str:
    buy   = s["direction"] == "BUY"
    arrow = "🟢" if buy else "🔴"
    str_emoji = {"STRONG": "💪", "MODERATE": "👍", "WEAK": "⚠️", "AVOID": "🚫"}.get(s.get("strength", ""), "")
    rsi  = s.get("rsi", 0)
    rsi_note = " (Oversold)" if rsi < 30 else " (Overbought)" if rsi > 70 else ""

    return (
        f"{arrow} *{s['pair']}  {s['direction']}* {str_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Timeframe:     `{s['timeframe']}`\n"
        f"🎯 Confidence:    `{s['confidence']}%`\n"
        f"💰 Entry:         `{float(s['entry_price']):.5f}`\n"
        f"🛑 Stop Loss:     `{float(s['stop_loss']):.5f}`  _{float(s['sl_pips']):.1f} pips_\n"
        f"✅ Take Profit:   `{float(s['take_profit']):.5f}`  _{float(s['tp_pips']):.1f} pips_\n"
        f"⚖️  Risk:Reward:  `1:{s['risk_reward']}`\n"
        f"📊 RSI:           `{rsi:.1f}`{rsi_note}\n"
        f"🕯 Pattern:       `{s.get('candle_pattern', '—')}`\n"
        f"🕐 Best Entry:    `{s.get('entry_time', '—')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 _{s.get('ai_analysis', '')[:120]}…_"
    )

def signal_keyboard(s: dict):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📈 View Chart",    callback_data=f"chart_{s['pair']}_{s['timeframe']}"),
        InlineKeyboardButton("🔕 Unsubscribe",   callback_data="unsubscribe"),
    ]])

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    subscribers[cid] = {"filter_pair": "", "filter_tf": "", "active": True}
    await update.message.reply_text(
        "👋 *Welcome to ForexPro Bot!*\n\n"
        "You'll now receive live forex signal alerts.\n\n"
        "Commands:\n"
        "• /signals — Latest signals\n"
        "• /prices — Live prices\n"
        "• /filter EURUSD H1 — Filter alerts\n"
        "• /settings — Your settings\n"
        "• /unsubscribe — Stop alerts\n"
        "• /help — All commands\n\n"
        "⚠️ _For educational purposes only. Not financial advice._",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*ForexPro Bot Commands*\n\n"
        "/start — Subscribe to alerts\n"
        "/signals — Last 5 signals\n"
        "/signals EURUSD — Signals for a pair\n"
        "/prices — Live price quotes\n"
        "/filter EURUSD H1 — Set pair + TF filter\n"
        "/filter clear — Remove all filters\n"
        "/settings — Your subscription settings\n"
        "/subscribe — Re-enable alerts\n"
        "/unsubscribe — Pause alerts\n"
        "/help — This message",
        parse_mode="Markdown"
    )

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args  = ctx.args
    limit = 5
    path  = f"/signals/latest?limit={limit}"

    await update.message.reply_text("⏳ Fetching latest signals…")

    try:
        data = await api_get(path, api_token)
        sigs = data.get("signals", [])

        # Filter by pair if arg given
        if args:
            sigs = [s for s in sigs if s["pair"].upper() == args[0].upper()]

        if not sigs:
            await update.message.reply_text("No active signals found right now.")
            return

        for s in sigs[:5]:
            await update.message.reply_text(
                format_signal(s),
                parse_mode="Markdown",
                reply_markup=signal_keyboard(s)
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        data   = await api_get("/prices/live?pairs=EURUSD,GBPUSD,USDJPY,XAUUSD,BTCUSD,AUDUSD,GBPJPY")
        prices = data.get("prices", [])
        lines  = ["*Live Forex Prices*\n━━━━━━━━━━━━━━━"]
        for p in prices:
            arr = "🟢" if p["direction"] == "up" else "🔴"
            chg = float(p.get("change_pct", 0))
            prc = float(p["price"])
            fmt = f"{prc:.2f}" if p["pair"] == "BTCUSD" else f"{prc:.5f}"
            lines.append(f"{arr} `{p['pair']:8}` `{fmt}`  `{chg:+.3f}%`")
        lines.append(f"\n_Updated: {datetime.now().strftime('%H:%M:%S')}_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    args = ctx.args

    if not args or args[0].lower() == "clear":
        if cid in subscribers:
            subscribers[cid]["filter_pair"] = ""
            subscribers[cid]["filter_tf"]   = ""
        await update.message.reply_text("✅ Filters cleared — you'll receive all signals.")
        return

    pair = args[0].upper() if args else ""
    tf   = args[1].upper() if len(args) > 1 else ""

    valid_pairs = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","XAUUSD","GBPJPY","EURJPY","BTCUSD"]
    valid_tfs   = ["M15","M30","H1","H4","D1","W1"]

    if pair and pair not in valid_pairs:
        await update.message.reply_text(f"❌ Unknown pair `{pair}`\nValid: {', '.join(valid_pairs)}", parse_mode="Markdown")
        return
    if tf and tf not in valid_tfs:
        await update.message.reply_text(f"❌ Unknown timeframe `{tf}`\nValid: {', '.join(valid_tfs)}", parse_mode="Markdown")
        return

    if cid not in subscribers:
        subscribers[cid] = {"active": True}
    subscribers[cid]["filter_pair"] = pair
    subscribers[cid]["filter_tf"]   = tf

    msg = f"✅ Filter set: `{pair or 'ALL PAIRS'}` `{tf or 'ALL TIMEFRAMES'}`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in subscribers:
        await update.message.reply_text("You're not subscribed. Send /start to subscribe.")
        return
    sub = subscribers[cid]
    await update.message.reply_text(
        f"*Your Settings*\n\n"
        f"Status:    `{'Active ✅' if sub.get('active') else 'Paused ⏸'}`\n"
        f"Pair:      `{sub.get('filter_pair') or 'All pairs'}`\n"
        f"Timeframe: `{sub.get('filter_tf') or 'All timeframes'}`\n\n"
        f"Use /filter to change · /unsubscribe to pause",
        parse_mode="Markdown"
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in subscribers:
        subscribers[cid] = {"filter_pair": "", "filter_tf": "", "active": True}
    else:
        subscribers[cid]["active"] = True
    await update.message.reply_text("✅ Alerts re-enabled! You'll receive new signals.")

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in subscribers:
        subscribers[cid]["active"] = False
    await update.message.reply_text("⏸ Alerts paused. Send /subscribe to re-enable.")

# ── Callback query handler ────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data.startswith("chart_"):
        parts = data.split("_")
        pair  = parts[1] if len(parts) > 1 else "EURUSD"
        tf    = parts[2] if len(parts) > 2 else "H1"
        await q.message.reply_text(
            f"📊 *{pair} {tf} Chart*\n\nOpen MT5 or TradingView to view the live chart.\n"
            f"Symbol: `{pair}` | Timeframe: `{tf}`",
            parse_mode="Markdown"
        )
    elif data == "unsubscribe":
        cid = q.message.chat.id
        if cid in subscribers:
            subscribers[cid]["active"] = False
        await q.message.reply_text("⏸ Alerts paused. /subscribe to re-enable.")

# ── Background job: poll for new signals ─────────────────────────────────────
async def poll_and_alert(ctx: ContextTypes.DEFAULT_TYPE):
    global last_sig_id, api_token

    # Refresh token if empty
    if not api_token:
        api_token = await api_login()
        if not api_token:
            print("Bot: Failed to get API token")
            return

    try:
        data = await api_get("/signals/latest?limit=5", api_token)
        sigs = data.get("signals", [])
        if not sigs:
            return

        newest = sigs[0]
        sig_id = str(newest.get("id", ""))

        if sig_id == last_sig_id:
            return  # No new signals

        last_sig_id = sig_id
        print(f"Bot: New signal {newest['pair']} {newest['direction']} conf={newest['confidence']}%")

        # Send to all active subscribers
        for cid, sub in subscribers.items():
            if not sub.get("active", True):
                continue

            # Apply filters
            fp = sub.get("filter_pair", "")
            ft = sub.get("filter_tf", "")
            if fp and newest["pair"] != fp:
                continue
            if ft and newest["timeframe"] != ft:
                continue

            try:
                await ctx.bot.send_message(
                    chat_id=cid,
                    text=format_signal(newest),
                    parse_mode="Markdown",
                    reply_markup=signal_keyboard(newest)
                )
                print(f"Bot: Alert sent to {cid}")
            except Exception as e:
                print(f"Bot: Failed to send to {cid}: {e}")

    except Exception as e:
        print(f"Bot poll error: {e}")
        api_token = ""  # Force re-login next time

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set TELEGRAM_BOT_TOKEN in environment or in this file")
        print("   Get token from @BotFather on Telegram")
        return

    print(f"🤖 ForexPro Bot starting…")
    print(f"   API: {API_BASE}")
    print(f"   Poll: every {POLL_INTERVAL}s")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("signals",     cmd_signals))
    app.add_handler(CommandHandler("prices",      cmd_prices))
    app.add_handler(CommandHandler("filter",      cmd_filter))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Background signal polling job
    app.job_queue.run_repeating(poll_and_alert, interval=POLL_INTERVAL, first=10)

    print("✅ Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()