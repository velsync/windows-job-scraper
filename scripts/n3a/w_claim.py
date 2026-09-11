"""Race one claim from a real separate OS process. Prints CLAIMED ... | NONE | BUSY."""
import sqlite3
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from jobscraper.db.connection import connect_db
from jobscraper.runtime.claims import claim_next_request

path, name, now = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    conn = connect_db(path)
    claim = claim_next_request(conn, name, now=now)
    print(f"CLAIMED {claim.request_id} {claim.attempt_id}" if claim else "NONE", flush=True)
    conn.close()
except sqlite3.OperationalError as e:
    print(f"BUSY {e}", flush=True)
