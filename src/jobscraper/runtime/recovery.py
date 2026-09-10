"""Restart recovery for durable requests (03 §16/§18, RUN-07/RUN-09).

The service process is the single-machine claim/capacity coordinator
(RUN-09): request ownership lives inside that one process.  A service
restart therefore orphans every ``RUNNING`` request — no worker can
exist to finish or abandon it — and recovery runs under the fresh
service epoch opened by ``clock.begin_service_epoch`` (which records any
still-open epoch as ended by ``SERVICE_RESTART``; ownership from prior
epochs is invalid from that moment on):

* each orphaned RUNNING request is reclaimed with the RUN-07
  transitions (prior attempt ABANDONED; RETRY_WAIT while the attempt
  budget remains, FAILED once exhausted).  Restart reclamation does not
  wait out the old lease deadline because the lease's only possible
  owner cannot exist after a restart;
* runs whose durable cancellation was interrupted by the crash are
  finalized: still-pending acquisition work becomes CANCELLED, groups
  close, and the aggregate status (CANCELLED) is persisted.

Non-cancelled interrupted runs stay honestly non-terminal: nothing
fabricates an outcome the dead driver never produced.  The operator can
cancel such a run (terminal CANCELLED) or start a new one, which resumes
enumeration from the durable binding cursor.

Nothing is deleted and already accepted evidence remains valid.
"""

from __future__ import annotations

import sqlite3

from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.claims import (
    ABANDONED_SERVICE_RESTART,
    _abandon_and_requeue,
)
from jobscraper.runtime.clock import db_utc_now
from jobscraper.runtime.runs import aggregate_run


def recover_interrupted_requests(
    conn: sqlite3.Connection, *, now: str | None = None
) -> dict:
    """Reclaim requests orphaned by a service restart and finalize
    interrupted cancellations.  Returns ``{"reclaimed": [...],
    "finalized_cancelled_runs": [...]}``."""
    ts = now or db_utc_now(conn)
    orphaned = conn.execute(
        """
        SELECT id, run_id, current_attempt_id, attempt_count, max_attempts
        FROM scrape_requests
        WHERE status = 'RUNNING'
        ORDER BY created_at, id
        """
    ).fetchall()

    reclaimed: list[str] = []
    affected_runs: set[str] = set()
    for row in orphaned:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: a live worker may have
            # committed between the scan and this transaction.
            current = conn.execute(
                "SELECT id, status, current_attempt_id, attempt_count, max_attempts"
                " FROM scrape_requests WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "RUNNING":
                conn.execute("COMMIT")
                continue
            _abandon_and_requeue(
                conn,
                current,
                ts=ts,
                abandoned_reason=ABANDONED_SERVICE_RESTART,
                failure_detail="attempt budget exhausted (service restart)",
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:  # pragma: no cover - already rolled back
                pass
            raise
        reclaimed.append(row["id"])
        affected_runs.add(row["run_id"])

    finalized: list[str] = []
    for run_id in sorted(affected_runs):
        row = conn.execute(
            "SELECT cancel_requested_at FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None or row["cancel_requested_at"] is None:
            continue
        # The crash interrupted the cancellation sweep: finish it and
        # persist the run's terminal aggregate.
        request_run_cancellation(conn, run_id, now=ts)
        if aggregate_run(conn, run_id, now=ts) is not None:
            finalized.append(run_id)

    return {"reclaimed": reclaimed, "finalized_cancelled_runs": finalized}


__all__ = ["recover_interrupted_requests"]
