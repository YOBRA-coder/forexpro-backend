"""
ForexPro Email Alert System
────────────────────────────
Sends beautiful HTML signal emails via SMTP (Gmail / any provider).

SETUP:
  pip install aiosmtplib jinja2

  For Gmail:
    1. Enable 2FA on your Google account
    2. Go to myaccount.google.com → Security → App Passwords
    3. Generate an App Password for "Mail"
    4. Use that 16-char password as SMTP_PASSWORD

  For other providers: change SMTP_HOST / SMTP_PORT

  Set environment variables or edit the Config class below.

USAGE (standalone):
  python email_alerts.py                # test email to yourself

USAGE (integrated with main API):
  Add to forexpro_main.py:
    from email_alerts import send_signal_email, EmailAlert
    # After signal is generated and saved to DB:
    await send_signal_email(user_email, signal_data)

  Or run as a background service polling new signals:
    python email_alerts.py --watch
"""

import asyncio
import aiosmtplib
import aiohttp
import json
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dataclasses import dataclass

# ── Config ────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    SMTP_HOST:     str = os.getenv("SMTP_HOST",     "smtp.gmail.com")
    SMTP_PORT:     int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER:     str = os.getenv("SMTP_USER",     "your@gmail.com")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "your_app_password")
    FROM_NAME:     str = os.getenv("FROM_NAME",     "ForexPro Signals")
    API_BASE:      str = os.getenv("API_BASE",      "http://localhost:8766")
    API_EMAIL:     str = os.getenv("API_EMAIL",     "provider@forexpro.com")
    API_PASSWORD:  str = os.getenv("API_PASSWORD",  "demo123")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))

cfg = Config()

# ── HTML Email Template ───────────────────────────────────────────────────────
def build_html(s: dict) -> str:
    buy       = s["direction"] == "BUY"
    dir_color = "#00C853" if buy else "#D32F2F"
    dir_bg    = "#E8F5E9" if buy else "#FFEBEE"
    str_color = {"STRONG": "#1B5E20", "MODERATE": "#E65100", "WEAK": "#BF360C", "AVOID": "#B71C1C"}.get(s.get("strength", ""), "#333")
    rr        = s.get("risk_reward", 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ForexPro Signal: {s['pair']} {s['direction']}</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:30px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">

  <!-- Header -->
  <tr>
    <td style="background:#0D1318;padding:28px 32px;text-align:center">
      <div style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.5px">
        Forex<span style="color:#F0B429">Pro</span>
      </div>
      <div style="font-size:11px;color:#4A6070;margin-top:4px;letter-spacing:2px">SIGNAL ALERT</div>
    </td>
  </tr>

  <!-- Signal hero -->
  <tr>
    <td style="background:{dir_bg};padding:28px 32px;text-align:center">
      <div style="font-size:36px;font-weight:800;color:{dir_color};letter-spacing:-1px">
        {s['pair']} {s['direction']}
      </div>
      <div style="margin-top:10px;display:inline-flex;gap:10px">
        <span style="background:{dir_color};color:#fff;padding:4px 14px;border-radius:99px;font-size:12px;font-weight:700">{s['direction']}</span>
        <span style="background:#fff;color:{str_color};border:1px solid {str_color};padding:4px 14px;border-radius:99px;font-size:12px;font-weight:700">{s.get('strength','')}</span>
        <span style="background:#fff;color:#555;border:1px solid #ddd;padding:4px 14px;border-radius:99px;font-size:12px">{s['timeframe']}</span>
      </div>
    </td>
  </tr>

  <!-- Confidence bar -->
  <tr>
    <td style="padding:20px 32px 0">
      <div style="display:flex;justify-content:space-between;margin-bottom:6px">
        <span style="font-size:12px;color:#666">AI Confidence</span>
        <span style="font-size:14px;font-weight:700;color:#F0B429">{s['confidence']}%</span>
      </div>
      <div style="height:8px;background:#eee;border-radius:4px;overflow:hidden">
        <div style="width:{s['confidence']}%;height:100%;background:linear-gradient(90deg,#F0B429,#FF8F00);border-radius:4px"></div>
      </div>
    </td>
  </tr>

  <!-- Price levels -->
  <tr>
    <td style="padding:24px 32px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          {''.join([
            f'<td style="text-align:center;padding:16px;background:#f9f9f9;border-radius:8px;margin:4px" width="33%">'
            f'<div style="font-size:10px;color:#999;letter-spacing:1px;margin-bottom:6px">{lbl}</div>'
            f'<div style="font-size:16px;font-weight:700;color:{col};font-family:monospace">{float(val):.5f}</div>'
            f'</td>'
            for lbl, val, col in [
              ("ENTRY",       s["entry_price"], "#333"),
              ("STOP LOSS",   s["stop_loss"],   "#D32F2F"),
              ("TAKE PROFIT", s["take_profit"], "#1B5E20"),
            ]
          ])}
        </tr>
      </table>
    </td>
  </tr>

  <!-- Stats grid -->
  <tr>
    <td style="padding:0 32px 24px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          {''.join([
            f'<td style="text-align:center;padding:12px 8px;border-top:1px solid #f0f0f0">'
            f'<div style="font-size:10px;color:#999;margin-bottom:4px">{lbl}</div>'
            f'<div style="font-size:14px;font-weight:600;color:{col}">{val}</div>'
            f'</td>'
            for lbl, val, col in [
              ("SL PIPS",   f"{float(s['sl_pips']):.1f}",     "#D32F2F"),
              ("TP PIPS",   f"{float(s['tp_pips']):.1f}",     "#1B5E20"),
              ("RISK:REWARD",f"1:{rr}",                       "#E65100"),
              ("RSI",       f"{float(s.get('rsi',50)):.1f}",  "#1565C0"),
            ]
          ])}
        </tr>
      </table>
    </td>
  </tr>

  <!-- AI Analysis -->
  <tr>
    <td style="padding:0 32px 24px">
      <div style="background:#F8F9FA;border-left:4px solid #F0B429;border-radius:0 8px 8px 0;padding:16px">
        <div style="font-size:10px;color:#999;letter-spacing:1.5px;margin-bottom:8px">AI ANALYSIS</div>
        <div style="font-size:13px;color:#444;line-height:1.7">{s.get('ai_analysis','')}</div>
      </div>
    </td>
  </tr>

  <!-- Entry time -->
  <tr>
    <td style="padding:0 32px 24px">
      <div style="background:#E3F2FD;border-radius:8px;padding:14px;text-align:center">
        <div style="font-size:11px;color:#1565C0;font-weight:600;margin-bottom:4px">⏰ BEST ENTRY TIME</div>
        <div style="font-size:14px;color:#0D47A1;font-weight:700">{s.get('entry_time','London/NY Session')}</div>
      </div>
    </td>
  </tr>

  <!-- Patterns -->
  <tr>
    <td style="padding:0 32px 24px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="50%" style="padding-right:8px">
            <div style="background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
              <div style="font-size:10px;color:#999;margin-bottom:4px">CANDLE PATTERN</div>
              <div style="font-size:13px;font-weight:600;color:{dir_color}">{s.get('candle_pattern','—')}</div>
            </div>
          </td>
          <td width="50%">
            <div style="background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
              <div style="font-size:10px;color:#999;margin-bottom:4px">CHART PATTERN</div>
              <div style="font-size:13px;font-weight:600;color:#1565C0">{s.get('chart_pattern','—')}</div>
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- CTA -->
  <tr>
    <td style="padding:0 32px 32px;text-align:center">
      <a href="http://localhost:5173" style="display:inline-block;background:#F0B429;color:#000;text-decoration:none;padding:14px 36px;border-radius:8px;font-weight:700;font-size:14px">
        View Full Dashboard →
      </a>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#0D1318;padding:20px 32px;text-align:center">
      <div style="font-size:11px;color:#4A6070;line-height:1.7">
        ForexPro Platform · Signal generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC<br>
        ⚠️ For educational purposes only. This is not financial advice.<br>
        <a href="#" style="color:#F0B429;text-decoration:none">Unsubscribe from alerts</a>
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

def build_plain(s: dict) -> str:
    buy = s["direction"] == "BUY"
    return (
        f"ForexPro Signal Alert\n"
        f"{'='*40}\n"
        f"{s['pair']} {s['direction']} | {s['timeframe']}\n"
        f"Confidence: {s['confidence']}% | Strength: {s.get('strength','')}\n\n"
        f"Entry:      {float(s['entry_price']):.5f}\n"
        f"Stop Loss:  {float(s['stop_loss']):.5f}  ({float(s.get('sl_pips',0)):.1f} pips)\n"
        f"Take Profit:{float(s['take_profit']):.5f}  ({float(s.get('tp_pips',0)):.1f} pips)\n"
        f"Risk:Reward: 1:{s.get('risk_reward',0)}\n\n"
        f"RSI: {float(s.get('rsi',50)):.1f}\n"
        f"Pattern: {s.get('candle_pattern','—')}\n"
        f"Best Entry Time: {s.get('entry_time','London/NY')}\n\n"
        f"AI: {s.get('ai_analysis','')}\n\n"
        f"---\nForexPro Platform | Not financial advice."
    )

# ── Send email ────────────────────────────────────────────────────────────────
async def send_signal_email(to_email: str, signal: dict, subject: str = None) -> bool:
    """Send a signal alert email to one recipient."""
    if not subject:
        subject = f"[ForexPro] {signal['pair']} {signal['direction']} Signal — {signal['confidence']}% Confidence"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{cfg.FROM_NAME} <{cfg.SMTP_USER}>"
    msg["To"]      = to_email

    msg.attach(MIMEText(build_plain(signal), "plain"))
    msg.attach(MIMEText(build_html(signal),  "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg.SMTP_HOST,
            port=cfg.SMTP_PORT,
            username=cfg.SMTP_USER,
            password=cfg.SMTP_PASSWORD,
            start_tls=True,
        )
        print(f"Email sent to {to_email}: {signal['pair']} {signal['direction']}")
        return True
    except Exception as e:
        print(f"Email error to {to_email}: {e}")
        return False

async def send_bulk_emails(recipients: list[str], signal: dict) -> dict:
    """Send to multiple recipients concurrently."""
    tasks   = [send_signal_email(r, signal) for r in recipients]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    sent    = sum(1 for r in results if r is True)
    print(f"Bulk email: {sent}/{len(recipients)} sent for {signal['pair']}")
    return {"sent": sent, "failed": len(recipients) - sent}

# ── API helpers ───────────────────────────────────────────────────────────────
async def api_login() -> str:
    async with aiohttp.ClientSession() as s:
        r = await s.post(f"{cfg.API_BASE}/auth/login",
                         json={"email": cfg.API_EMAIL, "password": cfg.API_PASSWORD})
        d = await r.json()
        return d.get("token", "")

async def get_all_user_emails(token: str) -> list[str]:
    """In production: query DB for users with email_alerts=True."""
    # For now returns demo emails — replace with DB query
    return ["yobby@forexpro.com"]

# ── Background watcher ────────────────────────────────────────────────────────
async def watch_signals():
    """Continuously poll for new signals and email subscribers."""
    print(f"Email watcher started — polling every {cfg.POLL_INTERVAL}s")
    token      = await api_login()
    last_id    = ""

    while True:
        try:
            if not token:
                token = await api_login()

            async with aiohttp.ClientSession() as sess:
                hdrs = {"Authorization": f"Bearer {token}"}
                r    = await sess.get(f"{cfg.API_BASE}/signals/latest?limit=3", headers=hdrs)
                data = await r.json()

            sigs = data.get("signals", [])
            if sigs and str(sigs[0].get("id")) != last_id:
                newest = sigs[0]
                last_id = str(newest["id"])
                print(f"New signal detected: {newest['pair']} {newest['direction']}")

                # Get email list
                recipients = await get_all_user_emails(token)
                if recipients:
                    await send_bulk_emails(recipients, newest)

        except Exception as e:
            print(f"Watcher error: {e}")
            token = ""

        await asyncio.sleep(cfg.POLL_INTERVAL)

# ── Test / CLI entry point ────────────────────────────────────────────────────
async def _test():
    """Send a test email with a dummy signal."""
    test_signal = {
        "pair":         "EURUSD",
        "direction":    "BUY",
        "timeframe":    "H1",
        "strength":     "STRONG",
        "confidence":   82,
        "entry_price":  1.08500,
        "stop_loss":    1.08100,
        "take_profit":  1.09300,
        "sl_pips":      40.0,
        "tp_pips":      80.0,
        "risk_reward":  2.0,
        "rsi":          38.5,
        "candle_pattern": "Hammer",
        "chart_pattern":  "Ascending Channel",
        "entry_time":   "08:00-12:00 GMT (London Open)",
        "ai_analysis":  "Confluences: EMA20>EMA50; RSI oversold 38.5; Price at lower BB; MACD positive. Score: 82/100. High-conviction setup.",
    }
    print("Sending test email…")
    ok = await send_signal_email(cfg.SMTP_USER, test_signal)
    print("Test email sent!" if ok else "Failed — check SMTP settings")

if __name__ == "__main__":
    if "--watch" in sys.argv:
        asyncio.run(watch_signals())
    else:
        asyncio.run(_test())