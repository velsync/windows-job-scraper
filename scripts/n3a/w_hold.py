"""Claim in a real process, then sleep until killed (simulates service death)."""
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from jobscraper.db.connection import connect_db
from jobscraper.runtime.claims import claim_next_request

path, name, now = sys.argv[1], sys.argv[2], sys.argv[3]
conn = connect_db(path)
claim = claim_next_request(conn, name, now=now)
print(f"HELD {claim.attempt_id} {claim.request_id}", flush=True)
time.sleep(600)  # parent kills us; never exits cooperatively
