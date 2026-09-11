"""Fresh process: read back the persisted cooldown gate + circuit row."""
import json
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from jobscraper.db.connection import connect_db
from jobscraper.runtime.rate import RateKey, dispatch_gate

path, now = sys.argv[1], sys.argv[2]
conn = connect_db(path)
key = RateKey("bnd-1", "example.test", None)
gate = dispatch_gate(conn, key, now=now)
row = conn.execute(
    "SELECT circuit_state, last_retry_after, cooldown_until"
    " FROM binding_host_rate_state WHERE binding_id='bnd-1' AND host='example.test'"
).fetchone()
print(json.dumps({"allowed": gate.allowed, "cooldown_until": gate.cooldown_until,
                  "circuit_state": row["circuit_state"] if row else None}),
      flush=True)
conn.close()
