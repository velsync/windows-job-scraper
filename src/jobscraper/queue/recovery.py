"""Crash recovery and startup reconciliation.

Authority: RUN-19 (crash points that must remain safe); module 03 section 18.
"""

from __future__ import annotations

import json

from jobscraper.db.connection import Database, immediate_transaction
from jobscraper.queue.claims import reclaim_expired_requests
from jobscraper.timeutil import add_seconds, utc_now_s


def recover_on_startup(db: Database, *, now: str | None = None) -> dict:
    """Reconcile queue state after a service crash/restart.

    * reclaim RUNNING requests with expired leases (crash mid-flight);
    * RETRY_WAIT requests whose next_retry_at is due remain claimable;
    * requests RUNNING with a live lease are left alone (attached worker may
      still be draining; the lease will expire naturally if not).
    """
    now = now or utc_now_s()
    reclaimed = reclaim_expired_requests(db, now=now, abandoned_reason="service_restart")
    report = {"reclaimed": reclaimed, "runs_marked": []}
    # Runs that were RUNNING but whose requests are all terminal get marked.
    rows = db.query(
        "SELECT r.id, r.status, (SELECT COUNT(*) FROM scrape_requests q WHERE q.run_id = r.id"
        " AND q.status IN ('PENDING','RUNNING','RETRY_WAIT')) AS open_count"
        " FROM scrape_runs r WHERE r.status = 'RUNNING'"
    )
    for row in rows:
        if row["open_count"] == 0:
            with immediate_transaction(db.conn) as tx:
                tx.execute(
                    "UPDATE scrape_runs SET status='PARTIAL', finished_at=?,"
                    " processing_status='RESUMABLE' WHERE id=? AND status='RUNNING'",
                    (now, row["id"]),
                )
            report["runs_marked"].append(row["id"])
    return report
