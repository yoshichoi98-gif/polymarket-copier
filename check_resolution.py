#!/usr/bin/env python3
"""Debug helper: show gamma resolution status for held positions in likely-finished games."""
import json
import sqlite3
import urllib.parse
import urllib.request

db = sqlite3.connect("/root/polymarket-copier/ledger.db")
rows = db.execute(
    "SELECT DISTINCT condition_id, title FROM positions"
    " WHERE title LIKE '%Kansas City%' OR title LIKE '%Guatemala%' LIMIT 5"
).fetchall()
for cid, title in rows:
    url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode(
        {"condition_ids": cid}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "paper-copier/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        markets = json.loads(r.read().decode())
    if not markets:
        print(f"{title[:48]:48} -> gamma EMPTY")
        continue
    m = markets[0]
    print(
        f"{title[:48]:48} -> closed={m.get('closed')}"
        f" uma={m.get('umaResolutionStatus')} prices={m.get('outcomePrices')}"
    )
