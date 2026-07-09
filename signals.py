"""
Market Data Engine
- Primary: Twelve Data free API (800 req/day free, real forex OHLCV)
- Fallback: Realistic synthetic data (GBM + market cycles)
- All technical indicators computed in-house (no external TA lib needed)
"""
import math, hashlib, json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import urllib.request, urllib.error

# ── Twelve Data free API (no key needed for basic quotes) ─────────────────────
TWELVE_DATA_KEY = ""  # Optional: get free key at twelvedata.com for more calls

PAIR_CONFIG = {
    "EURUSD": (1.0850, 0.0050, 0.0001, 1.2, "EUR/USD"),
    "GBPUSD": (1.2700, 0.0080, 0.0001, 1.4, "GBP/USD"),
    "USDJPY": (149.50, 0.60,   0.01,   0.9, "USD/JPY"),
    "AUDUSD": (0.6550, 0.0040, 0.0001, 1.3, "AUD/USD"),
    "USDCAD": (1.3600, 0.0045, 0.0001, 1.5, "USD/CAD"),
    "USDCHF": (0.8950, 0.0035, 0.0001, 1.4, "USD/CHF"),
    "NZDUSD": (0.6080, 0.0038, 0.0001, 1.6, "NZD/USD"),
    "EURGBP": (0.8550, 0.0030, 0.0001, 1.8, "EUR/GBP"),
    "EURJPY": (162.20, 0.75,   0.01,   1.1, "EUR/JPY"),
    "GBPJPY": (190.50, 1.20,   0.01,   1.3, "GBP/JPY"),
    "XAUUSD": (2320.0, 8.0,    0.1,    3.0, "XAU/USD"),
    "BTCUSD": (68000.0,1500.0, 1.0,   25.0, "BTC/USD"),
}

TF_MAP = {
    "M1":  ("1min",     1),
    "M5":  ("5min",     5),
    "M15": ("15min",  15),
    "M30": ("30min",  30),
    "H1":  ("1h",     60),
    "H4":  ("4h",    240),
    "D1":  ("1day", 1440),
    "W1":  ("1week",10080),
}

def fetch_live_ohlcv(pair: str, timeframe: str, outputsize: int = 150) -> Optional[pd.DataFrame]:
    """Fetch real OHLCV data from Twelve Data API"""
    try:
        tf_api, _ = TF_MAP.get(timeframe, ("1h", 60))
        symbol = PAIR_CONFIG[pair][4].replace("/", "")  # EURUSD format
        
        key_param = f"&apikey={TWELVE_DATA_KEY}" if TWELVE_DATA_KEY else "&apikey=demo"
        url = (f"https://api.twelvedata.com/time_series?"
               f"symbol={PAIR_CONFIG[pair][4]}&interval={tf_api}"
               f"&outputsize={outputsize}&format=JSON{key_param}")
        
        req = urllib.request.Request(url, headers={"User-Agent": "ForexPro/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        
        if "values" not in data or not data["values"]:
            return None
            
        rows = data["values"]
        df = pd.DataFrame(rows)
        df = df.rename(columns={"open":"open","high":"high","low":"low","close":"close","datetime":"time"})
        for col in ["open","high","low","close"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("time").reset_index(drop=True)
        df.index = pd.to_datetime(df["time"])
        return df[["open","high","low","close"]]
    except Exception as e:
        return None

def synthetic_ohlcv(pair: str, timeframe: str, n: int = 300, seed: int = None) -> pd.DataFrame:
    """High-quality synthetic OHLCV via GBM + mean reversion + cycles"""
    base, daily_vol, pip, _, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])
    mins = TF_MAP.get(timeframe, ("", 60))[1]
    tf_vol = daily_vol * math.sqrt(mins / 1440)
    
    s = seed or (int(hashlib.md5(f"{pair}{timeframe}".encode()).hexdigest(), 16) % 2**31)
    rng = np.random.default_rng(s)
    
    closes = [base]
    trend = rng.uniform(-0.00015, 0.00015)
    cycle_len = max(20, n // 6)
    
    for i in range(1, n):
        cyc = 0.35 * tf_vol * math.sin(2 * math.pi * i / cycle_len)
        rev = -0.04 * (closes[-1] - base) / base
        shock = rng.normal(0, tf_vol)
        ret = trend + cyc / closes[-1] + rev + shock / closes[-1]
        closes.append(closes[-1] * (1 + ret))
    
    closes = np.array(closes)
    opens  = np.roll(closes, 1); opens[0] = closes[0]
    highs  = np.maximum(opens, closes) + np.abs(rng.normal(0, tf_vol * 0.5, n))
    lows   = np.minimum(opens, closes) - np.abs(rng.normal(0, tf_vol * 0.5, n))
    
    now = datetime.now().replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=mins * (n - i)) for i in range(n)]
    
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=times)
    return df

def get_ohlcv(pair: str, timeframe: str, n: int = 200) -> pd.DataFrame:
    """Get OHLCV — tries live first, falls back to synthetic"""
    if timeframe in ("H1", "H4", "D1"):  # Live data available for these
        live = fetch_live_ohlcv(pair, timeframe, min(n, 500))
        if live is not None and len(live) >= 50:
            return live.tail(n)
    return synthetic_ohlcv(pair, timeframe, n + 50,
                           seed=int(datetime.now().strftime("%Y%m%d%H")))

# ── Technical Indicators ──────────────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def sma(s, p): return s.rolling(p).mean()

def compute_rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0).rolling(p).mean(); l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def compute_macd(s):
    m = ema(s, 12) - ema(s, 26); sig = ema(m, 9)
    return m, sig, m - sig

def compute_bb(s, p=20, k=2):
    mid = sma(s, p); std = s.rolling(p).std()
    return mid + k*std, mid, mid - k*std

def compute_atr(h, l, c, p=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def compute_stoch(h, l, c, kp=14, dp=3):
    ll = l.rolling(kp).min(); hh = h.rolling(kp).max()
    k = 100*(c-ll)/(hh-ll+1e-10)
    return k, k.rolling(dp).mean()

def compute_cci(h, l, c, p=20):
    tp = (h+l+c)/3; ma = sma(tp, p)
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())))
    return (tp - ma) / (0.015 * md + 1e-10)

def compute_adx(h, l, c, p=14):
    """Wilder's ADX + directional indicators. ADX > ~20-25 indicates a market with
    enough trend strength to trade directionally; below that, price is chopping
    sideways and directional signals are unreliable regardless of indicator votes."""
    up_move = h.diff()
    down_move = -l.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1/p, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm, index=h.index).ewm(alpha=1/p, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=h.index).ewm(alpha=1/p, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/p, adjust=False).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    df["ema20"]  = ema(c, 20)
    df["ema50"]  = ema(c, 50)
    df["ema200"] = ema(c, 200)
    df["rsi"]    = compute_rsi(c)
    df["macd"], df["macd_s"], df["macd_h"] = compute_macd(c)
    df["bb_up"], df["bb_mid"], df["bb_low"] = compute_bb(c)
    df["atr"]    = compute_atr(h, l, c)
    df["stoch_k"], df["stoch_d"] = compute_stoch(h, l, c)
    df["cci"]    = compute_cci(h, l, c)
    df["adx"], df["plus_di"], df["minus_di"] = compute_adx(h, l, c)
    return df.dropna()

# ── Pattern Detection ─────────────────────────────────────────────────────────
def detect_candle(row, prev) -> str:
    o,h,l,c = row["open"],row["high"],row["low"],row["close"]
    _,ph,pl,pc = prev["open"],prev["high"],prev["low"],prev["close"]
    body = abs(c-o)+1e-10; rng = h-l+1e-10
    lw = min(o,c)-l; uw = h-max(o,c)
    if lw>=2*body and uw<body and c>o:     return "Hammer"
    if uw>=2*body and lw<body and c<o:     return "Shooting Star"
    if body/rng>0.85 and c>o:             return "Bullish Marubozu"
    if body/rng>0.85 and c<o:             return "Bearish Marubozu"
    if body/rng<0.08:                      return "Doji"
    if c>o and c>ph and o<pc:             return "Bullish Engulfing"
    if c<o and c<pl and o>pc:             return "Bearish Engulfing"
    if lw>=2*body:                         return "Pin Bar (Bull)"
    if uw>=2*body:                         return "Pin Bar (Bear)"
    return "Standard"

def detect_chart_pattern(df) -> str:
    if len(df) < 20: return "N/A"
    recent = df.tail(20); c=recent["close"].values; h=recent["high"].values; l=recent["low"].values
    peaks   = [i for i in range(1,len(h)-1) if h[i]>h[i-1] and h[i]>h[i+1]]
    troughs = [i for i in range(1,len(l)-1) if l[i]<l[i-1] and l[i]<l[i+1]]
    if len(peaks)>=2 and abs(h[peaks[-1]]-h[peaks[-2]])/h[peaks[-2]]<0.006: return "Double Top"
    if len(troughs)>=2 and abs(l[troughs[-1]]-l[troughs[-2]])/l[troughs[-2]]<0.006: return "Double Bottom"
    slope = np.polyfit(range(len(c)), c, 1)[0]
    std_r = np.std(c[-8:])/np.std(c) if np.std(c) else 1
    if std_r<0.35 and slope>0: return "Bull Flag"
    if std_r<0.35 and slope<0: return "Bear Flag"
    if slope>0 and c[-1]>c[-5]: return "Ascending Channel"
    if slope<0 and c[-1]<c[-5]: return "Descending Channel"
    return "No Clear Pattern"

# ── Signal Builder ────────────────────────────────────────────────────────────
NO_TRADE_ADX_FLOOR = 18       # below this, ADX says the market isn't trending — don't force a direction
NO_TRADE_VOTE_MARGIN = 1      # BUY vs SELL votes must differ by more than this to take a side

def _low_liquidity_window(now=None) -> Optional[str]:
    """Cheap heuristic for illiquid/high-slippage windows: weekend market close/open
    and the NY->Sydney rollover hour (21:00-22:00 GMT) where spreads widen and
    price action gets noisy. This is NOT a real economic-calendar/news feed —
    there's no news API key configured — so it only catches predictable liquidity
    gaps, not scheduled data releases (NFP, CPI, rate decisions, etc). Wire in a
    calendar provider (e.g. ForexFactory/TradingEconomics API) for real news avoidance."""
    now = now or datetime.utcnow()
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 21):
        return "Weekend — forex market closed"
    if now.weekday() == 4 and now.hour >= 21:
        return "Market closing for the weekend — low liquidity"
    if now.hour == 21:
        return "NY/Sydney rollover — spreads widen, low liquidity"
    return None

def build_signal(pair: str, timeframe: str, df: pd.DataFrame, provider_id: int = None) -> dict:
    row  = df.iloc[-1]; prev = df.iloc[-2]
    price = float(row["close"]); atr = float(row["atr"])
    _, _, pip, spread, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])

    votes_buy_list = [
        row["ema20"] > row["ema50"], row["close"] > row["ema200"],
        row["rsi"] < 50, row["macd_h"] > 0,
        row["stoch_k"] < 50, row["close"] < row["bb_mid"],
        row["plus_di"] > row["minus_di"],
    ]
    votes_buy = sum(votes_buy_list)
    votes_sell = len(votes_buy_list) - votes_buy
    adx = float(row["adx"])
    liquidity_note = _low_liquidity_window()

    # NO TRADE: votes basically tied (no real edge either way), or ADX says the
    # market is ranging/choppy rather than trending, or we're in a low-liquidity
    # window where any edge is likely to get eaten by spread/slippage anyway.
    no_trade_reason = None
    if abs(votes_buy - votes_sell) <= NO_TRADE_VOTE_MARGIN:
        no_trade_reason = f"Indicators split {votes_buy}-{votes_sell} — no clear directional edge"
    elif adx < NO_TRADE_ADX_FLOOR:
        no_trade_reason = f"ADX {adx:.1f} — market is ranging, not trending (need {NO_TRADE_ADX_FLOOR}+)"
    elif liquidity_note:
        no_trade_reason = liquidity_note

    if no_trade_reason:
        return {
            "provider_id": provider_id, "pair": pair, "timeframe": timeframe,
            "direction": "NO_TRADE", "strength": "AVOID", "confidence": 0,
            "entry_price": round(price, 5), "stop_loss": None, "take_profit": None,
            "sl_pips": 0, "tp_pips": 0, "risk_reward": 0,
            "support_resistance": detect_support_resistance(df), "trendline": detect_trendline(df),
            "markers": [], "atr": round(atr, 5), "rsi": round(float(row["rsi"]), 2),
            "macd": round(float(row["macd"]), 6), "macd_signal": round(float(row["macd_s"]), 6),
            "macd_hist": round(float(row["macd_h"]), 6), "ema20": round(float(row["ema20"]), 5),
            "ema50": round(float(row["ema50"]), 5), "bb_upper": round(float(row["bb_up"]), 5),
            "bb_lower": round(float(row["bb_low"]), 5), "stoch_k": round(float(row["stoch_k"]), 2),
            "adx": round(adx, 1), "candle_pattern": detect_candle(row, prev),
            "chart_pattern": detect_chart_pattern(df),
            "entry_time": datetime.now().isoformat(), "expires_at": datetime.now().isoformat(),
            "ai_analysis": f"NO TRADE — {no_trade_reason}. ADX {adx:.1f}, votes {votes_buy}-{votes_sell}. "
                            f"Sitting out preserves capital until a clearer setup forms.",
            "status": "no_trade",
        }

    direction = "BUY" if votes_buy > votes_sell else "SELL"

    score = 50; reasons = []
    checks = {
        "BUY": [
            (row["ema20"]>row["ema50"],    8, "EMA20 > EMA50 (uptrend)"),
            (row["close"]>row["ema200"],   7, "Price above EMA200"),
            (row["rsi"]<35,                9, f"RSI oversold ({row['rsi']:.1f})"),
            (row["rsi"]<60,                4, f"RSI bullish ({row['rsi']:.1f})"),
            (row["macd_h"]>0,              6, "MACD histogram positive"),
            (row["stoch_k"]<25,            7, f"Stoch oversold ({row['stoch_k']:.1f})"),
            (row["close"]<=row["bb_low"],  8, "Price at lower Bollinger Band"),
            (row["cci"]<-100,              5, "CCI oversold"),
            (row["macd"]>row["macd_s"],    6, "MACD bullish crossover"),
            (adx>=25,                      8, f"ADX {adx:.1f} confirms strong trend"),
            (row["plus_di"]>row["minus_di"], 5, "+DI above -DI"),
        ],
        "SELL": [
            (row["ema20"]<row["ema50"],    8, "EMA20 < EMA50 (downtrend)"),
            (row["close"]<row["ema200"],   7, "Price below EMA200"),
            (row["rsi"]>65,                9, f"RSI overbought ({row['rsi']:.1f})"),
            (row["rsi"]>40,                4, f"RSI bearish ({row['rsi']:.1f})"),
            (row["macd_h"]<0,              6, "MACD histogram negative"),
            (row["stoch_k"]>75,            7, f"Stoch overbought ({row['stoch_k']:.1f})"),
            (row["close"]>=row["bb_up"],   8, "Price at upper Bollinger Band"),
            (row["cci"]>100,               5, "CCI overbought"),
            (row["macd"]<row["macd_s"],    6, "MACD bearish crossover"),
            (adx>=25,                      8, f"ADX {adx:.1f} confirms strong trend"),
            (row["minus_di"]>row["plus_di"], 5, "-DI above +DI"),
        ]
    }
    for cond, pts, reason in checks[direction]:
        if cond: score += pts; reasons.append(reason)
    score = min(100, max(0, score))

    mults = {"M15":(1.0,2.0),"M30":(1.2,2.5),"H1":(1.5,3.0),"H4":(1.8,3.5),"D1":(2.0,4.0),"W1":(2.5,5.0)}
    sl_m, tp_m = mults.get(timeframe, (1.5, 3.0))
    sl = price - atr*sl_m if direction=="BUY" else price + atr*sl_m
    tp = price + atr*tp_m if direction=="BUY" else price - atr*tp_m
    sl_pips = abs(price-sl)/pip; tp_pips = abs(tp-price)/pip
    rr = round(tp_pips/sl_pips, 2) if sl_pips else 0

    strength = "STRONG" if score>=80 else "MODERATE" if score>=65 else "WEAK" if score>=50 else "AVOID"
    exp_h = {"M15":1,"M30":2,"H1":4,"H4":16,"D1":48,"W1":168}.get(timeframe, 4)
    sessions = {"M15":"08:00-10:00 GMT","M30":"08:00-11:00 GMT","H1":"08:00-12:00 GMT",
                "H4":"07:00-09:00 or 13:00-15:00 GMT","D1":"00:00 GMT Daily","W1":"Mon 00:00 GMT"}

    ai = (f"Confluences ({len(reasons)}): {'; '.join(reasons[:4])}. "
          f"Pattern: {detect_candle(row,prev)}. ADX {adx:.1f} ({'trending' if adx>=25 else 'developing trend'}). "
          f"AI Score: {score}/100. "
          f"{'High-conviction setup — manage with trailing stop.' if score>=75 else 'Moderate setup — use strict SL discipline.'}")

    sr_levels = detect_support_resistance(df)
    trendline = detect_trendline(df)
    markers   = build_markers(df, direction)

    return {
        "provider_id":   provider_id,
        "pair":          pair,
        "timeframe":     timeframe,
        "direction":     direction,
        "strength":      strength,
        "confidence":    score,
        "entry_price":   round(price, 5),
        "stop_loss":     round(sl, 5),
        "take_profit":   round(tp, 5),
        "sl_pips":       round(sl_pips, 1),
        "tp_pips":       round(tp_pips, 1),
        "risk_reward":   rr,
        "support_resistance": sr_levels,
        "trendline":     trendline,
        "markers":       markers,
        "atr":           round(atr, 5),
        "rsi":           round(float(row["rsi"]), 2),
        "macd":          round(float(row["macd"]), 6),
        "ema20":         round(float(row["ema20"]), 5),
        "ema50":         round(float(row["ema50"]), 5),
        "bb_upper":      round(float(row["bb_up"]), 5),
        "bb_lower":      round(float(row["bb_low"]), 5),
        "stoch_k":       round(float(row["stoch_k"]), 2),
        "adx":           round(adx, 1),
        "candle_pattern":detect_candle(row, prev),
        "chart_pattern": detect_chart_pattern(df),
        "entry_time":    sessions.get(timeframe, "London/NY Session"),
        "ai_analysis":   ai,
        "expires_at":    (datetime.now() + timedelta(hours=exp_h)).isoformat(),
        "status":        "active",
        "ohlcv": {
            "time":   [str(t)[:16] for t in df.index[-80:]],
            "open":   [round(float(v),5) for v in df["open"].tail(80)],
            "high":   [round(float(v),5) for v in df["high"].tail(80)],
            "low":    [round(float(v),5) for v in df["low"].tail(80)],
            "close":  [round(float(v),5) for v in df["close"].tail(80)],
            "ema20":  [round(float(v),5) for v in df["ema20"].tail(80)],
            "ema50":  [round(float(v),5) for v in df["ema50"].tail(80)],
            "bb_up":  [round(float(v),5) for v in df["bb_up"].tail(80)],
            "bb_low": [round(float(v),5) for v in df["bb_low"].tail(80)],
            "rsi":    [round(float(v),2) for v in df["rsi"].tail(80)],
            "macd_h": [round(float(v),6) for v in df["macd_h"].tail(80)],
        }
    }

# ── Chart Annotation Engine (S/R, trendlines, markers) ────────────────────────
def detect_support_resistance(df: pd.DataFrame, lookback: int = 120, n_levels: int = 4) -> list:
    """Cluster recent swing highs/lows into horizontal support & resistance levels."""
    if len(df) < 20:
        return []
    recent = df.tail(min(lookback, len(df)))
    h, l = recent["high"].values, recent["low"].values
    swing_highs = [h[i] for i in range(2, len(h) - 2)
                   if h[i] == max(h[i-2:i+3])]
    swing_lows  = [l[i] for i in range(2, len(l) - 2)
                   if l[i] == min(l[i-2:i+3])]
    price_range = float(recent["high"].max() - recent["low"].min()) or 1.0
    tol = price_range * 0.006  # cluster tolerance

    def cluster(points):
        pts = sorted(points)
        clusters = []
        for p in pts:
            if clusters and abs(p - clusters[-1]["avg"]) < tol:
                c = clusters[-1]
                c["vals"].append(p)
                c["avg"] = sum(c["vals"]) / len(c["vals"])
            else:
                clusters.append({"vals": [p], "avg": p})
        clusters.sort(key=lambda c: len(c["vals"]), reverse=True)
        return [round(float(c["avg"]), 5) for c in clusters[:n_levels]]

    last_price = float(recent["close"].iloc[-1])
    res = [lvl for lvl in cluster(swing_highs) if lvl > last_price]
    sup = [lvl for lvl in cluster(swing_lows) if lvl < last_price]
    levels = ([{"price": p, "type": "resistance"} for p in sorted(res)[:n_levels]] +
              [{"price": p, "type": "support"} for p in sorted(sup, reverse=True)[:n_levels]])
    return levels

def detect_trendline(df: pd.DataFrame, lookback: int = 40) -> Optional[dict]:
    """Fit a simple trendline through recent swing lows (uptrend) or swing highs (downtrend)."""
    if len(df) < lookback:
        return None
    recent = df.tail(lookback)
    c = recent["close"].values
    slope = np.polyfit(range(len(c)), c, 1)[0]
    use_lows = slope >= 0
    series = recent["low"] if use_lows else recent["high"]
    vals = series.values
    if use_lows:
        i1 = int(np.argmin(vals[:len(vals)//2] if len(vals) > 4 else vals))
        i2 = len(vals) - 1
    else:
        i1 = int(np.argmax(vals[:len(vals)//2] if len(vals) > 4 else vals))
        i2 = len(vals) - 1
    t1, t2 = recent.index[i1], recent.index[i2]
    v1, v2 = float(vals[i1]), float(vals[i2])
    return {
        "direction": "up" if use_lows else "down",
        "p1": {"time": str(t1)[:16], "value": round(v1, 5)},
        "p2": {"time": str(t2)[:16], "value": round(v2, 5)},
    }

def build_markers(df: pd.DataFrame, direction: str) -> list:
    """Return lightweight-charts-style markers for the most recent notable candle pattern."""
    if len(df) < 3:
        return []
    row, prev = df.iloc[-1], df.iloc[-2]
    pattern = detect_candle(row, prev)
    markers = []
    if pattern != "Standard":
        bullish = "Bull" in pattern or pattern in ("Hammer", "Bullish Marubozu", "Bullish Engulfing")
        markers.append({
            "time": str(df.index[-1])[:16],
            "position": "belowBar" if bullish else "aboveBar",
            "color": "#00E070" if bullish else "#FF3550",
            "shape": "arrowUp" if bullish else "arrowDown",
            "text": pattern,
        })
    markers.append({
        "time": str(df.index[-1])[:16],
        "position": "belowBar" if direction == "BUY" else "aboveBar",
        "color": "#F0B429",
        "shape": "arrowUp" if direction == "BUY" else "arrowDown",
        "text": f"{direction} Signal",
    })
    return markers

def pip_value_usd(pair: str, pnl_pips: float, lot_size: float) -> float:
    """Rough standard-lot pip value approximation (~$10/pip on a 1.0 lot for
    most USD-quoted pairs). Good enough for a simulated copy-trading ledger."""
    _, _, pip, _, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])
    per_pip_standard = 10.0 if pair != "XAUUSD" and pair != "BTCUSD" else (100.0 if pair == "XAUUSD" else 1.0)
    return round(pnl_pips * lot_size * per_pip_standard, 2)

def compute_margin_usd(pair: str, lot_size: float) -> float:
    """Cash reserved from the user's balance while a position is open, at a
    simplified ~1:100 leverage (matches the cent/nano-lot brokers recommended
    to users on the Profile page). Not a real margin-call engine — just enough
    so opening/closing a copy trade actually moves the paper balance instead
    of balance sitting frozen at its signup value forever."""
    per_lot = 3000.0 if pair == "XAUUSD" else (2000.0 if pair == "BTCUSD" else 1000.0)
    return round(lot_size * per_lot, 2)

def compute_risk_based_lot(balance: float, risk_pct: float, pair: str, sl_pips: float, max_lot: float) -> float:
    """Proper position sizing: lot = (balance x risk%) / (SL distance in pips x pip value).
    This is what actually makes risk_pct mean something — previously it was stored on
    every subscription/trade but never used; every trade just took the flat max_lot
    regardless of the account size or how far away the stop loss was, so a tight-SL
    signal and a wide-SL signal risked wildly different amounts of real money for the
    'same' risk_pct setting. max_lot still acts as a hard ceiling (a safety cap the
    follower set), it just no longer IS the position size by default.
    """
    if sl_pips <= 0 or balance <= 0:
        return round(min(max_lot, 0.01), 2)
    per_pip_standard = 10.0 if pair != "XAUUSD" and pair != "BTCUSD" else (100.0 if pair == "XAUUSD" else 1.0)
    risk_amount = balance * (risk_pct / 100.0)
    lot = risk_amount / (sl_pips * per_pip_standard)
    lot = max(0.01, min(lot, max_lot))
    return round(lot, 2)

def get_live_quote(pair: str) -> dict:
    """Get single live price quote"""
    base, vol, pip, spread, _ = PAIR_CONFIG[pair]
    try:
        url = f"https://api.twelvedata.com/price?symbol={PAIR_CONFIG[pair][4]}&apikey=demo"
        req = urllib.request.Request(url, headers={"User-Agent": "ForexPro/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if "price" in data:
            price = float(data["price"])
            return {"pair": pair, "price": price,  "bid": round(price - spread*pip/2, 5), "ask": round(price + spread*pip/2, 5), "source": "live"}
    except Exception as e:
        print(f"[ERROR] Failed to fetch live quote for {pair}: {e}")

    # Fallback
   
    import random
    rng = np.random.default_rng(int(datetime.now().strftime("%Y%m%d%H%M")) // 2 + abs(hash(pair)) % 9999)
    price = base * (1 + rng.normal(0, vol/base*0.25))
    chg   = rng.normal(0, vol*0.1)
    return {
        "pair": pair, "price": round(price, 5),
        "bid":  round(price - spread*pip/2, 5),
        "ask":  round(price + spread*pip/2, 5),
        "change": round(chg, 5), "change_pct": round(chg/base*100, 4),
        "high": round(price + abs(rng.normal(0,vol*0.4)),5),
        "low":  round(price - abs(rng.normal(0,vol*0.4)),5),
        "spread": spread, "direction": "up" if chg>=0 else "down",
        "source": "simulated", "timestamp": datetime.now().isoformat()
    }

# ── Backtester ────────────────────────────────────────────────────────────────
def run_backtest(pair: str, timeframe: str, bars: int = 1000, seed: int = 7) -> dict:
    """Walk-forward backtest: at every bar, build_signal() sees ONLY the data up to
    that point (no lookahead), and if it fires a BUY/SELL we track forward bar by
    bar until price actually touches SL or TP (or the signal's expiry passes with
    neither hit, counted as an open/timeout). This tests the exact same decision
    function used in live trading — not a separate/idealized backtest model —
    so the win rate reported here is what you'd actually have gotten trading
    this signal engine on this pair/timeframe.
    """
    warmup = 220  # bars needed before indicators (EMA200, ADX) are meaningful
    df_full = synthetic_ohlcv(pair, timeframe, bars + warmup, seed=seed)
    df_full = add_indicators(df_full).reset_index()
    time_col = df_full.columns[0]

    trades = []
    open_trade = None
    equity_curve = [0.0]
    running_pips = 0.0
    max_dd = 0.0
    peak = 0.0
    no_trade_count = 0

    for i in range(warmup, len(df_full)):
        window = df_full.iloc[max(0, i-210):i+1].set_index(time_col)
        row = df_full.iloc[i]

        # Manage an open trade first: did this bar touch SL or TP?
        if open_trade:
            hi, lo = float(row["high"]), float(row["low"])
            hit = None
            if open_trade["direction"] == "BUY":
                if lo <= open_trade["sl"]: hit = ("loss", open_trade["sl"])
                elif hi >= open_trade["tp"]: hit = ("win", open_trade["tp"])
            else:
                if hi >= open_trade["sl"]: hit = ("loss", open_trade["sl"])
                elif lo <= open_trade["tp"]: hit = ("win", open_trade["tp"])
            open_trade["bars_open"] += 1
            if hit or open_trade["bars_open"] > 200:
                result, exit_price = hit if hit else ("timeout", float(row["close"]))
                pip_size = open_trade["pip"]
                pips = (exit_price - open_trade["entry"]) / pip_size * (1 if open_trade["direction"] == "BUY" else -1)
                running_pips += pips
                peak = max(peak, running_pips)
                max_dd = max(max_dd, peak - running_pips)
                equity_curve.append(round(running_pips, 1))
                trades.append({
                    "entry_time": open_trade["entry_time"], "direction": open_trade["direction"],
                    "entry": round(open_trade["entry"], 5), "exit": round(exit_price, 5),
                    "result": result, "pips": round(pips, 1), "confidence": open_trade["confidence"],
                })
                open_trade = None
            continue

        try:
            sig = build_signal(pair, timeframe, window)
        except Exception:
            continue
        if sig["direction"] == "NO_TRADE":
            no_trade_count += 1
            continue
        if sig["confidence"] < 60:
            continue
        _, _, pip_size, _, _ = PAIR_CONFIG.get(pair, PAIR_CONFIG["EURUSD"])
        open_trade = {
            "direction": sig["direction"], "entry": sig["entry_price"], "sl": sig["stop_loss"],
            "tp": sig["take_profit"], "pip": pip_size, "bars_open": 0,
            "confidence": sig["confidence"], "entry_time": str(row[time_col]),
        }

    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    timeouts = [t for t in trades if t["result"] == "timeout"]
    gross_win = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win else 0.0)

    # Longest losing streak — the number a trader actually needs for position sizing
    streak = max_streak = 0
    for t in trades:
        if t["result"] == "loss":
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "pair": pair, "timeframe": timeframe, "bars_tested": bars,
        "total_signals_fired": len(trades), "no_trade_bars": no_trade_count,
        "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
        "win_rate_pct": win_rate, "profit_factor": profit_factor,
        "total_pips": round(running_pips, 1), "max_drawdown_pips": round(max_dd, 1),
        "max_consecutive_losses": max_streak,
        "avg_win_pips": round(gross_win / len(wins), 1) if wins else 0.0,
        "avg_loss_pips": round(-gross_loss / len(losses), 1) if losses else 0.0,
        "equity_curve": equity_curve,
        "trades": trades[-100:],  # cap payload size
        "note": "Backtested on synthetic price data (GBM + cycles), not real historical "
                "broker ticks — treat win rate as a test of the DECISION LOGIC's internal "
                "consistency, not a promise of live performance. Configure a real market "
                "data key (see TWELVE_DATA_KEY) to backtest against actual history.",
    }
