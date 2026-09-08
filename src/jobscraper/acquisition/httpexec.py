"""Host-owned HTTP executor (02 §11, 04 §5.1).

Design invariants:

* the executor performs I/O only — it never touches the database, so no
  network wait can occur inside a DB write transaction (03 §50);
* destination validation is bound to the actual connection: the address
  returned by ``check_url`` is the address the socket connects to, so a
  DNS change between check and connect cannot redirect the connection
  (DNS-rebinding-safe by construction);
* every redirect hop is re-validated against the same policy with a cap;
* body size and duration are hard-capped (the socket is torn down at the
  cap — a hostile source cannot stream forever);
* response headers are redacted before recording;
* TLS uses certificate verification with SNI bound to the validated host.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time

from jobscraper.acquisition.envelope import (
    ExecutionPlanEnvelope,
    validate_envelope,
)
from jobscraper.acquisition.failures import FailureKind, FailureRecord
from jobscraper.acquisition.result import ResultEnvelope
from jobscraper.net.destination import (
    DestinationPolicy,
    DestinationRejected,
    check_redirect_hop,
    check_url,
)
from jobscraper.timeutil import utc_now_s

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def finish_and(result: ResultEnvelope, started: float, failure: FailureRecord | None) -> ResultEnvelope:
    """Close one execution: timing, typed failure, and the derived evidence.

    ``finalize()`` computes body/normalized-content hashes and the
    content-addressed references (02 §11.3, 03 §30).  A policy denial is
    recorded on the envelope itself so the durable evidence shows *why* there
    is no content, rather than an empty result looking like a successful one.
    """
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.failure = failure
    if failure is not None and failure.kind is FailureKind.POLICY_REJECTED:
        reason = (failure.details_redacted or {}).get("reason_code") or "UNSPECIFIED"
        result.security_policy_result = f"DENIED:{reason}"
    return result.finalize()


def _failure(envelope: ExecutionPlanEnvelope, kind: FailureKind, retryable: bool,
             health: str, details: dict | None = None,
             http_status: int | None = None) -> FailureRecord:
    return FailureRecord(
        kind=kind,
        retryable=retryable,
        source_health_impact=health,
        http_status=http_status,
        source_id=envelope.source_id,
        binding_id=envelope.binding_id,
        adapter_id=envelope.adapter_id,
        adapter_version=envelope.adapter_version,
        run_id=envelope.run_id,
        request_id=envelope.request_id,
        attempt_id=envelope.attempt_id,
        details_redacted=details or {},
        observed_at=utc_now_s(),
    )


def _base(envelope: ExecutionPlanEnvelope) -> ResultEnvelope:
    return ResultEnvelope(
        execution_plan_id=envelope.plan_id,
        request_id=envelope.request_id,
        attempt_id=envelope.attempt_id,
        run_source_plan_id=envelope.run_source_plan_id,
        source_id=envelope.source_id,
        binding_id=envelope.binding_id,
        binding_revision_id=envelope.binding_revision_id,
        adapter_id=envelope.adapter_id,
        adapter_version=envelope.adapter_version,
        strategy=envelope.strategy,
        execution_class=envelope.execution_class,
        requested_url=envelope.payload.url,
        final_url=envelope.payload.url,
        status_code=None,
        fetched_at=utc_now_s(),
    )


def _connect(destination, timeout_s: float) -> socket.socket:
    """Open a socket to the *validated* address (destination binding)."""
    last_error: Exception | None = None
    for address in destination.addresses:
        try:
            sock = socket.create_connection(
                (str(address), destination.port), timeout=timeout_s
            )
            return sock
        except (OSError, socket.timeout) as exc:
            last_error = exc
    raise ConnectionError(f"could not connect to any validated address: {last_error}")


def _read_capped(response: http.client.HTTPResponse, max_bytes: int) -> tuple[bytes, bool, int]:
    """Read the body with a hard byte cap; returns (body, truncated, total)."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                truncated = True
                break
            chunks.append(chunk)
    except (http.client.IncompleteRead, OSError):
        if not chunks and not truncated:
            raise
    return b"".join(chunks), truncated, total


def execute_request(
    envelope: ExecutionPlanEnvelope, policy: DestinationPolicy
) -> ResultEnvelope:
    """Execute one validated request plan and normalize to a ResultEnvelope."""
    result = _base(envelope)
    started = time.monotonic()
    try:
        validate_envelope(envelope, policy)
    except DestinationRejected as exc:
        return finish_and(
            result, started,
            _failure(envelope, FailureKind.POLICY_REJECTED, False, "NONE",
                     {"reason_code": exc.reason_code, "detail": exc.detail}),
        )

    def finish(failure: FailureRecord | None = None) -> ResultEnvelope:
        return finish_and(result, started, failure)

    url = envelope.payload.url
    current_base: str | None = None
    for hop in range(policy.max_redirects + 1):
        if hop > 0:
            result.redirect_chain.append(url)
        try:
            if hop == 0:
                destination = check_url(url, policy)
            else:
                destination = check_redirect_hop(url, policy, hop - 1, base_url=current_base)
        except DestinationRejected as exc:
            return finish(
                _failure(
                    envelope,
                    FailureKind.POLICY_REJECTED,
                    retryable=False,
                    health="NONE",
                    details={"reason_code": exc.reason_code, "detail": exc.detail,
                             "hop": hop},
                )
            )

        conn: http.client.HTTPConnection
        try:
            sock = _connect(destination, envelope.payload.timeout_s)
            if destination.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=destination.host.strip("[]"))
                conn = http.client.HTTPConnection(
                    destination.host, destination.port, timeout=envelope.payload.timeout_s
                )
                conn.sock = sock
            else:
                conn = http.client.HTTPConnection(
                    destination.host, destination.port, timeout=envelope.payload.timeout_s
                )
                conn.sock = sock
            headers = dict(envelope.payload.headers)
            host_header = destination.host
            if not (
                (destination.scheme == "http" and destination.port == 80)
                or (destination.scheme == "https" and destination.port == 443)
            ):
                host_header = f"{destination.host}:{destination.port}"
            headers.setdefault("Host", host_header)
            headers.setdefault("User-Agent", "windows-job-scraper/0.1 (local-first)")
            headers.setdefault("Connection", "close")
            conn.request(envelope.payload.method.upper(), _path_query(destination), headers=headers)
            response = conn.getresponse()
        except (ssl.SSLError, ssl.CertificateError) as exc:
            return finish(
                _failure(envelope, FailureKind.TLS_ERROR, True, "DEGRADED",
                         {"error_type": type(exc).__name__})
            )
        except socket.timeout:
            return finish(
                _failure(envelope, FailureKind.TIMEOUT, True, "DEGRADED",
                         {"timeout_s": envelope.payload.timeout_s})
            )
        except (ConnectionError, OSError) as exc:
            return finish(
                _failure(envelope, FailureKind.CONNECT_ERROR, True, "DEGRADED",
                         {"error_type": type(exc).__name__})
            )

        if response.status in _REDIRECT_STATUSES:
            location = response.getheader("Location")
            result.status_code = response.status
            result.final_url = destination.normalized_url
            current_base = destination.normalized_url
            conn.close()
            if not location:
                return finish(
                    _failure(envelope, FailureKind.SOURCE_CHANGED, True, "DEGRADED",
                             {"detail": "redirect without Location"})
                )
            url = location
            if hop == policy.max_redirects:
                return finish(
                    _failure(envelope, FailureKind.POLICY_REJECTED, False, "NONE",
                             {"reason_code": "REDIRECT_TOO_MANY"})
                )
            continue

        # final response
        result.status_code = response.status
        result.final_url = destination.normalized_url
        result.content_type = response.getheader("Content-Type")
        from jobscraper.acquisition.result import _redact_headers

        result.headers_redacted = _redact_headers(dict(response.getheaders()))
        result.validators_sent = [
            k for k in envelope.payload.headers if k.lower().startswith("if-")
        ]
        if response.status == 304:
            result.was_304 = True
            result.body = b""
            conn.close()
            return finish()
        if response.status >= 400:
            # Bounded diagnostic body so the validity classifier can use
            # content markers (login/challenge) on error statuses.
            try:
                diag, _trunc, total_read = _read_capped(
                    response, min(envelope.payload.max_bytes, 64 * 1024)
                )
            except (socket.timeout, http.client.HTTPException, OSError):
                diag, total_read = b"", 0
            finally:
                try:
                    conn.close()
                except OSError:  # pragma: no cover
                    pass
            result.body = diag
            result.bytes_downloaded = total_read
            if response.status >= 500:
                return finish(
                    _failure(envelope, FailureKind.HTTP_5XX, True, "DEGRADED",
                             http_status=response.status)
                )
            return finish(
                _failure(envelope, FailureKind.HTTP_4XX, False, "DEGRADED",
                         http_status=response.status)
            )
        try:
            body, truncated, total_read = _read_capped(response, envelope.payload.max_bytes)
        except (socket.timeout, http.client.HTTPException, OSError) as exc:
            return finish(
                _failure(envelope, FailureKind.TIMEOUT if isinstance(exc, socket.timeout) else FailureKind.CONNECT_ERROR,
                         True, "DEGRADED", {"error_type": type(exc).__name__})
            )
        finally:
            try:
                conn.close()
            except OSError:  # pragma: no cover
                pass
        result.bytes_downloaded = total_read
        if truncated:
            return finish(
                _failure(envelope, FailureKind.POLICY_REJECTED, False, "NONE",
                         {"reason_code": "BODY_TOO_LARGE",
                          "max_bytes": envelope.payload.max_bytes})
            )
        result.body = body
        return finish()

    return finish(  # pragma: no cover - loop always returns
        _failure(envelope, FailureKind.POLICY_REJECTED, False, "NONE",
                 {"reason_code": "REDIRECT_TOO_MANY"})
    )


def _path_query(destination) -> str:
    path = destination.path or "/"
    if destination.query:
        return f"{path}?{destination.query}"
    return path


__all__ = ["execute_request"]
