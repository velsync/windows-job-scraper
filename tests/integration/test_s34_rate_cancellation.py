"""S3.4 durable cooldown/cancellation tests. GENERATED ONLY; not executed."""

from __future__ import annotations

from jobscraper.db.connection import Database
from jobscraper.db.migrations import LATEST_SCHEMA_VERSION, migrate_schema
from jobscraper.runtime.cancellation import request_run_cancellation
from jobscraper.runtime.rate import RateKey, dispatch_gate, record_failure

NOW = "2026-09-11T08:00:00.000000Z"


def _binding(db: Database) -> None:
    c = db.conn
    c.execute(
        "INSERT INTO sources (id,display_name,source_family,entry_url,created_at,updated_at)"
        " VALUES ('src','S','PUBLIC_FEED','https://example.test/jobs',?,?)",
        (NOW, NOW),
    )
    c.execute(
        "INSERT INTO source_adapter_bindings (id,source_id,display_name,created_at)"
        " VALUES ('bnd','src','B',?)", (NOW,)
    )
    c.commit()


def test_retry_after_cooldown_survives_database_reopen(tmp_path):
    path = tmp_path / "rate.db"
    db = Database(path)
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _binding(db)
    key = RateKey("bnd", "example.test", None)
    record_failure(
        db.conn,
        key,
        failure_kind="RATE_LIMIT",
        delay_s=120,
        retry_after_raw="120",
        now=NOW,
    )
    before = dispatch_gate(db.conn, key, now=NOW)
    assert not before.allowed
    assert before.cooldown_until is not None
    db.close()

    reopened = Database(path)
    after = dispatch_gate(reopened.conn, key, now=NOW)
    row = reopened.conn.execute(
        "SELECT circuit_state,last_retry_after,cooldown_until"
        " FROM binding_host_rate_state WHERE binding_id='bnd' AND host='example.test'"
    ).fetchone()
    assert not after.allowed
    assert after.cooldown_until == before.cooldown_until
    assert row["circuit_state"] == "OPEN"
    assert row["last_retry_after"] == "120"
    reopened.close()


def test_cooldown_expiry_reenables_dispatch(tmp_path):
    db = Database(tmp_path / "rate-expiry.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _binding(db)
    key = RateKey("bnd", "example.test", None)
    record_failure(
        db.conn, key, failure_kind="HTTP_5XX", delay_s=60,
        retry_after_raw=None, now=NOW,
    )
    assert not dispatch_gate(db.conn, key, now=NOW).allowed
    assert dispatch_gate(
        db.conn, key, now="2026-09-11T08:01:01.000000Z"
    ).allowed
    db.close()


def test_transient_failure_without_delay_never_creates_permanent_open_circuit(tmp_path):
    db = Database(tmp_path / "rate-no-delay.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _binding(db)
    key = RateKey("bnd", "example.test", None)
    record_failure(
        db.conn, key, failure_kind="HTTP_5XX", delay_s=None,
        retry_after_raw=None, now=NOW,
    )
    row = db.conn.execute(
        "SELECT circuit_state,cooldown_until,recent_failure_count"
        " FROM binding_host_rate_state WHERE binding_id='bnd' AND host='example.test'"
    ).fetchone()
    assert row["circuit_state"] == "CLOSED"
    assert row["cooldown_until"] is None
    assert row["recent_failure_count"] == 1
    assert dispatch_gate(db.conn, key, now=NOW).allowed
    db.close()


def test_cancellation_closes_retry_wait_acquisition_without_touching_evidence(tmp_path):
    db = Database(tmp_path / "cancel-retry.db")
    migrate_schema(db.conn, LATEST_SCHEMA_VERSION)
    _binding(db)
    c = db.conn
    c.execute(
        "INSERT INTO scrape_runs(id,status,created_at) VALUES ('run','RUNNING',?)",
        (NOW,),
    )
    c.execute(
        "INSERT INTO scrape_requests("
        "id,run_id,source_id,binding_id,request_type,request_unique_key,status,"
        "next_retry_at,created_at,updated_at) VALUES "
        "('req','run','src','bnd','LIST_FETCH','uniq','RETRY_WAIT',?,?,?)",
        ("2026-09-11T08:05:00.000000Z", NOW, NOW),
    )
    c.execute(
        "INSERT INTO events(at,level,kind,message,data_json,request_id)"
        " VALUES (?,?,?,?,?,?)",
        (NOW, "INFO", "ACCEPTED_EVIDENCE", "x", "{}", "req"),
    )
    c.commit()
    request_run_cancellation(c, "run", now=NOW)
    row = c.execute(
        "SELECT status,last_failure_kind FROM scrape_requests WHERE id='req'"
    ).fetchone()
    assert row["status"] == "CANCELLED"
    assert row["last_failure_kind"] == "CANCELLED"
    assert c.execute(
        "SELECT COUNT(*) FROM events WHERE kind='ACCEPTED_EVIDENCE'"
    ).fetchone()[0] == 1
    db.close()
