#!/usr/bin/env python3
"""Polymarket paper copy-trader.

Watches configured traders' public activity feeds and mirrors each trade
as a paper position at 1% of their share size, at their fill price.
No wallet, no keys, no real money — everything is recorded in a local
SQLite ledger and surfaced via macOS notifications + copier.log.

Run:  python3 copier.py
Stop: Ctrl-C (or launchctl unload the LaunchAgent)
"""

import json
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

TRADERS = {
    "0x204f72f35326db932158cba6adff0b9a1da95e14": "swisstony",
    "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11": "ColdMath",
    "0xe5efd6543002653b2eef940d8254911862ad9e7a": "0xe5ef…",   # Shocked-Yoke, sports whale
    "0xdefebc7ed6f5d6e8710398f37eea6fc1746a17f4": "0xdefe…",   # S&P/SPY daily markets
    "0x53c48faa2373f597b693567f79c3782c6ce7740b": "uma003",    # small/occasional trader
    "0x048215305cbcf7cc790735bf00119551d75c6b0a": "dumbfuqLP",     # AI markets, mid-size
    "0x8e77537e059837d3c2ca5b4efe75e74e9498c4f3": "dwpoker",       # geopolitics/novelty
    "0x5235578efe24555b0c98e7dc10a902b09089c04a": "back-in-whack", # geopolitics
    "0xb91aeb5accc33a5f9a8615b8ed6b2d352e913987": "afghj2421",     # Gold, sports, big bets
}
COPY_RATIO = 1.0           # copy their size $-for-$ (full mirror)
MIN_BET = 0.0              # no minimum — faithful sizing, dust stays dust
MAX_SLIPPAGE = 0.05        # skip the copy if live price is >5¢ worse than their fill
NOTIFICATIONS = False      # macOS notifications per copied trade
POLL_INTERVAL = 1          # seconds between activity polls (limit: 1000 req/10s)
SETTLE_INTERVAL = 600      # seconds between market-resolution sweeps
MIN_SHARES = 0.000001      # dust threshold below which a position is closed

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ledger.db"
LOG_PATH = BASE_DIR / "copier.log"

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, message):
    """Pop a macOS notification. Failures are non-fatal."""
    if not NOTIFICATIONS:
        return
    try:
        script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - notification must never kill the loop
        log(f"notify failed: {e}")


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def http_get_json(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "paper-copier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def cents(price):
    return f"{float(price) * 100:.1f}¢".replace(".0¢", "¢")


def fmt_pnl(pnl):
    return f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"


# ----------------------------------------------------------------------------
# Ledger (SQLite)
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS trades (          -- every copied paper fill
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    trader TEXT,
    side TEXT,
    asset TEXT,
    condition_id TEXT,
    outcome_index INTEGER,
    title TEXT,
    outcome TEXT,
    their_shares REAL,
    our_shares REAL,
    price REAL,               -- their fill price
    our_price REAL,           -- live price we simulated filling at
    usdc REAL,
    tx_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_fill
    ON trades (tx_hash, asset, side, their_shares);
CREATE TABLE IF NOT EXISTS positions (       -- open paper positions
    asset TEXT PRIMARY KEY,
    condition_id TEXT,
    outcome_index INTEGER,
    title TEXT,
    outcome TEXT,
    trader TEXT,
    shares REAL,
    avg_cost REAL,            -- our average entry price
    their_avg REAL,           -- trader's average entry price
    their_shares REAL,        -- trader's net shares (so sells mirror their %)
    event_slug TEXT           -- polymarket.com/event/<slug>
);
CREATE TABLE IF NOT EXISTS skipped (         -- trades not copied (slippage cap)
    tx_hash TEXT,
    asset TEXT,
    side TEXT,
    their_shares REAL,
    ts INTEGER,
    trader TEXT,
    title TEXT,
    outcome TEXT,
    their_price REAL,
    market_price REAL,
    event_slug TEXT,
    UNIQUE (tx_hash, asset, side, their_shares)
);
CREATE TABLE IF NOT EXISTS snapshots (       -- end-of-day P&L history
    date TEXT PRIMARY KEY,
    ts INTEGER,
    realized REAL,
    unrealized REAL,
    total REAL,
    open_count INTEGER,
    fills INTEGER
);
CREATE TABLE IF NOT EXISTS traders (         -- who we're tailing (editable via dashboard)
    wallet TEXT PRIMARY KEY,
    name TEXT,
    added_ts INTEGER,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trader_snapshots ( -- per-trader end-of-day history
    date TEXT,
    trader TEXT,
    ts INTEGER,
    realized REAL,
    unrealized REAL,
    total REAL,
    open_count INTEGER,
    fills INTEGER,
    PRIMARY KEY (date, trader)
);
CREATE TABLE IF NOT EXISTS closed (          -- realized results
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    asset TEXT,
    title TEXT,
    outcome TEXT,
    trader TEXT,
    shares REAL,
    avg_cost REAL,                           -- our average entry price
    their_avg REAL,                          -- trader's average entry price
    exit_price REAL,
    pnl REAL,
    reason TEXT,                             -- 'sell' or 'resolved'
    event_slug TEXT
);
"""


def open_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    # WAL lets the dashboard read/write without colliding with the copier's
    # constant writes; busy_timeout makes any contention wait instead of erroring.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    # Migrations for ledgers created before his-vs-our price tracking.
    for stmt in (
        "ALTER TABLE trades ADD COLUMN our_price REAL",
        "ALTER TABLE positions ADD COLUMN their_avg REAL",
        "ALTER TABLE positions ADD COLUMN their_shares REAL",
        "ALTER TABLE positions ADD COLUMN event_slug TEXT",
        "ALTER TABLE closed ADD COLUMN event_slug TEXT",
        "ALTER TABLE closed ADD COLUMN their_avg REAL",
        "ALTER TABLE skipped ADD COLUMN ts INTEGER",
        "ALTER TABLE skipped ADD COLUMN trader TEXT",
        "ALTER TABLE skipped ADD COLUMN title TEXT",
        "ALTER TABLE skipped ADD COLUMN outcome TEXT",
        "ALTER TABLE skipped ADD COLUMN their_price REAL",
        "ALTER TABLE skipped ADD COLUMN market_price REAL",
        "ALTER TABLE skipped ADD COLUMN event_slug TEXT",
    ):
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    db.execute("UPDATE trades SET our_price=price WHERE our_price IS NULL")
    db.execute("UPDATE positions SET their_avg=avg_cost WHERE their_avg IS NULL")
    # Pre-MIN_BET positions were exactly 1% copies, so their size is ours / ratio.
    db.execute(
        "UPDATE positions SET their_shares = shares / ? WHERE their_shares IS NULL",
        (COPY_RATIO,),
    )
    db.commit()
    seed_traders(db)
    return db


def seed_traders(db):
    """Populate the traders table once from the legacy hardcoded TRADERS dict."""
    if db.execute("SELECT COUNT(*) FROM traders").fetchone()[0]:
        return
    for wallet, name in TRADERS.items():
        added = get_meta(db, f"last_seen:{wallet}") or get_meta(db, "start_ts") or int(time.time())
        db.execute(
            "INSERT OR IGNORE INTO traders (wallet, name, added_ts, active)"
            " VALUES (?,?,?,1)",
            (wallet, name, int(added)),
        )
    db.commit()


def load_traders(db):
    """Active traders to tail, as a dict {wallet: name}."""
    return dict(db.execute(
        "SELECT wallet, name FROM traders WHERE active=1 ORDER BY added_ts"))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


# ----------------------------------------------------------------------------
# Core copy logic (pure-ish; also used by tests)
# ----------------------------------------------------------------------------


def record_trade(db, trader_name, act, our_price=None):
    """Mirror one activity record as a paper fill.

    `our_price` is the live price we'd realistically fill at right now;
    when None (offline tests, API hiccup) we fall back to their price.
    Returns a human summary string if the trade was copied, else None
    (duplicate, dust, or a sell of something we don't hold).
    """
    side = act["side"].upper()
    their_shares = float(act["size"])
    their_price = float(act["price"])
    price = our_price if our_price is not None else their_price  # our fill
    our_shares = their_shares * COPY_RATIO
    asset = act["asset"]

    if our_shares < MIN_SHARES:
        return None

    # Slippage cap: like a limit order near their price — if the live market
    # has moved more than MAX_SLIPPAGE against us, we simply don't fill.
    if our_price is not None:
        slip = (price - their_price) if side == "BUY" else (their_price - price)
        if slip > MAX_SLIPPAGE:
            try:
                db.execute(
                    "INSERT INTO skipped (tx_hash, asset, side, their_shares, ts,"
                    " trader, title, outcome, their_price, market_price, event_slug)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        act.get("transactionHash", ""), asset, side, their_shares,
                        int(act["timestamp"]), trader_name, act.get("title", ""),
                        act.get("outcome", ""), their_price, price,
                        act.get("eventSlug", ""),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                return None  # already logged this skip
            return (
                f"⏭ SKIPPED {trader_name} {side} — his {cents(their_price)} but market"
                f" now {cents(price)} (> {cents(MAX_SLIPPAGE)} slippage)"
                f" — {act.get('title', '?')}: {act.get('outcome', '?')}"
            )

    pos = db.execute(
        "SELECT shares, avg_cost, their_avg, their_shares, event_slug"
        " FROM positions WHERE asset=?",
        (asset,),
    ).fetchone()

    if side == "SELL":
        if not pos:
            return None  # they're exiting a position that predates monitoring
        held, avg_cost, pos_their_avg, their_held, pos_slug = pos
        # Mirror the fraction of THEIR position they're selling.
        if their_held and their_held > MIN_SHARES:
            frac = min(1.0, their_shares / their_held)
            sell_shares = held * frac
        else:
            sell_shares = min(our_shares, held)
        if sell_shares < MIN_SHARES:
            return None
    else:
        sell_shares = 0.0
        # Optional minimum per copied buy (no-op when MIN_BET is 0).
        if MIN_BET and our_shares * price < MIN_BET:
            our_shares = MIN_BET / price

    # Dedup + record the fill. UNIQUE index rejects replays after restart.
    usdc = (sell_shares if side == "SELL" else our_shares) * price
    try:
        db.execute(
            "INSERT INTO trades (ts, trader, side, asset, condition_id, outcome_index,"
            " title, outcome, their_shares, our_shares, price, our_price, usdc, tx_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(act["timestamp"]), trader_name, side, asset,
                act.get("conditionId", ""), int(act.get("outcomeIndex", 0)),
                act.get("title", ""), act.get("outcome", ""),
                their_shares, (sell_shares if side == "SELL" else our_shares),
                their_price, price, usdc, act.get("transactionHash", ""),
            ),
        )
    except sqlite3.IntegrityError:
        return None  # already copied this fill

    if side == "BUY":
        if pos:
            held, avg_cost, their_avg, their_held, _ = pos
            if their_avg is None:
                their_avg = avg_cost
            new_shares = held + our_shares
            new_cost = (held * avg_cost + our_shares * price) / new_shares
            new_their = (held * their_avg + our_shares * their_price) / new_shares
            db.execute(
                "UPDATE positions SET shares=?, avg_cost=?, their_avg=?,"
                " their_shares=? WHERE asset=?",
                (new_shares, new_cost, new_their, (their_held or 0) + their_shares, asset),
            )
        else:
            db.execute(
                "INSERT INTO positions (asset, condition_id, outcome_index, title,"
                " outcome, trader, shares, avg_cost, their_avg, their_shares, event_slug)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset, act.get("conditionId", ""), int(act.get("outcomeIndex", 0)),
                    act.get("title", ""), act.get("outcome", ""), trader_name,
                    our_shares, price, their_price, their_shares,
                    act.get("eventSlug", ""),
                ),
            )
        db.commit()
        return (
            f"\U0001f4c8 {trader_name} BUY {our_shares:.2f} sh"
            f" — his {cents(their_price)} / ours {cents(price)} (${usdc:.2f})"
            f" — {act.get('title', '?')}: {act.get('outcome', '?')}"
        )

    # SELL
    pnl = sell_shares * (price - avg_cost)
    remaining = held - sell_shares
    db.execute(
        "INSERT INTO closed (ts, asset, title, outcome, trader, shares, avg_cost,"
        " their_avg, exit_price, pnl, reason, event_slug) VALUES (?,?,?,?,?,?,?,?,?,?,'sell',?)",
        (
            int(act["timestamp"]), asset, act.get("title", ""), act.get("outcome", ""),
            trader_name, sell_shares, avg_cost,
            pos_their_avg if pos_their_avg is not None else avg_cost,
            price, pnl, pos_slug or act.get("eventSlug", ""),
        ),
    )
    if remaining < MIN_SHARES:
        db.execute("DELETE FROM positions WHERE asset=?", (asset,))
    else:
        db.execute(
            "UPDATE positions SET shares=?, their_shares=? WHERE asset=?",
            (remaining, max(0.0, (their_held or 0) - their_shares), asset),
        )
    db.commit()
    return (
        f"\U0001f4c9 {trader_name} SELL {sell_shares:.2f} sh"
        f" — his {cents(their_price)} / ours {cents(price)} ({fmt_pnl(pnl)})"
        f" — {act.get('title', '?')}: {act.get('outcome', '?')}"
    )


def settle_position(db, asset, resolved_price, ts):
    """Close an open position at its resolved price. Returns summary string."""
    pos = db.execute(
        "SELECT shares, avg_cost, title, outcome, trader, event_slug, their_avg"
        " FROM positions WHERE asset=?",
        (asset,),
    ).fetchone()
    if not pos:
        return None
    shares, avg_cost, title, outcome, trader, ev_slug, their_avg = pos
    pnl = shares * (resolved_price - avg_cost)
    db.execute(
        "INSERT INTO closed (ts, asset, title, outcome, trader, shares, avg_cost,"
        " their_avg, exit_price, pnl, reason, event_slug)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'resolved',?)",
        (ts, asset, title, outcome, trader, shares, avg_cost,
         their_avg if their_avg is not None else avg_cost, resolved_price, pnl, ev_slug),
    )
    db.execute("DELETE FROM positions WHERE asset=?", (asset,))
    db.commit()
    won = "WON" if resolved_price >= 0.5 else "LOST"
    return f"✅ Resolved {won} ({fmt_pnl(pnl)}) — {title}: {outcome} [{trader}]"


# ----------------------------------------------------------------------------
# Polling
# ----------------------------------------------------------------------------


def live_fill_price(asset, side):
    """The price we'd realistically get right now: pay the ask to buy,
    hit the bid to sell. (CLOB side=SELL quotes the ask, side=BUY the bid.)
    Returns None on failure — caller falls back to the trader's price."""
    clob_side = "SELL" if side == "BUY" else "BUY"
    try:
        resp = http_get_json(
            f"{CLOB_API}/price", {"token_id": asset, "side": clob_side}, timeout=5
        )
        p = float(resp["price"])
        return p if 0 < p < 1 else None
    except Exception:  # noqa: BLE001
        return None


def poll_trader(db, wallet, name, since_ts):
    """Fetch activity since `since_ts` and copy new trades. Returns newest ts seen."""
    acts = http_get_json(
        f"{DATA_API}/activity",
        {
            "user": wallet,
            "type": "TRADE",
            "limit": 500,
            "start": since_ts,
            "sortDirection": "ASC",
        },
    )
    newest = since_ts
    for act in acts:
        newest = max(newest, int(act["timestamp"]))
        our_price = live_fill_price(act["asset"], act["side"].upper())
        summary = record_trade(db, name, act, our_price=our_price)
        if summary:
            log(summary)
            notify("Polymarket Copier", summary)
    return newest


def take_daily_snapshot(db):
    """Record one P&L snapshot per calendar day (for the dashboard history)."""
    today = time.strftime("%Y-%m-%d")
    if get_meta(db, "last_snapshot_date") == today:
        return
    realized = db.execute("SELECT COALESCE(SUM(pnl),0) FROM closed").fetchone()[0]
    fills = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    realized_t = dict(db.execute(
        "SELECT trader, COALESCE(SUM(pnl),0) FROM closed GROUP BY trader"))
    fills_t = dict(db.execute("SELECT trader, COUNT(*) FROM trades GROUP BY trader"))
    unrealized = 0.0
    unreal_t, open_t = {}, {}
    positions = db.execute(
        "SELECT asset, shares, avg_cost, trader FROM positions").fetchall()
    for asset, shares, avg_cost, trader in positions:
        open_t[trader] = open_t.get(trader, 0) + 1
        try:
            mid = float(
                http_get_json(f"{CLOB_API}/midpoint", {"token_id": asset}, timeout=5)["mid"]
            )
            u = shares * (mid - avg_cost)
            unrealized += u
            unreal_t[trader] = unreal_t.get(trader, 0.0) + u
        except Exception:  # noqa: BLE001 - skip unpriceable positions
            pass
    now_ts = int(time.time())
    db.execute(
        "INSERT OR REPLACE INTO snapshots (date, ts, realized, unrealized, total,"
        " open_count, fills) VALUES (?,?,?,?,?,?,?)",
        (today, now_ts, realized, unrealized, realized + unrealized,
         len(positions), fills),
    )
    for trader in set(realized_t) | set(open_t):
        r = realized_t.get(trader, 0.0)
        u = unreal_t.get(trader, 0.0)
        db.execute(
            "INSERT OR REPLACE INTO trader_snapshots (date, trader, ts, realized,"
            " unrealized, total, open_count, fills) VALUES (?,?,?,?,?,?,?,?)",
            (today, trader, now_ts, r, u, r + u,
             open_t.get(trader, 0), fills_t.get(trader, 0)),
        )
    db.commit()
    set_meta(db, "last_snapshot_date", today)
    log(f"📊 Daily snapshot {today}: total {fmt_pnl(realized + unrealized)}"
        f" (realized {fmt_pnl(realized)}, {len(positions)} open, {fills} fills)")


def settlement_sweep(db):
    """Check open positions' markets for resolution and settle them."""
    rows = db.execute(
        "SELECT DISTINCT condition_id FROM positions WHERE condition_id != ''"
    ).fetchall()
    for (cid,) in rows:
        try:
            markets = http_get_json(f"{GAMMA_API}/markets", {"condition_ids": cid})
        except Exception as e:  # noqa: BLE001
            log(f"settlement check failed for {cid}: {e}")
            continue
        if not markets:
            # Some markets never appear in gamma; the CLOB still knows them
            # and flags the winning token after resolution.
            try:
                m = http_get_json(f"{CLOB_API}/markets/{cid}")
            except Exception as e:  # noqa: BLE001
                log(f"clob settlement check failed for {cid}: {e}")
                continue
            tokens = m.get("tokens") or []
            if not m.get("closed") or not any(t.get("winner") for t in tokens):
                continue
            winners = {t.get("token_id"): (1.0 if t.get("winner") else 0.0) for t in tokens}
            held = db.execute(
                "SELECT asset FROM positions WHERE condition_id=?", (cid,)
            ).fetchall()
            for (asset,) in held:
                if asset in winners:
                    summary = settle_position(db, asset, winners[asset], int(time.time()))
                    if summary:
                        log(summary)
                        notify("Polymarket Copier", summary)
            continue
        m = markets[0]
        if not m.get("closed"):
            continue
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
        except (ValueError, TypeError):
            continue
        if not prices:
            continue
        positions = db.execute(
            "SELECT asset, outcome_index FROM positions WHERE condition_id=?", (cid,)
        ).fetchall()
        for asset, oi in positions:
            if oi >= len(prices):
                continue
            summary = settle_position(db, asset, float(prices[oi]), int(time.time()))
            if summary:
                log(summary)
                notify("Polymarket Copier", summary)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------


def main():
    db = open_db()

    start_ts = get_meta(db, "start_ts")
    if start_ts is None:
        start_ts = int(time.time())
        set_meta(db, "start_ts", start_ts)
        log(f"First run — copying trades from now on (start_ts={start_ts})")
    start_ts = int(start_ts)

    # Per-trader watermark of the newest trade timestamp we've processed.
    last_seen = {}

    log(
        "Watching: "
        + ", ".join(f"{n} ({w[:8]}…)" for w, n in load_traders(db).items())
        + f" | copy ratio {COPY_RATIO:.0%} | poll {POLL_INTERVAL}s"
    )

    last_settle = 0.0
    while True:
        # Re-read the trader list each loop so dashboard edits take effect live.
        traders = load_traders(db)
        for wallet, name in traders.items():
            if wallet not in last_seen:
                saved = get_meta(db, f"last_seen:{wallet}")
                # New traders start from "now" — never backfill earlier trades.
                last_seen[wallet] = int(saved) if saved else int(time.time())
                log(f"➕ now tailing {name} ({wallet[:10]}…)")
            try:
                newest = poll_trader(db, wallet, name, last_seen[wallet])
                if newest != last_seen[wallet]:
                    last_seen[wallet] = newest
                    set_meta(db, f"last_seen:{wallet}", newest)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log(f"poll error for {name}: {e}")

        if time.time() - last_settle > SETTLE_INTERVAL:
            last_settle = time.time()
            try:
                settlement_sweep(db)
            except Exception as e:  # noqa: BLE001
                log(f"settlement sweep error: {e}")
            try:
                take_daily_snapshot(db)
            except Exception as e:  # noqa: BLE001
                log(f"snapshot error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
