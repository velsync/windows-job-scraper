"""Live request authorization for Slice 3 (RUN-02, §18, SEC-06).

Historical :class:`RunSourcePlan` rows are immutable reproducibility evidence;
they are never treated as continuing permission.  Every source-network unit is
re-authorized against current stable administrative state at claim,
pre-dispatch, heartbeat and commit boundaries.  Host-native obligations are a
separate authority domain: accepted evidence may keep draining locally after
source cancellation/quarantine and never gains source-network authority merely
because it is claimable (R2-F3).

Slice 3 intentionally has no authenticated/browser acquisition authority.
When a pinned binding requires authentication the public HTTP runtime fails
closed with ``AUTH_REQUIRED``.  Slice 6 may supply the authenticated-session
owner without changing this network-vs-local distinction.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass

from jobscraper.runtime.clock import current_service_epoch
from jobscraper.runtime.requests import ACQUISITION_REQUEST_TYPES


class AuthorizationDomain(enum.Enum):
    SOURCE_NETWORK = "SOURCE_NETWORK"
    LOCAL_PROCESSING = "LOCAL_PROCESSING"


class AuthorizationReason(enum.Enum):
    ALLOWED = "ALLOWED"
    REQUEST_MISSING = "REQUEST_MISSING"
    REQUEST_NOT_RUNNING = "REQUEST_NOT_RUNNING"
    ATTEMPT_MISMATCH = "ATTEMPT_MISMATCH"
    RUN_CANCELLED = "RUN_CANCELLED"
    PLAN_MISSING = "PLAN_MISSING"
    PLAN_IDENTITY_MISMATCH = "PLAN_IDENTITY_MISMATCH"
    SOURCE_DISABLED = "SOURCE_DISABLED"
    SOURCE_QUARANTINED = "SOURCE_QUARANTINED"
    BINDING_DISABLED = "BINDING_DISABLED"
    BINDING_QUARANTINED = "BINDING_QUARANTINED"
    BINDING_RETIRED = "BINDING_RETIRED"
    PERMISSION_REVOKED = "PERMISSION_REVOKED"
    AUTH_REQUIRED = "AUTH_REQUIRED"


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    domain: AuthorizationDomain
    reason: AuthorizationReason
    detail: str = ""


class AuthorizationDenied(RuntimeError):
    """Current authority denies the requested operation."""

    def __init__(self, request_id: str, decision: AuthorizationDecision):
        self.request_id = request_id
        self.decision = decision
        super().__init__(
            f"authorization denied for request {request_id}: "
            f"{decision.reason.value} ({decision.detail})"
        )


_ACQUISITION_SQL = """
AND run.cancel_requested_at IS NULL
AND s.desired_state = 'ENABLED'
AND s.administrative_state = 'NORMAL'
AND b.desired_state = 'ENABLED'
AND b.administrative_state = 'NORMAL'
AND b.retired_at IS NULL
AND EXISTS (
    SELECT 1
    FROM run_source_plans rsp
    JOIN source_adapter_binding_revisions br
      ON br.id = rsp.binding_revision_id
    JOIN adapter_permission_profiles pp
      ON pp.id = rsp.permission_profile_id
    WHERE rsp.id = req.run_source_plan_id
      AND rsp.run_id = req.run_id
      AND rsp.source_id = req.source_id
      AND rsp.binding_id = req.binding_id
      AND br.binding_id = req.binding_id
      AND br.id = rsp.binding_revision_id
      AND br.binding_id = rsp.binding_id
      AND br.adapter_id = rsp.adapter_id
      AND br.adapter_version = rsp.adapter_version
      AND br.strategy = rsp.strategy
      AND br.execution_class = rsp.execution_class
      AND br.permission_profile_id = rsp.permission_profile_id
      AND br.permission_profile_revision = rsp.permission_profile_revision
      AND br.auth_scope_id IS rsp.auth_scope_id
      AND br.auth_requirement = 'NONE'
      AND EXISTS (
          SELECT 1
            FROM adapter_definitions ad
           WHERE ad.adapter_id = rsp.adapter_id
             AND ad.adapter_version = rsp.adapter_version
             AND ad.adapter_api_version = rsp.adapter_api_version
      )
      AND pp.administrative_state = 'NORMAL'
      AND pp.retired_at IS NULL
)
"""


def acquisition_claim_sql_predicate() -> str:
    """SQL predicate used by the deterministic acquisition claim selector.

    Aliases are intentionally fixed to the claim query's ``req/run/s/b``
    aliases.  Keeping this fragment here prevents claim authorization from
    drifting away from the bounded pre-dispatch/commit predicate.
    """

    return _ACQUISITION_SQL


def authorization_domain(request_type: str) -> AuthorizationDomain:
    return (
        AuthorizationDomain.SOURCE_NETWORK
        if request_type in ACQUISITION_REQUEST_TYPES
        else AuthorizationDomain.LOCAL_PROCESSING
    )


def _row(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            req.id AS request_id,
            req.status AS request_status,
            req.current_attempt_id,
            req.run_id AS request_run_id,
            req.run_source_plan_id,
            req.source_id AS request_source_id,
            req.binding_id AS request_binding_id,
            req.request_type,
            run.cancel_requested_at,
            s.desired_state AS source_desired_state,
            s.administrative_state AS source_administrative_state,
            b.desired_state AS binding_desired_state,
            b.administrative_state AS binding_administrative_state,
            b.retired_at AS binding_retired_at,
            rsp.run_id AS plan_run_id,
            rsp.source_id AS plan_source_id,
            rsp.binding_id AS plan_binding_id,
            rsp.binding_revision_id,
            rsp.adapter_id AS plan_adapter_id,
            rsp.adapter_version AS plan_adapter_version,
            rsp.adapter_api_version AS plan_adapter_api_version,
            rsp.strategy AS plan_strategy,
            rsp.execution_class AS plan_execution_class,
            rsp.permission_profile_id,
            rsp.permission_profile_revision,
            rsp.auth_scope_id AS plan_auth_scope_id,
            br.binding_id AS revision_binding_id,
            br.adapter_id AS revision_adapter_id,
            br.adapter_version AS revision_adapter_version,
            br.strategy AS revision_strategy,
            br.execution_class AS revision_execution_class,
            br.permission_profile_id AS revision_permission_profile_id,
            br.permission_profile_revision AS revision_permission_profile_revision,
            br.auth_requirement,
            br.auth_scope_id AS binding_auth_scope_id,
            ad.adapter_api_version AS definition_adapter_api_version,
            pp.administrative_state AS permission_administrative_state,
            pp.retired_at AS permission_retired_at
        FROM scrape_requests req
        JOIN scrape_runs run ON run.id = req.run_id
        JOIN sources s ON s.id = req.source_id
        JOIN source_adapter_bindings b ON b.id = req.binding_id
        LEFT JOIN run_source_plans rsp ON rsp.id = req.run_source_plan_id
        LEFT JOIN source_adapter_binding_revisions br
               ON br.id = rsp.binding_revision_id
        LEFT JOIN adapter_definitions ad
               ON ad.adapter_id = br.adapter_id AND ad.adapter_version = br.adapter_version
        LEFT JOIN adapter_permission_profiles pp
               ON pp.id = rsp.permission_profile_id
        WHERE req.id = ?
        """,
        (request_id,),
    ).fetchone()


def evaluate_request_authorization(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    attempt_id: str | None = None,
    require_running: bool = False,
) -> AuthorizationDecision:
    """Evaluate current authority without mutating durable state.

    Callers that need a race-free decision invoke this inside their existing
    ``BEGIN IMMEDIATE`` transaction.  The function itself never opens a
    transaction and performs no I/O outside SQLite.
    """

    row = _row(conn, request_id)
    if row is None:
        return AuthorizationDecision(
            False,
            AuthorizationDomain.SOURCE_NETWORK,
            AuthorizationReason.REQUEST_MISSING,
            "request row does not exist",
        )

    domain = authorization_domain(row["request_type"])
    if require_running and row["request_status"] != "RUNNING":
        return AuthorizationDecision(
            False, domain, AuthorizationReason.REQUEST_NOT_RUNNING, row["request_status"]
        )
    if attempt_id is not None and row["current_attempt_id"] != attempt_id:
        return AuthorizationDecision(
            False,
            domain,
            AuthorizationReason.ATTEMPT_MISMATCH,
            f"current={row['current_attempt_id']!r}",
        )

    # R2-F3: already-created host-native obligations are local evidence work.
    # They intentionally ignore source-network cancellation/quarantine/auth
    # state and may never use this allowance to initiate source I/O.
    if domain is AuthorizationDomain.LOCAL_PROCESSING:
        return AuthorizationDecision(True, domain, AuthorizationReason.ALLOWED)

    if row["cancel_requested_at"] is not None:
        return AuthorizationDecision(False, domain, AuthorizationReason.RUN_CANCELLED)
    if row["source_desired_state"] != "ENABLED":
        return AuthorizationDecision(False, domain, AuthorizationReason.SOURCE_DISABLED)
    if row["source_administrative_state"] != "NORMAL":
        return AuthorizationDecision(False, domain, AuthorizationReason.SOURCE_QUARANTINED)
    if row["binding_desired_state"] != "ENABLED":
        return AuthorizationDecision(False, domain, AuthorizationReason.BINDING_DISABLED)
    if row["binding_administrative_state"] != "NORMAL":
        return AuthorizationDecision(False, domain, AuthorizationReason.BINDING_QUARANTINED)
    if row["binding_retired_at"] is not None:
        return AuthorizationDecision(False, domain, AuthorizationReason.BINDING_RETIRED)

    if row["run_source_plan_id"] is None or row["binding_revision_id"] is None:
        return AuthorizationDecision(False, domain, AuthorizationReason.PLAN_MISSING)
    if (
        row["request_run_id"] != row["plan_run_id"]
        or row["request_source_id"] != row["plan_source_id"]
        or row["request_binding_id"] != row["plan_binding_id"]
        or row["revision_binding_id"] != row["plan_binding_id"]
        or row["revision_adapter_id"] != row["plan_adapter_id"]
        or row["revision_adapter_version"] != row["plan_adapter_version"]
        or row["definition_adapter_api_version"] != row["plan_adapter_api_version"]
        or row["revision_strategy"] != row["plan_strategy"]
        or row["revision_execution_class"] != row["plan_execution_class"]
        or row["revision_permission_profile_id"] != row["permission_profile_id"]
        or row["revision_permission_profile_revision"]
           != row["permission_profile_revision"]
        or row["binding_auth_scope_id"] != row["plan_auth_scope_id"]
    ):
        return AuthorizationDecision(
            False, domain, AuthorizationReason.PLAN_IDENTITY_MISMATCH
        )

    if (
        row["permission_administrative_state"] != "NORMAL"
        or row["permission_retired_at"] is not None
    ):
        return AuthorizationDecision(False, domain, AuthorizationReason.PERMISSION_REVOKED)

    # Authenticated/browser acquisition is Slice 6.  A historical plan that
    # pins such a requirement is evidence, not authority for Slice-3 HTTP.
    if (row["auth_requirement"] or "NONE") != "NONE":
        return AuthorizationDecision(
            False,
            domain,
            AuthorizationReason.AUTH_REQUIRED,
            f"auth_requirement={row['auth_requirement']!r}",
        )

    return AuthorizationDecision(True, domain, AuthorizationReason.ALLOWED)


def require_request_authorized(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    attempt_id: str | None = None,
    require_running: bool = False,
) -> AuthorizationDecision:
    decision = evaluate_request_authorization(
        conn,
        request_id,
        attempt_id=attempt_id,
        require_running=require_running,
    )
    if not decision.allowed:
        raise AuthorizationDenied(request_id, decision)
    return decision


def denial_failure_kind(decision: AuthorizationDecision) -> str:
    if decision.reason is AuthorizationReason.RUN_CANCELLED:
        return "CANCELLED"
    if decision.reason is AuthorizationReason.AUTH_REQUIRED:
        return "AUTH_REQUIRED"
    return "POLICY_REJECTED"


def finalize_authorization_denial(
    conn: sqlite3.Connection,
    request_id: str,
    attempt_id: str,
    *,
    decision: AuthorizationDecision,
    now: str,
) -> bool:
    """Record a live-policy denial without committing request-owned outputs.

    This is deliberately narrower than :func:`runtime.fence.fenced_commit`:
    it only closes the still-current, still-leased attempt after the output
    transaction has been rolled back.  A stale worker cannot use a policy
    denial to terminalize somebody else's request.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        epoch = current_service_epoch(conn)
        if epoch is None:
            conn.execute("ROLLBACK")
            return False

        # The caller's decision explains why it arrived here, but it is not a
        # continuing authorization fact.  Re-read current authority under the
        # same write transaction so an already-cleared revocation cannot close
        # a valid current attempt.
        current = evaluate_request_authorization(
            conn, request_id, attempt_id=attempt_id, require_running=True
        )
        if current.allowed:
            conn.execute("ROLLBACK")
            return False
        decision = current
        terminal = (
            "CANCELLED"
            if decision.reason is AuthorizationReason.RUN_CANCELLED
            else "FAILED"
        )
        failure_kind = denial_failure_kind(decision)
        detail = json.dumps(
            {"authorization_reason": decision.reason.value, "detail": decision.detail},
            sort_keys=True,
            separators=(",", ":"),
        )

        updated = conn.execute(
            """
            UPDATE scrape_requests
               SET status = ?, finished_at = ?, last_failure_kind = ?,
                   last_failure_json = ?, next_retry_at = NULL, current_worker_id = NULL,
                   current_attempt_id = NULL, lease_until = NULL,
                   updated_at = ?
             WHERE id = ? AND status = 'RUNNING'
               AND current_attempt_id = ? AND lease_until > ?
               AND EXISTS (
                   SELECT 1 FROM request_attempts a
                    WHERE a.attempt_id = scrape_requests.current_attempt_id
                      AND a.request_id = scrape_requests.id
                      AND a.service_epoch_id = ?
               )
            """,
            (
                terminal, now, failure_kind, detail, now, request_id,
                attempt_id, now, epoch.epoch_id,
            ),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """
            UPDATE request_attempts
               SET outcome = ?, failure_kind = ?, finished_at = ?
             WHERE attempt_id = ? AND request_id = ?
            """,
            (terminal, failure_kind, now, attempt_id, request_id),
        )
        conn.execute("COMMIT")
        return True
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


__all__ = [
    "AuthorizationDecision",
    "AuthorizationDenied",
    "AuthorizationDomain",
    "AuthorizationReason",
    "acquisition_claim_sql_predicate",
    "authorization_domain",
    "denial_failure_kind",
    "evaluate_request_authorization",
    "finalize_authorization_denial",
    "require_request_authorized",
]
