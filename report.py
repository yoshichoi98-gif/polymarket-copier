#!/usr/bin/env python3
"""P&L report for the Polymarket paper copy-trader.

Usage: python3 report.py
Reads ledger.db, marks open positions to live CLOB midpoints, and prints
open positions, closed results, and per-trader totals.
"""

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "ledger.db"
CLOB_API = "https://clob.polymarket.com"


def http_get_json(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "paper-copier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def midpoint(token_id):
    try:
        return float(http_get_json(f"{CLOB_API}/midpoint", {"token_id": token_id})["mid"])
    except Exception:  # noqa: BLE001 - report should degrade, not crash
        return None


def fmt_money(x, signed=True):
    if x < 0:
        return f"-${abs(x):,.2f}"
    return f"{'+' if signed else ''}${x:,.2f}"


def main():
    if not DB_PATH.exists():
        print("No ledger yet — the copier hasn't recorded anything.")
        return
    db = sqlite3.connect(DB_PATH)

    start_ts = db.execute("SELECT value FROM meta WHERE key='start_ts'").fetchone()
    if start_ts:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(start_ts[0])))
        print(f"Paper copy-trading since {started}\n")

    # ----- Open positions ----------------------------------------------------
    rows = db.execute(
        "SELECT trader, title, outcome, asset, shares, avg_cost, their_avg"
        " FROM positions ORDER BY trader, title"
    ).fetchall()
    fill_times = {
        asset: (first, n) for asset, first, n in db.execute(
            "SELECT asset, MIN(ts), COUNT(*) FROM trades WHERE side='BUY' GROUP BY asset")
    }

    unrealized = defaultdict(float)
    cost_open = defaultdict(float)
    print(f"OPEN POSITIONS ({len(rows)})")
    print("-" * 78)
    if not rows:
        print("(none)")
    for trader, title, outcome, asset, shares, avg_cost, their_avg in rows:
        if their_avg is None:
            their_avg = avg_cost
        mid = midpoint(asset)
        cost = shares * avg_cost
        cost_open[trader] += cost
        first, n = fill_times.get(asset, (None, 0))
        opened = (
            time.strftime("opened %m-%d %H:%M", time.localtime(first)) if first else ""
        ) + (f" ({n} fills)" if n > 1 else "")
        entry = f"{opened} — his entry {their_avg * 100:.1f}¢ / yours {avg_cost * 100:.1f}¢"
        if mid is None:
            print(
                f"  [{trader}] {title}: {outcome}\n"
                f"      {shares:.2f} sh — {entry} (cost {fmt_money(cost, False)})"
                f" — live price unavailable"
            )
            continue
        pnl = shares * (mid - avg_cost)
        unrealized[trader] += pnl
        print(
            f"  [{trader}] {title}: {outcome}\n"
            f"      {shares:.2f} sh — {entry} → now {mid * 100:.1f}¢"
            f"  |  cost {fmt_money(cost, False)}  |  unrealized {fmt_money(pnl)}"
        )

    # ----- Closed positions --------------------------------------------------
    closed = db.execute(
        "SELECT trader, title, outcome, shares, avg_cost, exit_price, pnl, reason"
        " FROM closed ORDER BY ts"
    ).fetchall()
    realized = defaultdict(float)
    wins = defaultdict(int)
    total_closed = defaultdict(int)
    print(f"\nCLOSED ({len(closed)})")
    print("-" * 78)
    if not closed:
        print("(none)")
    for trader, title, outcome, shares, avg_cost, exit_price, pnl, reason in closed:
        realized[trader] += pnl
        total_closed[trader] += 1
        if pnl >= 0:
            wins[trader] += 1
        print(
            f"  [{trader}] {title}: {outcome} ({reason})\n"
            f"      {shares:.2f} sh, {avg_cost * 100:.1f}¢ → {exit_price * 100:.1f}¢"
            f"  |  {fmt_money(pnl)}"
        )

    # ----- Recent fills (his price vs ours) ------------------------------------
    fills = db.execute(
        "SELECT ts, trader, side, our_shares, price, our_price, title, outcome"
        " FROM trades ORDER BY ts DESC, id DESC LIMIT 15"
    ).fetchall()
    print("\nRECENT FILLS (his price vs yours, newest first)")
    print("-" * 78)
    if not fills:
        print("(none)")
    for ts, trader, side, our_shares, price, our_price, title, outcome in fills:
        if our_price is None:
            our_price = price
        when = time.strftime("%m-%d %H:%M", time.localtime(ts))
        slip = (our_price - price) * 100 if side == "BUY" else (price - our_price) * 100
        slip_s = f"slip {slip:+.1f}¢" if abs(slip) > 0.049 else "no slip"
        print(
            f"  {when} [{trader}] {side} {our_shares:.2f} sh — "
            f"his {price * 100:.1f}¢ / yours {our_price * 100:.1f}¢ ({slip_s}) — "
            f"{title}: {outcome}"
        )

    # ----- Totals -------------------------------------------------------------
    n_trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    traders = sorted(set(list(unrealized) + list(realized) + list(cost_open)))
    print("\nTOTALS")
    print("-" * 78)
    grand = 0.0
    for t in traders:
        total = realized[t] + unrealized[t]
        grand += total
        wr = f"{wins[t]}/{total_closed[t]} wins" if total_closed[t] else "no closed bets"
        print(
            f"  {t:<12} realized {fmt_money(realized[t]):>10}"
            f"  unrealized {fmt_money(unrealized[t]):>10}"
            f"  total {fmt_money(total):>10}  ({wr})"
        )
    print(f"\n  Overall P&L: {fmt_money(grand)}  ({n_trades} fills copied)")


if __name__ == "__main__":
    main()
