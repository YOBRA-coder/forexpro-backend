"""
Database: SQLite via Python's built-in sqlite3
- Zero setup, single file, production-ready for <10k users
- Easy to migrate to PostgreSQL later (just swap the connection)
- File: forexpro.db (auto-created on first run)
"""
import sqlite3, json, hashlib, os
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "forexpro.db")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent reads
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT    UNIQUE NOT NULL,
            username    TEXT    UNIQUE NOT NULL,
            password    TEXT    NOT NULL,
            role        TEXT    DEFAULT 'trader',  -- trader | provider | admin
            plan        TEXT    DEFAULT 'free',    -- free | pro | elite
            balance     REAL    DEFAULT 10000.0,
            equity      REAL    DEFAULT 10000.0,
            broker      TEXT    DEFAULT '',
            mt5_login   TEXT    DEFAULT '',
            mt5_server  TEXT    DEFAULT '',
            avatar      TEXT    DEFAULT '',
            bio         TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT (datetime('now')),
            last_login  TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id     INTEGER REFERENCES users(id),
            pair            TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            direction       TEXT NOT NULL,
            strength        TEXT NOT NULL,
            confidence      INTEGER NOT NULL,
            entry_price     REAL NOT NULL,
            stop_loss       REAL NOT NULL,
            take_profit     REAL NOT NULL,
            sl_pips         REAL,
            tp_pips         REAL,
            risk_reward     REAL,
            rsi             REAL,
            macd            REAL,
            ema20           REAL,
            ema50           REAL,
            bb_upper        REAL,
            bb_lower        REAL,
            stoch_k         REAL,
            atr             REAL,
            candle_pattern  TEXT,
            chart_pattern   TEXT,
            entry_time      TEXT,
            ai_analysis     TEXT,
            status          TEXT DEFAULT 'active',  -- active|closed|expired|cancelled
            result          TEXT,                    -- win|loss|breakeven
            pnl_pips        REAL DEFAULT 0,
            close_price     REAL,
            closed_at       TEXT,
            expires_at      TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS copy_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            follower_id     INTEGER REFERENCES users(id),
            provider_id     INTEGER REFERENCES users(id),
            signal_id       INTEGER REFERENCES signals(id),
            lot_size        REAL    DEFAULT 0.01,
            risk_pct        REAL    DEFAULT 2.0,
            entry_price     REAL,
            stop_loss       REAL,
            take_profit     REAL,
            status          TEXT    DEFAULT 'pending',  -- pending|open|closed|failed
            result          TEXT,
            pnl_pips        REAL    DEFAULT 0,
            pnl_usd         REAL    DEFAULT 0,
            opened_at       TEXT,
            closed_at       TEXT,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            follower_id     INTEGER REFERENCES users(id),
            provider_id     INTEGER REFERENCES users(id),
            risk_pct        REAL    DEFAULT 2.0,
            max_lot         REAL    DEFAULT 0.1,
            copy_sl         INTEGER DEFAULT 1,
            copy_tp         INTEGER DEFAULT 1,
            min_confidence  INTEGER DEFAULT 60,
            auto_copy       INTEGER DEFAULT 1,
            pairs_filter    TEXT    DEFAULT '[]',   -- JSON array, empty = all
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS providers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER UNIQUE REFERENCES users(id),
            display_name    TEXT NOT NULL,
            description     TEXT DEFAULT '',
            win_rate        REAL DEFAULT 0,
            total_signals   INTEGER DEFAULT 0,
            total_pips      REAL DEFAULT 0,
            avg_rr          REAL DEFAULT 0,
            monthly_pips    REAL DEFAULT 0,
            followers_count INTEGER DEFAULT 0,
            monthly_fee     REAL DEFAULT 0,
            is_verified     INTEGER DEFAULT 0,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS education_courses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            category    TEXT,   -- basics|technical|risk|psychology|advanced
            level       TEXT,   -- beginner|intermediate|advanced
            lessons     TEXT,   -- JSON
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_progress (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            course_id   INTEGER REFERENCES education_courses(id),
            lesson_idx  INTEGER DEFAULT 0,
            completed   INTEGER DEFAULT 0,
            score       INTEGER DEFAULT 0,
            updated_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, course_id)
        );

        CREATE TABLE IF NOT EXISTS trade_journal (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            pair        TEXT,
            direction   TEXT,
            entry_price REAL,
            exit_price  REAL,
            lot_size    REAL,
            pnl_usd     REAL,
            pnl_pips    REAL,
            notes       TEXT,
            emotion     TEXT,   -- calm|fearful|greedy|confident
            setup       TEXT,
            traded_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            type        TEXT,   -- signal|copy|education|system
            title       TEXT,
            message     TEXT,
            is_read     INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)

        # Seed demo users
        _seed_demo_data(db)
    print(f"[DB] SQLite initialized at {DB_PATH}")

def _seed_demo_data(db):
    import hashlib
    def hp(p): return hashlib.sha256(p.encode()).hexdigest()

    # Check if already seeded
    existing = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing > 0:
        return

    # Demo users
    db.execute("INSERT INTO users (email,username,password,role,plan,balance,equity) VALUES (?,?,?,?,?,?,?)",
               ("admin@forexpro.com","admin",hp("admin123"),"admin","elite",50000,52300))
    db.execute("INSERT INTO users (email,username,password,role,plan,balance,equity) VALUES (?,?,?,?,?,?,?)",
               ("provider@forexpro.com","TopTrader_FX",hp("demo123"),"provider","elite",125000,131450))
    db.execute("INSERT INTO users (email,username,password,role,plan,balance,equity) VALUES (?,?,?,?,?,?,?)",
               ("yobby@forexpro.com","Yobby",hp("demo123"),"trader","pro",500,487))
    db.execute("INSERT INTO users (email,username,password,role,plan,balance,equity) VALUES (?,?,?,?,?,?,?)",
               ("demo@forexpro.com","DemoTrader",hp("demo123"),"trader","free",10000,9850))

    # Provider profile
    db.execute("""INSERT INTO providers (user_id,display_name,description,win_rate,total_signals,total_pips,avg_rr,monthly_pips,followers_count,monthly_fee,is_verified)
                  VALUES (2,'TopTrader FX','Professional forex trader with 7 years experience. Specializing in EUR/USD and GBP/USD using price action + multi-TF analysis.',68.4,247,3420.5,2.3,412.0,34,29.99,1)""")

    # Subscription: Yobby follows TopTrader
    db.execute("INSERT INTO subscriptions (follower_id,provider_id,risk_pct,max_lot,min_confidence,auto_copy) VALUES (3,2,2.0,0.05,65,1)")

    # Seed education courses
    courses = [
        ("Forex Fundamentals","Everything you need to know to start trading forex safely","basics","beginner",json.dumps([
            {"title":"What is Forex?","content":"The foreign exchange market is the largest financial market in the world with $6.6 trillion daily volume. You trade currency pairs — buying one currency while selling another.","quiz":[{"q":"What does EUR/USD mean?","options":["Buy Euros, sell USD","EUR is base, USD is quote","Both A and B"],"answer":2}],"duration":5},
            {"title":"Understanding Pips","content":"A pip (percentage in point) is the smallest price move. For most pairs 1 pip = 0.0001. For JPY pairs, 1 pip = 0.01. On EUR/USD, moving from 1.0850 to 1.0860 = 10 pips.","quiz":[{"q":"How many pips is a move from 1.0850 to 1.0920?","options":["7 pips","70 pips","0.7 pips"],"answer":1}],"duration":6},
            {"title":"Lot Sizes & Leverage","content":"Standard lot = 100,000 units. Mini lot = 10,000. Micro lot = 1,000. Nano = 100. With 0.01 lot on EUR/USD, 1 pip = $0.10. Leverage amplifies gains AND losses — 1:100 means $100 controls $10,000.","quiz":[{"q":"On a 0.01 lot EUR/USD trade, how much is 20 pips worth?","options":["$2","$20","$200"],"answer":0}],"duration":8},
            {"title":"Market Sessions","content":"Sydney (22:00-07:00 GMT), Tokyo (00:00-09:00), London (07:00-16:00), New York (12:00-21:00). The London/NY overlap (12:00-16:00 GMT) has the highest volume and best setups.","quiz":[{"q":"Which session has the most volume and tightest spreads?","options":["Tokyo","London","Sydney"],"answer":1}],"duration":5},
            {"title":"Reading Currency Pairs","content":"Base currency is first, quote is second. EUR/USD = 1.0850 means 1 EUR buys 1.0850 USD. If you BUY EUR/USD you profit when EUR strengthens vs USD. Major pairs include USD. Crosses don't include USD.","quiz":[{"q":"If EUR/USD goes from 1.0850 to 1.0900, did EUR strengthen or weaken?","options":["Weakened","Strengthened","Stayed same"],"answer":1}],"duration":5},
        ])),
        ("Technical Analysis Mastery","Master charts, indicators and price action","technical","intermediate",json.dumps([
            {"title":"Support & Resistance","content":"Support is a price floor where buyers consistently enter. Resistance is a ceiling where sellers dominate. The more times a level is tested, the more significant it is. When S/R flips, former support becomes resistance.","quiz":[{"q":"When a support level is broken, it becomes...","options":["Neutral zone","New resistance","Stronger support"],"answer":1}],"duration":10},
            {"title":"RSI — Relative Strength Index","content":"RSI (0-100) measures momentum. Below 30 = oversold (look for buys). Above 70 = overbought (look for sells). RSI divergence is powerful: price makes new high but RSI makes lower high = bearish divergence (sell signal).","quiz":[{"q":"RSI at 28 on a downtrend at major support suggests?","options":["Continue selling","Potential buy reversal","No signal"],"answer":1}],"duration":8},
            {"title":"MACD Explained","content":"MACD = 12 EMA minus 26 EMA. Signal line = 9 EMA of MACD. Histogram = MACD minus Signal. Bullish cross (MACD crosses above signal) = buy. Bearish cross = sell. Histogram turning positive while below zero = early bull signal.","quiz":[{"q":"MACD line crosses above signal line — this is a...","options":["Sell signal","Buy signal","Neutral"],"answer":1}],"duration":8},
            {"title":"Bollinger Bands","content":"Three lines: middle SMA20, upper +2SD, lower -2SD. Price touching upper band = overbought. Lower band = oversold. Band squeeze = low volatility, breakout incoming. Price walking the upper band = strong uptrend.","quiz":[{"q":"Price repeatedly touching the lower Bollinger Band suggests?","options":["Strong uptrend","Oversold — potential reversal","Strong downtrend"],"answer":1}],"duration":7},
            {"title":"Multi-Timeframe Analysis","content":"Always analyze top-down: Daily → H4 → H1 → Entry. Daily = bias (direction). H4 = structure (S/R levels). H1 = setup confirmation. M15 = precise entry. Trading against the daily bias is the #1 mistake beginners make.","quiz":[{"q":"Which timeframe sets your overall trading bias?","options":["M15","H1","Daily"],"answer":2}],"duration":10},
        ])),
        ("Risk Management — Protect Your Capital","The only skill that keeps you in the game long-term","risk","beginner",json.dumps([
            {"title":"The 2% Rule","content":"Never risk more than 2% of your account on a single trade. On a $500 account, max risk = $10. This means you can lose 50 consecutive trades and still have $185 left. Risk management is why professionals survive.","quiz":[{"q":"On a $500 account with 2% rule, max loss per trade is?","options":["$10","$50","$100"],"answer":0}],"duration":6},
            {"title":"Position Sizing Calculator","content":"Lot size = (Account × Risk%) ÷ (SL in pips × Pip value). Example: $500 × 2% = $10 risk. SL = 20 pips. EUR/USD micro lot pip value = $0.10. So: $10 ÷ (20 × $0.10) = 0.05 lots (5 micro lots). Always calculate before entering.","quiz":[{"q":"$1000 account, 2% risk, 25 pip SL on EUR/USD (0.01 lot = $0.10/pip). Correct lot size?","options":["0.01 lots","0.08 lots","0.20 lots"],"answer":1}],"duration":10},
            {"title":"Stop Loss Placement","content":"SL goes beyond structure — not an arbitrary pip count. For support bounces: SL 5-10 pips below the support level. For pin bars: SL 5 pips beyond the wick. For breakouts: SL inside the broken level. Never move SL against you.","quiz":[{"q":"You buy at support. Where does your SL go?","options":["10 pips above entry","5-10 pips below the support level","At the previous high"],"answer":1}],"duration":8},
            {"title":"Risk:Reward Ratios","content":"Minimum 1:2 R:R. If you risk 20 pips, target 40 pips minimum. With 1:2 R:R and 50% win rate, you're profitable. With 1:3 R:R, you're profitable even at 35% win rate. The math works for you, not against you.","quiz":[{"q":"With 1:2 R:R and 40% win rate, are you profitable?","options":["No, you lose money","Yes, you profit","Break even"],"answer":0}],"duration":7},
            {"title":"The 3-Loss Rule","content":"After 3 consecutive losses, STOP TRADING for the day. Your mind is not in the right state. Emotional trading causes 80% of account blowups. Take a walk. Come back tomorrow. Protecting capital is more important than any single trade.","quiz":[{"q":"After 3 losses, you should...","options":["Double down to recover","Stop trading for the day","Switch to a different pair"],"answer":1}],"duration":5},
        ])),
        ("Trading Psychology","Master your mind — the hardest part of trading","psychology","intermediate",json.dumps([
            {"title":"Fear & Greed","content":"Fear makes you exit winners too early and avoid good setups. Greed makes you hold losers too long and overtrade. Both destroy accounts. The solution: a trading plan with fixed rules. Follow the plan, not your emotions.","quiz":[{"q":"You're in profit and feel urge to close early. This is...","options":["Greed","Fear","Good instinct"],"answer":1}],"duration":7},
            {"title":"FOMO — Fear of Missing Out","content":"FOMO causes you to chase trades that have already moved. Rule: if you missed the entry, you missed the trade. There will ALWAYS be another setup. Chasing moves leads to bad entries, wide SLs, and losses.","quiz":[{"q":"EUR/USD just moved 80 pips without you. You should...","options":["Enter now before it moves more","Wait for the next setup","Enter at market and hope"],"answer":1}],"duration":6},
            {"title":"Building Discipline","content":"Discipline = following your rules even when emotions say otherwise. Build it with: a written trading plan, a pre-trade checklist, a trading journal, and fixed session hours. Review your journal weekly. Patterns in your mistakes become visible.","quiz":[{"q":"The most effective tool for building trading discipline is?","options":["More trades","A trading journal","Bigger position sizes"],"answer":1}],"duration":8},
            {"title":"Accepting Losses","content":"Even the best traders lose 40% of their trades. A loss that follows your rules is a GOOD trade. A win that breaks your rules is a BAD trade. You cannot control outcomes — only process. Judge yourself on process, not results.","quiz":[{"q":"A trade hits your SL after following all your rules. This was...","options":["A bad trade","A good trade with bad outcome","Your strategy failing"],"answer":1}],"duration":6},
        ])),
        ("Advanced: Copy Trading & Automation","Build passive income through copy trading systems","advanced","advanced",json.dumps([
            {"title":"What is Copy Trading?","content":"Copy trading automatically replicates a signal provider's trades in your account. When they open EUR/USD Buy 0.1 lots, your account opens proportionally (e.g., 0.01 lots based on your settings). You earn when they earn.","quiz":[{"q":"In copy trading, your position size should be...","options":["Same as provider","Proportional to your account size","Always 0.01 lots"],"answer":1}],"duration":6},
            {"title":"Choosing a Provider","content":"Key metrics: Win rate (>55% minimum), Risk:Reward (>1:2), Drawdown (<20%), Minimum 100 signals history, Consistent monthly pips. Avoid providers with: <3 months history, >30% drawdown, or suspiciously high win rates (>90%).","quiz":[{"q":"A provider shows 95% win rate over 50 trades. You should...","options":["Subscribe immediately","Be very suspicious — this is unsustainable","Ask for more details"],"answer":1}],"duration":8},
            {"title":"Risk Settings for Copy Trading","content":"Risk per copy trade: 1-2% of YOUR account (not provider's). Max lot cap: set based on your balance. Min confidence filter: set to 65+ to only copy high-conviction signals. Auto-copy: on for best results. Pairs filter: limit to pairs you understand.","quiz":[{"q":"Best risk % per copy trade for a $500 account beginner?","options":["5-10%","1-2%","0.5%"],"answer":1}],"duration":8},
            {"title":"MT5 Integration","content":"MetaTrader 5 is the industry standard. To connect: create account at FBS/Exness, get login+password+server. Use MT5 EA (Expert Advisor) for auto-copy or use broker's copy trading portal. Always test on demo first for 30+ days.","quiz":[{"q":"Before live copy trading, you should test for...","options":["1 week","30+ days on demo","No testing needed if provider is good"],"answer":1}],"duration":7},
        ])),
    ]
    for title, desc, cat, level, lessons in courses:
        db.execute("INSERT INTO education_courses (title,description,category,level,lessons) VALUES (?,?,?,?,?)",
                   (title, desc, cat, level, lessons))

    print("[DB] Demo data seeded")

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hashlib.sha256(plain.encode()).hexdigest() == hashed
