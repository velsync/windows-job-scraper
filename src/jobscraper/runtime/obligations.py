"""Durable host-native downstream processing obligations.

Authority: ARC-05 flow invariants (every accepted immutable observation
atomically creates or satisfies a durable processing obligation); module 03
section 18 (cancellation cannot orphan accepted evidence).
"""

from __future__ import annotations

import secrets

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.timeutil import utc_now_s


def create_obligation(
    tx,
    *,
    kind: str,
    observation_id: str,
    run_id: str | None,
    request_id: str | None,
    payload: dict | None = None,
    now: str | None = None,
) -> str:
    """Create an obligation inside the caller's fenced transaction."""
    now = now or utc_now_s()
    obligation_id = "obl-" + secrets.token_hex(10)
    tx.execute(
        "INSERT INTO processing_obligations(id, kind, run_id, request_id, observation_id,"
        " payload_json, status, created_at) VALUES (?,?,?,?,?,?, 'PENDING', ?)",
        (obligation_id, kind, run_id, request_id, observation_id,
         __import__("json").dumps(payload or {}, sort_keys=True), now),
    )
    return obligation_id


def claim_pending(db: Database, *, limit: int = 50, now: str | None = None):
    """Claim pending obligations (service is the single processor)."""
    import json

    now = now or utc_now_s()
    claimed = []
    with immediate_transaction(db.conn) as tx:
        rows = tx.execute(
            "SELECT * FROM processing_obligations WHERE status='PENDING' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            token = secrets.token_hex(8)
            tx.execute(
                "UPDATE processing_obligations SET status='RUNNING', claim_token=?, attempt_count=attempt_count+1"
                " WHERE id=? AND status='PENDING'",
                (token, row["id"]),
            )
            data = dict(row)
            data["claim_token"] = token
            data["payload"] = json.loads(row["payload_json"] or "{}")
            claimed.append(data)
    return claimed


def satisfy(db: Database, obligation_id: str, *, claim_token: str, now: str | None = None) -> bool:
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        cur = tx.execute(
            "UPDATE processing_obligations SET status='SATISFIED', satisfied_at=?"
            " WHERE id=? AND status='RUNNING' AND claim_token=?",
            (now, obligation_id, claim_token),
        )
        return cur.rowcount == 1


def fail(db: Database, obligation_id: str, *, claim_token: str, now: str | None = None) -> bool:
    """Return a failed obligation to PENDING for retry (bounded by caller)."""
    now = now or utc_now_s()
    with immediate_transaction(db.conn) as tx:
        cur = tx.execute(
            "UPDATE processing_obligations SET status='PENDING', claim_token=NULL"
            " WHERE id=? AND status='RUNNING' AND claim_token=?",
            (obligation_id, claim_token),
        )
        return cur.rowcount == 1


def pending_count(db: Database, run_id: str | None = None) -> int:
    if run_id:
        row = db.query_one(
            "SELECT COUNT(*) c FROM processing_obligations WHERE status IN ('PENDING','RUNNING') AND run_id=?",
            (run_id,),
        )
    else:
        row = db.query_one("SELECT COUNT(*) c FROM processing_obligations WHERE status IN ('PENDING','RUNNING')")
    return int(row["c"])
