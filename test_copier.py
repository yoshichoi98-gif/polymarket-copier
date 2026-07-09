#!/usr/bin/env python3
"""Offline tests for copier.py ledger logic using injected fake activity records."""

import os
import tempfile

# Point the module at a throwaway DB before importing it.
import copier

copier.DB_PATH = tempfile.mktemp(suffix=".db")
copier.LOG_PATH = os.devnull
# Tests exercise the mechanics with the original 1% + $1-min config,
# independent of the live sizing in copier.py.
copier.COPY_RATIO = 0.01
copier.MIN_BET = 1.00


def fake(side, size, price, tx, asset="tokA", oi=0):
    return {
        "side": side, "size": size, "price": price, "asset": asset,
        "conditionId": "cond1", "outcomeIndex": oi, "timestamp": 1_700_000_000,
        "title": "Test Market", "outcome": "Yes", "transactionHash": tx,
    }


def main():
    db = copier.open_db()

    # 1. Buy: 1000 shares @ 0.60 -> we get 10 shares, $6 cost
    s = copier.record_trade(db, "swisstony", fake("BUY", 1000, 0.60, "tx1"))
    assert s and "BUY 10.00 sh" in s, s
    shares, cost = db.execute("SELECT shares, avg_cost FROM positions").fetchone()
    assert abs(shares - 10) < 1e-9 and abs(cost - 0.60) < 1e-9

    # 2. Duplicate fill is ignored
    assert copier.record_trade(db, "swisstony", fake("BUY", 1000, 0.60, "tx1")) is None

    # 3. Second buy at different price -> avg cost blends (10@.60 + 5@.90 = .70)
    copier.record_trade(db, "swisstony", fake("BUY", 500, 0.90, "tx2"))
    shares, cost = db.execute("SELECT shares, avg_cost FROM positions").fetchone()
    assert abs(shares - 15) < 1e-9 and abs(cost - 0.70) < 1e-9, (shares, cost)

    # 4. They sell 600 shares @ 0.80 -> we sell 6 sh, pnl = 6*(0.80-0.70)=+0.60
    s = copier.record_trade(db, "swisstony", fake("SELL", 600, 0.80, "tx3"))
    assert "SELL 6.00 sh" in s and "+$0.60" in s, s
    shares = db.execute("SELECT shares FROM positions").fetchone()[0]
    assert abs(shares - 9) < 1e-9

    # 5. Oversized sell is capped at our holdings and closes the position
    s = copier.record_trade(db, "swisstony", fake("SELL", 99999, 0.50, "tx4"))
    assert "SELL 9.00 sh" in s, s
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0

    # 6. Sell of a token we never held is ignored
    assert copier.record_trade(db, "swisstony", fake("SELL", 100, 0.5, "tx5", asset="tokB")) is None

    # 7. Settlement: buy then resolve at 1.0 -> pnl = 20*(1-0.25)=+15
    copier.record_trade(db, "ColdMath", fake("BUY", 2000, 0.25, "tx6", asset="tokC"))
    s = copier.settle_position(db, "tokC", 1.0, 1_700_000_100)
    assert "WON" in s and "+$15.00" in s, s
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0

    # 8. Losing settlement: buy then resolve at 0 -> pnl = -cost
    copier.record_trade(db, "ColdMath", fake("BUY", 1000, 0.40, "tx7", asset="tokD"))
    s = copier.settle_position(db, "tokD", 0.0, 1_700_000_200)
    assert "LOST" in s and "-$4.00" in s, s

    # 9. Slippage cap: live price >5¢ worse than his fill -> skipped, no position
    s = copier.record_trade(db, "swisstony", fake("BUY", 1000, 0.50, "tx8", asset="tokE"),
                            our_price=0.56)
    assert s and "SKIPPED" in s, s
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    # ...and the same skip isn't re-logged on the next poll
    assert copier.record_trade(db, "swisstony", fake("BUY", 1000, 0.50, "tx8", asset="tokE"),
                               our_price=0.56) is None

    # 10. Slippage within cap fills at OUR price (0.54 basis, not 0.50)
    s = copier.record_trade(db, "swisstony", fake("BUY", 1000, 0.50, "tx9", asset="tokF"),
                            our_price=0.54)
    assert "his 50¢ / ours 54¢" in s, s
    cost = db.execute("SELECT avg_cost FROM positions WHERE asset='tokF'").fetchone()[0]
    assert abs(cost - 0.54) < 1e-9
    copier.settle_position(db, "tokF", 1.0, 1_700_000_300)  # clean up: +10*0.46=+4.60

    # 11. $1 minimum: their 100 sh @ 0.50 -> 1% = $0.50, bumped to 2 sh ($1.00)
    s = copier.record_trade(db, "uma003", fake("BUY", 100, 0.50, "tx10", asset="tokG"))
    assert "BUY 2.00 sh" in s, s
    # ...and a 50% sell of THEIR position sells 50% of ours (1 sh), not 1% of theirs
    s = copier.record_trade(db, "uma003", fake("SELL", 50, 0.60, "tx11", asset="tokG"))
    assert "SELL 1.00 sh" in s, s  # pnl = 1*(0.60-0.50) = +0.10
    held = db.execute("SELECT shares FROM positions WHERE asset='tokG'").fetchone()[0]
    assert abs(held - 1.0) < 1e-9
    copier.settle_position(db, "tokG", 0.0, 1_700_000_400)  # clean up: -0.50

    realized = db.execute("SELECT SUM(pnl) FROM closed").fetchone()[0]
    # 0.60 - 1.80 + 15.00 - 4.00 + 4.60 + 0.10 - 0.50 = 14.00
    assert abs(realized - 14.00) < 1e-6, realized

    print("All 11 tests passed. Realized P&L in test ledger: $%.2f" % realized)
    os.unlink(copier.DB_PATH)


if __name__ == "__main__":
    main()
