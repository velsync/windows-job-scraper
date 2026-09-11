"""Record a Retry-After cooldown, print gate state, exit (restart boundary)."""
import json
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from jobscraper.db.connection import connect_db
from jobscraper.runtime.rate import RateKey, dispatch_gate, record_failure

path, now = sys.argv[1], sys.argv[2]
conn = connect_db(path)
key = RateKey("bnd-1", "example.test", None)
record_failure(conn, key, failure_kind="RATE_LIMIT", delay_s=120,
               retry_after_raw="120", now=now)
gate = dispatch_gate(conn, key, now=now)
print(json.dumps({"allowed": gate.allowed, "cooldown_until": gate.cooldown_until}),
      flush=True)
conn.close()
