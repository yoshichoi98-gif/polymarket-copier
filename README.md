# Polymarket Paper Copy-Trader

A zero-risk **paper-trading** bot that tails chosen Polymarket traders and
simulates copying their bets — no wallet, no private keys, no real money.
It records every simulated fill in a local SQLite ledger and serves a live
web dashboard of P&L, per-trader breakdowns, open/closed positions, and more.

## Pieces

| File | What it does |
|------|--------------|
| `copier.py` | The watcher. Polls each tracked trader's public activity feed every ~1s and mirrors new trades into `ledger.db` at the live price (skipping fills where the market has already moved past a slippage cap). Settles positions when markets resolve. |
| `dashboard.py` | Password-protected web dashboard (stdlib `http.server`). Overall P&L, by-trader table (with return %, wagered, skipped-trade "ghost" P&L), by-day view, and sortable/filterable Open / Closed / Recent fills / Skipped tabs. |
| `report.py` | Command-line P&L report. |
| `test_copier.py` | Offline unit tests for the buy/sell/settlement/slippage ledger logic. |

## How copying works

- **Sizing** — configurable in `copier.py` (`COPY_RATIO`, `MIN_BET`). Currently
  `$-for-$` (mirror the trader's exact size).
- **Slippage cap** — if the live price is more than `MAX_SLIPPAGE` (5¢) worse
  than the trader's fill by the time we react, the copy is skipped and logged.
- **No backfill** — a newly added trader is copied only from the moment added.
- **Traders** are stored in the `traders` table and managed live from the
  dashboard's "Manage traders" panel — no restart or code edit needed.

## Data sources (all public, no auth)

- Trader activity: `data-api.polymarket.com/activity`
- Live prices: `clob.polymarket.com/midpoint` and `/prices-history`
- Resolution: `gamma-api.polymarket.com/markets` (+ CLOB fallback)

## Running

```sh
# The watcher (writes ledger.db)
python3 copier.py

# The dashboard (reads ledger.db); set the login password via env var
DASH_PASSWORD=your-password python3 dashboard.py   # serves on :80

# On-demand P&L in the terminal
python3 report.py
```

In production both run as `systemd` services on a small Linux host, with the
DB in WAL mode so the dashboard reads/writes don't collide with the watcher.

## Notes

- `ledger.db` and logs are git-ignored — this repo is code only.
- Paper fills assume you'd fill near the trader's price a few seconds later;
  real copying suffers more slippage, so paper P&L is optimistic.
- This is a personal research tool, not financial advice.
