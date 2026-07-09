#!/usr/bin/env python3
"""Read-only web dashboard for the Polymarket paper copy-trader.

Serves a single auto-refreshing HTML page with overall P&L, per-trader
breakdown, daily history, open positions, and recent fills.
Run: python3 dashboard.py  (listens on port 80)
"""

import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "ledger.db"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
PORT = 80
CACHE_SECONDS = 60
# Dashboard password comes from the environment (set by the systemd unit) so no
# secret lives in the code. Falls back to "changeme" if unset.
PASSWORD = os.environ.get("DASH_PASSWORD", "changeme")
# Logged-in browsers hold this cookie token (stable as long as PASSWORD is).
TOKEN = hashlib.sha256(f"cookie-auth:{PASSWORD}".encode()).hexdigest()

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Polymarket Paper Copier</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body {{ font-family:-apple-system,system-ui,sans-serif; background:#0f1115; color:#e6e6e6;
        display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
 form {{ text-align:center; }}
 input {{ font-size:1.1em; padding:10px 14px; border-radius:8px; border:1px solid #333;
         background:#171a21; color:#e6e6e6; outline:none; }}
 button {{ font-size:1.1em; padding:10px 18px; margin-left:8px; border-radius:8px;
          border:none; background:#2563eb; color:white; cursor:pointer; }}
 .err {{ color:#f16a6a; margin-top:12px; }}
</style></head><body>
<form method="POST" action="/login">
  <h2>📋 Polymarket Paper Copier</h2>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Enter</button>
  <div class="err">{error}</div>
</form></body></html>"""

_cache = {"ts": 0.0, "html": b""}


def http_get_json(url, params=None, timeout=8):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "paper-copier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def connect_db():
    """A DB connection that waits out the copier's writes instead of erroring."""
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def midpoint(token_id):
    try:
        return float(http_get_json(f"{CLOB_API}/midpoint", {"token_id": token_id})["mid"])
    except Exception:  # noqa: BLE001
        return None


def resolve_trader(raw):
    """Turn a profile URL / @username / 0x wallet into (wallet, name).

    Returns (wallet, name) on success or raises ValueError with a friendly msg.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter a Polymarket profile link, username, or 0x address.")

    # 1) A 0x address anywhere in the input wins (covers profile URLs too).
    m = re.search(r"0x[a-fA-F0-9]{40}", raw)
    if m:
        wallet = m.group(0).lower()
    else:
        # 2) Otherwise treat it as a username; scrape the profile page for its wallet.
        username = raw.split("/")[-1].lstrip("@").strip()
        if not username:
            raise ValueError("Couldn't read a username from that input.")
        try:
            req = urllib.request.Request(
                f"https://polymarket.com/@{urllib.parse.quote(username)}",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                         " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                page = r.read().decode(errors="replace")
        except Exception:  # noqa: BLE001
            raise ValueError(f"Couldn't load polymarket.com/@{username}.")
        hits = re.findall(r"0x[a-fA-F0-9]{40}", page)
        if not hits:
            raise ValueError(
                f"No wallet found for @{username} — paste their profile link instead.")
        wallet = max(set(hits), key=hits.count).lower()  # dominant address on the page

    # Look up the display name (best-effort; fall back to a short wallet label).
    name = None
    try:
        prof = http_get_json(f"{GAMMA_API}/public-profile", {"address": wallet})
        name = prof.get("name") or prof.get("pseudonym")
    except Exception:  # noqa: BLE001
        pass
    if not name:
        name = wallet[:6] + "…" + wallet[-4:]
    return wallet, name


def add_trader(raw):
    """Resolve and insert a trader. Returns a status message."""
    wallet, name = resolve_trader(raw)
    db = connect_db()
    try:
        existing = db.execute(
            "SELECT active FROM traders WHERE wallet=?", (wallet,)).fetchone()
        now = int(time.time())
        if existing is not None:
            db.execute(
                "UPDATE traders SET active=1, name=?, added_ts=? WHERE wallet=?",
                (name, now, wallet))
            msg = f"Re-activated {name}."
        else:
            db.execute(
                "INSERT INTO traders (wallet, name, added_ts, active) VALUES (?,?,?,1)",
                (wallet, name, now))
            msg = f"Now tailing {name}."
        # No backfill: copier starts this trader from now.
        db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"last_seen:{wallet}", str(now)))
        db.commit()
        return msg
    finally:
        db.close()


def set_trader_active(wallet, active):
    db = connect_db()
    try:
        db.execute("UPDATE traders SET active=? WHERE wallet=?",
                   (1 if active else 0, wallet.lower()))
        db.commit()
    finally:
        db.close()


COPY_RATIO = 1.0  # mirror copier.py sizing for the skipped-trades ghost portfolio
MIN_BET = 0.0

_mid_cache = {}  # asset -> (fetched_at, mid); for ghost assets we don't hold


def midpoint_cached(token_id):
    c = _mid_cache.get(token_id)
    if c and time.time() - c[0] < 300:
        return c[1]
    m = midpoint(token_id)
    _mid_cache[token_id] = (time.time(), m)
    return m


_hist_cache = {}  # asset -> (fetched_at, [(t, p), ...]); charts don't need 60s freshness
HIST_TTL = 600


def price_history(token_id, start_ts):
    """Price points from when we opened the position until now."""
    cached = _hist_cache.get(token_id)
    if cached and time.time() - cached[0] < HIST_TTL:
        return cached[1]
    if not start_ts:
        return []
    age = max(time.time() - start_ts, 60)
    # Finer resolution for fresh positions, coarser for old ones.
    fidelity = 1 if age < 3 * 3600 else 5 if age < 86400 else 15 if age < 3 * 86400 else 60
    try:
        hist = http_get_json(
            f"{CLOB_API}/prices-history",
            {"market": token_id, "startTs": int(start_ts), "fidelity": fidelity},
        ).get("history", [])
        points = [(h["t"], float(h["p"])) for h in hist]
    except Exception:  # noqa: BLE001
        points = cached[1] if cached else []
    _hist_cache[token_id] = (time.time(), points)
    return points


def sparkline(points, entry, current=None):
    """Tiny inline SVG of the price since we bought, dashed line = our entry."""
    if points and current is not None:
        points = points + [(int(time.time()), current)]  # end the line at "now"
    if len(points) < 2:
        return '<span class="dim">—</span>'
    ps = [p for _, p in points]
    lo, hi = min(ps + [entry]), max(ps + [entry])
    if hi - lo < 0.005:
        lo, hi = lo - 0.005, hi + 0.005
    w, h, pad = 110, 28, 2

    def y(p):
        return pad + (h - 2 * pad) * (1 - (p - lo) / (hi - lo))

    pts = " ".join(
        f"{pad + (w - 2 * pad) * i / (len(ps) - 1):.1f},{y(p):.1f}"
        for i, p in enumerate(ps)
    )
    color = "#4cc38a" if ps[-1] >= entry else "#f16a6a"
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<line x1="{pad}" y1="{y(entry):.1f}" x2="{w - pad}" y2="{y(entry):.1f}"'
        f' stroke="#667" stroke-dasharray="3,3" stroke-width="1"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/></svg>'
    )


def money(x):
    cls = "pos" if x >= 0 else "neg"
    sign = "+" if x >= 0 else "-"
    return f'<span class="{cls}">{sign}${abs(x):,.2f}</span>'


def pct(num, den):
    """num/den as a signed, colored percentage; dash if den is zero."""
    if not den:
        return '<span class="dim">—</span>'
    v = num / den * 100
    cls = "pos" if v >= 0 else "neg"
    sign = "+" if v >= 0 else "-"
    return f'<span class="{cls}">{sign}{abs(v):.2f}%</span>'


def esc(s):
    return html.escape(str(s or ""))


def px(p):
    """A price cell that JS can re-render as cents or American odds."""
    if p is None:
        return "—"
    return f'<span class="px" data-p="{p:.4f}">{p * 100:.1f}¢</span>'


# Plain string (not f-string) so JS braces don't need escaping.
SCRIPT = """
<script>
const dirState = {};
function rowsOf(name) {
  return Array.from(document.getElementById('table-' + name).rows).slice(1);
}
function cellKey(cell) {
  // Prefer a price span's underlying probability so sorting is consistent in
  // both ¢ and odds display modes.
  const px = cell.querySelector('.px');
  if (px && px.dataset.p !== undefined) return parseFloat(px.dataset.p);
  let t = (cell.textContent || '').trim();
  if (t === '' || t === '\\u2014') return null;  // blanks/dashes sort last
  // Date forms -> a comparable number.
  let dm = t.match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
  if (dm) return (+dm[1]) * 1e4 + (+dm[2]) * 1e2 + (+dm[3]);
  let md = t.match(/^(\\d{1,2})\\/(\\d{1,2})(?:\\s+(\\d{1,2}):(\\d{2}))?/);
  if (md) return (+md[1]) * 1e6 + (+md[2]) * 1e4 + (+(md[3]||0)) * 1e2 + (+(md[4]||0));
  // Numeric forms: money, prices, odds, counts. Strip $ , ¢ % + and parens;
  // turn the unicode minus and a leading "-" into a negative number.
  let n = t.replace(/[,$\\u00a2%()\\s]/g, '').replace(/\\u2212/g, '-').replace(/^\\+/, '');
  if (n !== '' && !isNaN(n)) return parseFloat(n);
  return t.toLowerCase();
}
function sortTable(name, idx, th) {
  const k = name + ':' + idx;
  dirState[k] = (dirState[k] === 1) ? -1 : 1;  // first click ascending, then toggle
  const dir = dirState[k];
  const rows = rowsOf(name).filter(r => !r.classList.contains('sumrow'));
  const sum = rowsOf(name).filter(r => r.classList.contains('sumrow'));
  const keyed = rows.map(r => [cellKey(r.cells[idx]), r]);
  const numeric = keyed.every(([v]) => v === null || typeof v === 'number');
  keyed.sort((a, b) => {
    let x = a[0], y = b[0];
    if (x === null) return 1;            // blanks always last
    if (y === null) return -1;
    if (numeric) return (x - y) * dir;
    return String(x).localeCompare(String(y)) * dir;
  });
  const tb = rows[0] ? rows[0].parentNode : null;
  if (tb) { keyed.forEach(([, r]) => tb.appendChild(r)); sum.forEach(r => tb.appendChild(r)); }
  const tbl = th.closest('table');
  tbl.rows[0].querySelectorAll('th').forEach(
    h => h.textContent = h.textContent.replace(/ [\\u25b2\\u25bc]$/, ''));
  th.textContent += dir > 0 ? ' \\u25b2' : ' \\u25bc';
  if (['open', 'closed', 'fills', 'skipped'].includes(name)) applyFilter(name);
}
function wireSortable() {
  document.querySelectorAll('table.sortable').forEach(tbl => {
    const name = tbl.id.replace(/^table-/, '');
    [...tbl.rows[0].cells].forEach((th, idx) => {
      th.style.cursor = 'pointer';
      th.title = 'Click to sort';
      th.addEventListener('click', () => sortTable(name, idx, th));
    });
  });
}
function gtrader() {
  const el = document.getElementById('gtrader');
  return el ? el.value : '';
}
function applyFilter(name) {
  const qEl = document.getElementById('q-' + name);
  const q = (qEl && qEl.value || '').toLowerCase();
  const tr = gtrader();
  const howEl = document.getElementById('fhow-' + name);
  const how = howEl ? howEl.value : '';
  rowsOf(name).forEach(r => {
    const show = (!q || (r.dataset.market || '').includes(q))
      && (!tr || r.dataset.trader === tr)
      && (!how || r.dataset.how === how);
    r.style.display = show ? '' : 'none';
  });
}
function applyTrader() {
  localStorage.setItem('gtrader', gtrader());
  ['open', 'closed', 'fills', 'skipped'].forEach(applyFilter);
}
function showTab(name) {
  const pane = document.getElementById('pane-' + name);
  if (!pane) name = 'open';
  document.querySelectorAll('.tabpane').forEach(d => d.style.display = 'none');
  document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('active'));
  document.getElementById('pane-' + name).style.display = '';
  document.getElementById('tb-' + name).classList.add('active');
  localStorage.setItem('tab', name);
}
function fmtAmerican(p) {
  // Settled prices (0 or 1) have no betting line — show them as cents.
  if (p <= 0.001) return '0\\u00a2';
  if (p >= 0.999) return '100\\u00a2';
  return p < 0.5 ? '+' + Math.round(100 * (1 - p) / p) : '\\u2212' + Math.round(100 * p / (1 - p));
}
function applyOdds() {
  const us = localStorage.getItem('odds') === 'us';
  document.querySelectorAll('.px').forEach(el => {
    const p = parseFloat(el.dataset.p);
    el.textContent = us ? fmtAmerican(p) : (p * 100).toFixed(1) + '\\u00a2';
  });
  const btn = document.getElementById('oddsbtn');
  if (btn) btn.textContent = us ? 'Show \\u00a2' : 'Show +/\\u2212 odds';
}
function toggleOdds() {
  localStorage.setItem('odds', localStorage.getItem('odds') === 'us' ? 'cents' : 'us');
  applyOdds();
}
function filterTDaily() {
  const v = document.getElementById('fdate-tdaily').value;
  Array.from(document.getElementById('table-tdaily').rows).slice(1).forEach(r => {
    r.style.display = (r.dataset.date === v) ? '' : 'none';
  });
}
function showBT(mode) {
  const day = mode === 'day';
  document.getElementById('trader-totals').style.display = day ? 'none' : '';
  document.getElementById('trader-daily').style.display = day ? '' : 'none';
  document.getElementById('bt-totals').classList.toggle('active', !day);
  document.getElementById('bt-day').classList.toggle('active', day);
  localStorage.setItem('btmode', mode);
}
wireSortable();
applyOdds();
filterTDaily();
showBT(localStorage.getItem('btmode') === 'day' ? 'day' : 'totals');
(function restoreTrader() {
  const saved = localStorage.getItem('gtrader');
  const el = document.getElementById('gtrader');
  if (saved && el && [...el.options].some(o => o.value === saved)) el.value = saved;
  ['open', 'closed', 'fills', 'skipped'].forEach(applyFilter);
})();
showTab(localStorage.getItem('tab') || 'open');
</script>"""


def render():
    # Bound the long-lived price caches so they can't grow without limit on the
    # small box (they refill within a render or two).
    if len(_mid_cache) > 4000:
        _mid_cache.clear()
    if len(_hist_cache) > 4000:
        _hist_cache.clear()
    db = connect_db()
    now = time.strftime("%a %b %-d, %-I:%M %p")

    start = db.execute("SELECT value FROM meta WHERE key='start_ts'").fetchone()
    started = time.strftime("%b %-d", time.localtime(int(start[0]))) if start else "?"
    n_fills = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_skipped = db.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]

    # Currently-tailed traders (for the Manage traders panel).
    try:
        active_traders = db.execute(
            "SELECT wallet, name, added_ts FROM traders WHERE active=1 ORDER BY added_ts"
        ).fetchall()
    except sqlite3.OperationalError:
        active_traders = []
    manage_rows = "".join(
        f'<tr><td>{esc(nm)}</td>'
        f'<td class="dim">{esc(w[:10])}…{esc(w[-4:])}</td>'
        f'<td class="dim">{time.strftime("%b %-d", time.localtime(a)) if a else "—"}</td>'
        f'<td><form method="POST" action="/remove-trader" style="margin:0"'
        f' onsubmit="return confirm(\'Stop tailing {esc(nm)}? Existing positions stay.\')">'
        f'<input type="hidden" name="wallet" value="{esc(w)}">'
        f'<button class="rm">stop</button></form></td></tr>'
        for w, nm, a in active_traders
    ) or '<tr><td colspan="4" class="dim">none</td></tr>'

    # Per-trader realized + unrealized
    realized = defaultdict(float)
    for trader, pnl in db.execute("SELECT trader, COALESCE(SUM(pnl),0) FROM closed GROUP BY trader"):
        realized[trader] = pnl
    closed_stats = {
        t: (w, n) for t, w, n in db.execute(
            "SELECT trader, SUM(pnl >= 0), COUNT(*) FROM closed GROUP BY trader")
    }
    # Total $ wagered = sum of every copied BUY's cost, per trader (open + closed).
    wagered = defaultdict(float)
    for t, w in db.execute(
            "SELECT trader, COALESCE(SUM(usdc),0) FROM trades WHERE side='BUY' GROUP BY trader"):
        wagered[t] = w

    positions = db.execute(
        "SELECT trader, title, outcome, asset, shares, avg_cost, their_avg, event_slug"
        " FROM positions ORDER BY shares * avg_cost DESC"
    ).fetchall()
    slug_by_asset = {p[3]: p[7] for p in positions if p[7]}
    # First/last buy time + fill count per asset, to timestamp each position.
    fill_times = {
        asset: (first, last, n) for asset, first, last, n in db.execute(
            "SELECT asset, MIN(ts), MAX(ts), COUNT(*) FROM trades"
            " WHERE side='BUY' GROUP BY asset")
    }
    # Fetch all live prices + since-open history in parallel
    # (sequential would take ~30s at 190+ positions).
    assets = [p[3] for p in positions]
    with ThreadPoolExecutor(max_workers=16) as pool:
        mids = dict(zip(assets, pool.map(midpoint, assets)))
        hists = dict(zip(
            assets,
            pool.map(lambda a: price_history(a, fill_times.get(a, (None,))[0]), assets),
        ))
    unrealized = defaultdict(float)
    cost_open = defaultdict(float)
    pos_rows = []
    for trader, title, outcome, asset, shares, avg_cost, their_avg, event_slug in positions:
        mid = mids.get(asset)
        cost = shares * avg_cost
        cost_open[trader] += cost
        pnl = shares * (mid - avg_cost) if mid is not None else 0.0
        unrealized[trader] += pnl
        first, last, n = fill_times.get(asset, (None, None, 0))
        if first is None:
            opened = "—"
        else:
            opened = time.strftime("%m/%d %H:%M", time.localtime(first))
            if n > 1:
                opened += f' <span class="dim">(+{n - 1} fills)</span>'
        market = f"{esc(title)}: <b>{esc(outcome)}</b>"
        if event_slug:
            market = (
                f'<a href="https://polymarket.com/event/{esc(event_slug)}"'
                f' target="_blank">{market} ↗</a>'
            )
        pos_rows.append(
            f'<tr data-trader="{esc(trader)}" data-market="{esc((title + " " + outcome).lower())}"'
            f' data-opened="{first or 0}" data-cost="{cost:.4f}" data-pnl="{pnl:.4f}">'
            f"<td>{esc(trader)}</td><td>{market}</td>"
            f"<td>{opened}</td>"
            f"<td>{px(their_avg or avg_cost)}</td><td>{px(avg_cost)}</td>"
            f"<td>{px(mid)}</td>"
            f"<td>{sparkline(hists.get(asset, []), avg_cost, mid)}</td>"
            f"<td>${cost:,.2f}</td><td>{money(pnl)}</td></tr>"
        )

    # Ghost portfolio (what skipped BUYs would be worth) — aggregated in SQL per
    # (trader, asset) to keep memory bounded; the raw skipped table is ~53k rows.
    # cb_their = Σ their_shares·their_price; cb_mkt = Σ their_shares·market_price.
    ghost_groups = db.execute(
        "SELECT trader, asset, SUM(their_shares), SUM(their_shares*their_price),"
        " SUM(their_shares*market_price), COUNT(*) FROM skipped"
        " WHERE side='BUY' AND market_price IS NOT NULL AND their_shares>0"
        " GROUP BY trader, asset"
    ).fetchall()
    exit_by_asset = dict(db.execute("SELECT asset, exit_price FROM closed"))
    # Settled assets price for free from our closed table; only unsettled ones
    # need a live midpoint — cap those fetches so render stays light on the box.
    GHOST_FETCH_CAP = 400
    to_fetch = sorted(
        {g[1] for g in ghost_groups if g[1] not in exit_by_asset and g[1] not in mids}
    )[:GHOST_FETCH_CAP]
    with ThreadPoolExecutor(max_workers=16) as pool:
        ghost_mids = dict(zip(to_fetch, pool.map(midpoint_cached, to_fetch)))

    def cur_price(a):
        if a in exit_by_asset:
            return exit_by_asset[a]
        m = mids.get(a)
        return m if m is not None else ghost_mids.get(a)

    g_invested = g_value = 0.0
    g_priced = g_unpriced = total_ghost = 0
    skipped_pnl_t = defaultdict(float)
    skipped_n_t = defaultdict(int)
    skip_r = skip_u = 0.0  # settled vs still-live portion of skipped P&L
    for g_trader, a, tsh, cb_their, cb_mkt, n in ghost_groups:
        total_ghost += n
        cur = cur_price(a)
        if cur is None:
            g_unpriced += n
            continue
        g_invested += cb_mkt
        g_value += tsh * cur
        g_priced += n
        pnl_val = tsh * cur - cb_their
        skipped_pnl_t[g_trader] += pnl_val
        skipped_n_t[g_trader] += n
        if a in exit_by_asset:
            skip_r += pnl_val
        else:
            skip_u += pnl_val
    g_pnl = g_value - g_invested

    traders = sorted(set(list(realized) + list(unrealized) + list(skipped_pnl_t)))
    trader_rows = []
    total_r = total_u = 0.0
    wins_tot = closed_tot = 0
    for t in traders:
        r, u = realized[t], unrealized[t]
        total_r += r
        total_u += u
        wins, closed_n = closed_stats.get(t, (0, 0))
        wins_tot += int(wins or 0)
        closed_tot += closed_n
        wr = f"{int(wins)}/{closed_n}" if closed_n else "—"
        sp = skipped_pnl_t.get(t, 0.0)
        sp_cell = (
            f"{money(sp)} <span class=\"dim\">({skipped_n_t[t]})</span>"
            if skipped_n_t.get(t) else '<span class="dim">—</span>'
        )
        trader_rows.append(
            f"<tr><td>{esc(t)}</td><td>${wagered[t]:,.2f}</td>"
            f"<td>${cost_open[t]:,.2f}</td><td>{money(r)}</td>"
            f"<td>{money(u)}</td><td><b>{money(r + u)}</b></td>"
            f"<td>{pct(r + u, wagered[t])}</td>"
            f"<td>{sp_cell}</td><td><b>{money(r + u + sp)}</b></td><td>{wr}</td></tr>"
        )
    grand = total_r + total_u
    skip_total = skip_r + skip_u
    trader_rows.append(
        f'<tr class="sumrow" style="border-top:2px solid #3a4254"><td><b>All</b></td>'
        f"<td><b>${sum(wagered.values()):,.2f}</b></td>"
        f"<td><b>${sum(cost_open.values()):,.2f}</b></td><td><b>{money(total_r)}</b></td>"
        f"<td><b>{money(total_u)}</b></td><td><b>{money(grand)}</b></td>"
        f"<td><b>{pct(grand, sum(wagered.values()))}</b></td>"
        f"<td><b>{money(skip_total)}</b></td><td><b>{money(grand + skip_total)}</b></td>"
        f"<td><b>{wins_tot}/{closed_tot}</b></td></tr>"
    )

    # Keep today's snapshot rows live (the copier seeds them once at midnight;
    # we overwrite with fresh numbers each render, so the last write of the
    # day naturally becomes that day's end-of-day record).
    try:
        today = time.strftime("%Y-%m-%d")
        now_ts = int(time.time())
        fills_t = dict(db.execute("SELECT trader, COUNT(*) FROM trades GROUP BY trader"))
        open_t = defaultdict(int)
        for p in positions:
            open_t[p[0]] += 1
        for t in traders:
            r, u = realized[t], unrealized[t]
            db.execute(
                "INSERT OR REPLACE INTO trader_snapshots (date, trader, ts, realized,"
                " unrealized, total, open_count, fills) VALUES (?,?,?,?,?,?,?,?)",
                (today, t, now_ts, r, u, r + u, open_t[t], fills_t.get(t, 0)),
            )
        db.execute(
            "INSERT OR REPLACE INTO snapshots (date, ts, realized, unrealized, total,"
            " open_count, fills) VALUES (?,?,?,?,?,?,?)",
            (today, now_ts, total_r, total_u, grand, len(positions), n_fills),
        )
        db.commit()
    except sqlite3.OperationalError:
        pass  # table missing or db briefly locked — next render will catch up

    # Per-trader DAILY activity — what happened ON each day, not cumulative.
    # Realized/wagered/fills come straight from the raw tables (accurate for the
    # whole history); the "Day Δ" swing (which includes open-position mark-to-
    # market moves) comes from end-of-day snapshots where we have them.
    banked_day = {(d, t): (p, n) for d, t, p, n in db.execute(
        "SELECT date(ts,'unixepoch','localtime') d, trader, COALESCE(SUM(pnl),0), COUNT(*)"
        " FROM closed GROUP BY d, trader")}
    trade_day = {(d, t): (w, n) for d, t, w, n in db.execute(
        "SELECT date(ts,'unixepoch','localtime') d, trader,"
        " COALESCE(SUM(CASE WHEN side='BUY' THEN usdc ELSE 0 END),0), COUNT(*)"
        " FROM trades GROUP BY d, trader")}
    totals_by_trader = defaultdict(dict)
    for d, t, tot in db.execute("SELECT date, trader, total FROM trader_snapshots"):
        totals_by_trader[t][d] = tot

    def day_delta(d, t):
        days = totals_by_trader.get(t, {})
        if d not in days:
            return None
        prev = [x for x in sorted(days) if x < d]
        return days[d] - (days[prev[-1]] if prev else 0.0)

    keys = set(banked_day) | set(trade_day) | {
        (d, t) for t in totals_by_trader for d in totals_by_trader[t]}
    daily_trader_rows = []
    for d, t in sorted(keys, key=lambda k: (k[0], banked_day.get(k, (0,))[0]), reverse=True):
        bp, bn = banked_day.get((d, t), (0.0, 0))
        wg, fn = trade_day.get((d, t), (0.0, 0))
        delta = day_delta(d, t)
        daily_trader_rows.append(
            f'<tr data-date="{esc(d)}"><td>{esc(t)}</td>'
            f"<td>{money(bp)}</td>"
            f"<td>{money(delta) if delta is not None else '<span class=dim>—</span>'}</td>"
            f"<td>${wg:,.2f}</td><td>{bn}</td><td>{fn}</td></tr>"
        )
    tdaily_dates = sorted({k[0] for k in keys}, reverse=True)

    snap_rows = []
    prev_total = 0.0
    for date, total, realized_s, open_count, fills in db.execute(
        "SELECT date, total, realized, open_count, fills FROM snapshots ORDER BY date"
    ):
        day_change = total - prev_total
        snap_rows.append(
            f"<tr><td>{esc(date)}</td><td><b>{money(total)}</b></td><td>{money(day_change)}</td>"
            f"<td>{money(realized_s)}</td><td>{open_count}</td><td>{fills}</td></tr>"
        )
        prev_total = total
    snap_rows.reverse()  # newest first

    n_closed = db.execute("SELECT COUNT(*) FROM closed").fetchone()[0]
    closed_traders = sorted(
        t for (t,) in db.execute("SELECT DISTINCT trader FROM closed")
    )
    # Render the most recent MAX_CLOSED rows (capped to keep memory bounded on
    # the 512MB box — the full closed table is ~26k rows). Charts only for the
    # most recent CHART_CLOSED; older rows show a cheap entry→exit text arrow.
    CHART_CLOSED = 150
    MAX_CLOSED = 1500
    closed_list = db.execute(
        "SELECT ts, trader, title, outcome, shares, avg_cost, exit_price, pnl,"
        " reason, event_slug, asset, their_avg FROM closed ORDER BY ts DESC LIMIT ?",
        (MAX_CLOSED,)
    ).fetchall()
    chart_assets = [r[10] for r in closed_list[:CHART_CLOSED]]
    with ThreadPoolExecutor(max_workers=16) as pool:
        closed_hists = dict(zip(
            chart_assets,
            pool.map(
                lambda a: price_history(a, fill_times.get(a, (None,))[0]), chart_assets
            ),
        ))
    closed_rows = []
    for i, (ts, trader, title, outcome, shares, avg_cost, exit_price, pnl, reason, ev_slug, asset, their_avg) in enumerate(closed_list):
        when = time.strftime("%m/%d %H:%M", time.localtime(ts))
        market = f"{esc(title)}: <b>{esc(outcome)}</b>"
        if ev_slug:
            market = (
                f'<a href="https://polymarket.com/event/{esc(ev_slug)}"'
                f' target="_blank">{market} ↗</a>'
            )
        how = "🏁 resolved" if reason == "resolved" else "💸 sold"
        if i < CHART_CLOSED:
            # Price path from our entry until the close — ends at the exit price.
            life = [(t, p) for t, p in closed_hists.get(asset, []) if t <= ts]
            chart = sparkline(life, avg_cost, exit_price)
        else:
            arrow = "↗" if exit_price >= avg_cost else "↘"
            cls = "pos" if exit_price >= avg_cost else "neg"
            chart = f'<span class="{cls}">{avg_cost * 100:.0f}¢ {arrow} {exit_price * 100:.0f}¢</span>'
        cost = shares * avg_cost
        his = their_avg if their_avg is not None else avg_cost
        closed_rows.append(
            f'<tr data-trader="{esc(trader)}" data-market="{esc((title + " " + outcome).lower())}"'
            f' data-opened="{ts}" data-pnl="{pnl:.4f}" data-cost="{cost:.4f}" data-how="{esc(reason)}">'
            f"<td>{when}</td><td>{esc(trader)}</td><td>{market}</td>"
            f"<td>{px(his)}</td><td>{px(avg_cost)}</td><td>{px(exit_price)}</td><td>{chart}</td>"
            f"<td>{shares:,.2f}</td><td>${cost:,.2f}</td><td>{money(pnl)}</td><td>{how}</td></tr>"
        )

    # Skipped trades (slippage cap), latest 100 only.
    skipped_list = db.execute(
        "SELECT ts, trader, side, their_shares, their_price, market_price, title,"
        " outcome, event_slug, asset FROM skipped ORDER BY COALESCE(ts, 0) DESC LIMIT 100"
    ).fetchall()
    # Recover market names for old (pre-title) rows — targeted lookup, not a
    # full scan of the 700k-row trades table.
    need = [r[9] for r in skipped_list if not r[6]]
    asset_info = {}
    if need:
        ph = ",".join("?" * len(need))
        for a, t, o in db.execute(
            f"SELECT asset, title, outcome FROM trades WHERE asset IN ({ph}) GROUP BY asset",
            need):
            asset_info[a] = (t, o)

    skipped_rows = []
    for ts, trader, side, their_shares, their_price, mkt_price, title, outcome, ev_slug, a in skipped_list:
        if not title and a in asset_info:
            title, outcome = asset_info[a]
        when = time.strftime("%m/%d %H:%M", time.localtime(ts)) if ts else "—"
        market = f"{esc(title) or '(unknown market)'}: <b>{esc(outcome)}</b>"
        if ev_slug:
            market = (
                f'<a href="https://polymarket.com/event/{esc(ev_slug)}"'
                f' target="_blank">{market} ↗</a>'
            )
        if their_price is not None and mkt_price is not None:
            slip = (mkt_price - their_price) if side == "BUY" else (their_price - mkt_price)
            slip_s = f'<span class="neg">+{slip * 100:.1f}¢</span>'
        else:
            slip_s = "—"
        gcell = '<span class="dim">—</span>'
        if side == "BUY" and mkt_price is not None:
            cur = cur_price(a)
            if cur is not None:
                gs = max(their_shares * COPY_RATIO, MIN_BET / mkt_price)
                gcell = money(gs * (cur - mkt_price))
        skipped_rows.append(
            f'<tr data-trader="{esc(trader or "")}"'
            f' data-market="{esc(((title or "") + " " + (outcome or "")).lower())}"'
            f' data-opened="{ts or 0}">'
            f"<td>{when}</td><td>{esc(trader) or '—'}</td>"
            f'<td>{esc(side)}</td><td>{market}</td>'
            f"<td>{px(their_price)}</td><td>{px(mkt_price)}</td><td>{slip_s}</td>"
            f"<td>{their_shares:,.2f}</td><td>{gcell}</td></tr>"
        )
    skipped_traders = sorted(
        t for (t,) in db.execute(
            "SELECT DISTINCT trader FROM skipped WHERE trader IS NOT NULL AND trader != ''")
    )

    n_fills_tbl = 300
    fill_rows = []
    for ts, trader, side, shares, price, our_price, title, outcome, asset in db.execute(
        "SELECT ts, trader, side, our_shares, price, our_price, title, outcome, asset"
        " FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (n_fills_tbl,)
    ):
        when = time.strftime("%m/%d %H:%M", time.localtime(ts))
        op = our_price if our_price is not None else price
        side_cls = "pos" if side == "BUY" else "neg"
        market = f"{esc(title)}: <b>{esc(outcome)}</b>"
        if asset in slug_by_asset:
            market = (
                f'<a href="https://polymarket.com/event/{esc(slug_by_asset[asset])}"'
                f' target="_blank">{market} ↗</a>'
            )
        fill_rows.append(
            f'<tr data-trader="{esc(trader)}"'
            f' data-market="{esc(((title or "") + " " + (outcome or "")).lower())}">'
            f"<td>{when}</td><td>{esc(trader)}</td>"
            f'<td class="{side_cls}">{esc(side)}</td><td>{shares:,.2f}</td>'
            f"<td>{px(price)}</td><td>{px(op)}</td>"
            f"<td>{market}</td></tr>"
        )

    # Full trader list for the shared filter (anyone with any activity + active).
    all_traders = sorted({
        r[0] for r in db.execute("SELECT DISTINCT trader FROM trades")
    } | {
        r[0] for r in db.execute("SELECT name FROM traders")
    } - {None, ""})

    db.close()
    rows_or_empty = lambda rows, cols: "".join(rows) or f'<tr><td colspan="{cols}" class="dim">nothing yet</td></tr>'
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Polymarket Paper Copier</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; background:#0f1115; color:#e6e6e6;
        margin:0 auto; max-width:980px; padding:16px; }}
 h1 {{ font-size:1.3em; }} h2 {{ font-size:1.05em; margin-top:28px; color:#9ad; }}
 .big {{ font-size:2.4em; font-weight:700; margin:8px 0; }}
 .pos {{ color:#4cc38a; }} .neg {{ color:#f16a6a; }} .dim {{ color:#888; }}
 table {{ border-collapse:collapse; width:100%; font-size:0.85em; }}
 th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #23262e; }}
 th {{ color:#789; font-weight:600; white-space:nowrap; }}
 table.sortable th:hover {{ color:#cde; background:#1d2330; }}
 tr:hover {{ background:#171a21; }}
 a {{ color:#8ab4f8; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 .controls {{ margin:8px 0 12px; display:flex; flex-wrap:wrap; gap:6px; align-items:center;
             color:#789; font-size:0.9em; }}
 .controls input, .controls select {{ background:#171a21; color:#e6e6e6; border:1px solid #333;
             border-radius:6px; padding:6px 10px; font-size:1em; }}
 .controls button {{ background:#1d2330; color:#9ad; border:1px solid #2a3242; border-radius:6px;
             padding:6px 10px; cursor:pointer; font-size:1em; }}
 .controls button:hover {{ background:#243049; }}
 .tabbar {{ display:flex; gap:6px; align-items:center; margin-top:28px;
           border-bottom:1px solid #2a3242; padding-bottom:0; }}
 .tabbtn {{ background:#171a21; color:#9ad; border:1px solid #2a3242; border-bottom:none;
           border-radius:8px 8px 0 0; padding:9px 18px; cursor:pointer; font-size:1em; }}
 .tabbtn.active {{ background:#243049; color:#fff; font-weight:600; }}
 .oddstoggle {{ background:#1d2330; color:#9ad; border:1px solid #2a3242;
               border-radius:6px; padding:6px 10px; cursor:pointer; font-size:0.9em; }}
 .gtrader {{ margin-left:auto; background:#1d2330; color:#9ad; border:1px solid #2a3242;
            border-radius:6px; padding:6px 10px; font-size:0.9em; }}
 .tabpane {{ padding-top:12px; }}
 .tabbar.sub {{ margin-top:8px; border-bottom:none; }}
 .tabbar.sub .tabbtn {{ padding:5px 14px; border-radius:6px; border:1px solid #2a3242;
                       font-size:0.9em; }}
 .manage {{ margin-top:16px; background:#171a21; border:1px solid #2a3242;
           border-radius:8px; padding:10px 14px; }}
 .manage summary {{ cursor:pointer; color:#9ad; font-weight:600; }}
 .addform {{ margin:12px 0; display:flex; gap:8px; flex-wrap:wrap; }}
 .addform input {{ flex:1; min-width:240px; background:#0f1115; color:#e6e6e6;
                  border:1px solid #333; border-radius:6px; padding:8px 12px; font-size:1em; }}
 .addform button {{ background:#2563eb; color:#fff; border:none; border-radius:6px;
                   padding:8px 16px; cursor:pointer; font-size:1em; }}
 .mtable {{ font-size:0.85em; }}
 button.rm {{ background:#3a2330; color:#f99; border:1px solid #5a2a3a; border-radius:5px;
             padding:3px 10px; cursor:pointer; }}
 button.rm:hover {{ background:#5a2a3a; }}
</style></head><body>
<h1>📋 Polymarket Paper Copier</h1>
<!--MSG-->
<div class="dim">Paper-trading since {started} · {n_fills} fills copied · {n_skipped} skipped (slippage) · updated {now} · auto-refreshes</div>
<div class="big">{money(grand)} <span class="dim" style="font-size:0.4em">copied</span></div>
<div>banked {money(total_r)} · open bets {money(total_u)}</div>
<div style="margin-top:4px">incl. missed (skips): <b>{money(grand + skip_total)}</b>
<span class="dim">(banked {money(total_r + skip_r)} · open {money(total_u + skip_u)} — as if every trade were copied at the trader's price)</span></div>

<details class="manage">
<summary>⚙️ Manage traders ({len(active_traders)})</summary>
<form method="POST" action="/add-trader" class="addform">
  <input name="trader" placeholder="Profile link, @username, or 0x address…" autocomplete="off">
  <button type="submit">Add trader</button>
</form>
<table class="mtable"><tr><th>Trader</th><th>Wallet</th><th>Added</th><th></th></tr>
{manage_rows}</table>
<div class="dim" style="margin-top:6px">New traders are tailed from the moment you add them (no backfill). "Stop" pauses copying their future trades; positions you already hold are untouched.</div>
</details>

<h2>By trader</h2>
<div class="tabbar sub">
  <button class="tabbtn" id="bt-totals" onclick="showBT('totals')">Totals</button>
  <button class="tabbtn" id="bt-day" onclick="showBT('day')">By day</button>
</div>
<div id="trader-totals">
<table id="table-traders" class="sortable"><tr><th>Trader</th><th>Wagered</th><th>$ in play</th><th>Banked</th><th>Open bets ±</th><th>Your P&amp;L</th><th>Return</th><th>Missed (skips)</th><th>Trader's true P&amp;L</th><th>Record</th></tr>
{rows_or_empty(trader_rows, 10)}</table>
<div class="dim" style="margin-top:6px; line-height:1.6">
<b>Wagered</b> — total $ put into buys over all time (open + closed) ·
<b>Return</b> — Your P&amp;L ÷ Wagered, i.e. profit per dollar wagered (turnover is huge with $-for-$ copying, so this runs small) ·
<b>$ in play</b> — money currently tied up in open bets ·
<b>Banked</b> — won/lost on finished bets (final) ·
<b>Open bets ±</b> — how open bets stand right now (can still change) ·
<b>Your P&amp;L</b> — Banked + Open bets ± = what copying actually got you ·
<b>Missed (skips)</b> — what the trades we skipped (slippage cap) would have added, at the trader's price ·
<b>Trader's true P&amp;L</b> — Your P&amp;L + Missed = their performance if nothing were skipped ·
<b>Record</b> — wins/losses on finished bets
</div>
</div>
<div id="trader-daily" style="display:none">
<div class="controls">
  <select id="fdate-tdaily" onchange="filterTDaily()">
    {''.join(f'<option value="{esc(d)}">{esc(d)}</option>' for d in tdaily_dates)}
  </select>
  <span class="dim">What each trader did <b>on that day</b> (today is live). "Banked" = P&amp;L from bets that closed that day; "Day Δ" = total P&amp;L swing that day incl. open-position moves.</span>
</div>
<table id="table-tdaily" class="sortable"><tr><th>Trader</th><th>Banked that day</th><th>Day Δ (incl. open)</th><th>Wagered that day</th><th>Closes</th><th>Fills</th></tr>
{rows_or_empty(daily_trader_rows, 6)}</table>
</div>

<h2>Daily history</h2>
<table><tr><th>Date</th><th>Total P&amp;L</th><th>Day change</th><th>Banked</th><th># open</th><th>Fills</th></tr>
{rows_or_empty(snap_rows, 6)}</table>

<div class="tabbar">
  <button class="tabbtn" id="tb-open" onclick="showTab('open')">Open ({len(positions)})</button>
  <button class="tabbtn" id="tb-closed" onclick="showTab('closed')">Closed ({n_closed})</button>
  <button class="tabbtn" id="tb-fills" onclick="showTab('fills')">Recent fills</button>
  <button class="tabbtn" id="tb-skipped" onclick="showTab('skipped')">Skipped ({n_skipped})</button>
  <select id="gtrader" class="gtrader" onchange="applyTrader()" title="Filter all tabs by trader">
    <option value="">All traders</option>
    {''.join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in all_traders)}
  </select>
  <button id="oddsbtn" class="oddstoggle" onclick="toggleOdds()">Show +/− odds</button>
</div>

<div class="tabpane" id="pane-open">
<div class="controls">
  <input id="q-open" placeholder="Search markets…" oninput="applyFilter('open')">
  <span class="dim">click a column header to sort ↕</span>
</div>
<table id="table-open" class="sortable"><tr><th>Trader</th><th>Market</th><th>Opened</th><th>His entry</th><th>Yours</th><th>Now</th><th>Since open</th><th>Cost</th><th>Unrealized</th></tr>
{rows_or_empty(pos_rows, 9)}</table>
<div class="dim" style="margin-top:6px"><b>Unrealized</b> here = what you'd be up/down if the bet ended at today's price — nothing is final until the market resolves.</div>
</div>

<div class="tabpane" id="pane-closed">
<div class="dim" style="margin:8px 0">Showing the most recent {min(1500, n_closed):,} of {n_closed:,} closed positions. Mini-charts for the latest {min(150, n_closed)}; older rows show entry→exit.</div>
<div class="controls">
  <input id="q-closed" placeholder="Search markets…" oninput="applyFilter('closed')">
  <select id="fhow-closed" onchange="applyFilter('closed')">
    <option value="">All exits</option>
    <option value="resolved">🏁 resolved</option>
    <option value="sell">💸 sold</option>
  </select>
  <span class="dim">click a column header to sort ↕</span>
</div>
<table id="table-closed" class="sortable"><tr><th>Closed</th><th>Trader</th><th>Market</th><th>His entry</th><th>Your entry</th><th>Exit</th><th>Entry → Exit</th><th>Shares</th><th>Cost</th><th>P&amp;L</th><th>How</th></tr>
{rows_or_empty(closed_rows, 11)}</table>
</div>

<div class="tabpane" id="pane-fills">
<div class="dim" style="margin:8px 0">Latest {n_fills_tbl} copied fills{' (use the trader filter above to focus on one)' if all_traders else ''}.</div>
<table id="table-fills" class="sortable"><tr><th>When</th><th>Trader</th><th>Side</th><th>Shares</th><th>His</th><th>Yours</th><th>Market</th></tr>
{rows_or_empty(fill_rows, 7)}</table>
</div>

<div class="tabpane" id="pane-skipped">
<div class="dim" style="margin:8px 0">Trades the bot did NOT copy because the live price had already moved more than 5¢ past the trader's fill — like a limit order that didn't fill.{f' Showing latest 100 of {n_skipped}.' if n_skipped > 100 else ''}</div>
<div style="margin:8px 0; padding:10px 14px; background:#171a21; border:1px solid #2a3242; border-radius:8px">
  <b>Ghost portfolio</b> — if the bot had taken the skipped buys anyway (at the worse price):
  invested ${g_invested:,.2f} → now worth ${g_value:,.2f} = <b>{money(g_pnl)}</b>
  <span class="dim">({g_priced:,} of {total_ghost:,} skipped buys priced{f', {g_unpriced:,} still-open ones not yet priced' if g_unpriced else ''}; in other words, the 5¢ cap has {'SAVED you ' + money(-g_pnl) if g_pnl < 0 else 'COST you ' + money(g_pnl)} so far)</span>
</div>
<div class="controls">
  <input id="q-skipped" placeholder="Search markets…" oninput="applyFilter('skipped')">
  <span class="dim">click a column header to sort ↕</span>
</div>
<table id="table-skipped" class="sortable"><tr><th>When</th><th>Trader</th><th>Side</th><th>Market</th><th>His price</th><th>Market was at</th><th>Slip</th><th>His shares</th><th>Ghost P&amp;L</th></tr>
{rows_or_empty(skipped_rows, 9)}</table>
</div>
{SCRIPT}
</body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "auth" and hmac.compare_digest(value, TOKEN):
                return True
        return False

    def _send_html(self, body, status=200, headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 8192)
        return urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))

    def _redirect(self, msg=""):
        url = "/?msg=" + urllib.parse.quote(msg) if msg else "/"
        self._send_html(f'<meta http-equiv="refresh" content="0;url={esc(url)}">')

    def do_POST(self):  # noqa: N802
        if self.path == "/login":
            form = self._read_form()
            if hmac.compare_digest(form.get("password", [""])[0], PASSWORD):
                self._send_html(
                    '<meta http-equiv="refresh" content="0;url=/">',
                    headers=[(
                        "Set-Cookie",
                        f"auth={TOKEN}; HttpOnly; Max-Age=31536000; SameSite=Lax; Path=/",
                    )],
                )
            else:
                self._send_html(LOGIN_PAGE.format(error="Wrong password"), status=401)
            return

        # Everything below requires a logged-in cookie.
        if not self._authorized():
            self._send_html(LOGIN_PAGE.format(error=""), status=401)
            return

        if self.path == "/add-trader":
            form = self._read_form()
            try:
                msg = add_trader(form.get("trader", [""])[0])
            except ValueError as e:
                msg = f"⚠️ {e}"
            except Exception as e:  # noqa: BLE001
                msg = f"⚠️ Couldn't add trader: {e}"
            _cache["ts"] = 0  # force rebuild so the list updates immediately
            self._redirect(msg)
            return

        if self.path == "/remove-trader":
            form = self._read_form()
            wallet = form.get("wallet", [""])[0]
            set_trader_active(wallet, False)
            _cache["ts"] = 0
            self._redirect("Stopped tailing that trader (their existing positions stay).")
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self._send_html(LOGIN_PAGE.format(error=""), status=401)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        # Always serve the pre-rendered page; the refresher thread keeps it fresh.
        body = _cache["html"] or b"<h1>warming up\xe2\x80\xa6 refresh in a few seconds</h1>"
        msg = urllib.parse.parse_qs(parsed.query).get("msg", [""])[0]
        if msg:
            banner = (
                '<div style="margin:8px 0;padding:10px 14px;background:#1d2e22;'
                'border:1px solid #2f5; border-radius:8px">' + esc(msg) + "</div>"
            )
            body = body.replace(b"<!--MSG-->", banner.encode(), 1)
        self._send_html(body)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


def refresher():
    """Re-render the page every CACHE_SECONDS so requests never wait on it."""
    while True:
        try:
            _cache["html"] = render()
            _cache["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            if not _cache["html"]:
                _cache["html"] = f"<h1>dashboard error: {esc(e)}</h1>".encode()
        time.sleep(CACHE_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
