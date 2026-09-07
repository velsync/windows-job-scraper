"""HTTP executor with connection-bound SSRF enforcement.

Authority: docs/spec/v0.3.1.3/02 (adapters plan, executors perform I/O);
04 section 5.1 (bind destination validation to the actual connection).

This executor uses stdlib ``http.client`` over sockets it creates itself so
that the *validated* address is the address actually connected to (no
DNS-rebinding TOCTOU). Redirects are followed manually with per-hop
re-validation. Body size and duration are capped. ``304`` is surfaced for the
revalidation layer. No proxy environment variables are honored.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from jobscraper.acquisition.contracts import ResultEnvelope
from jobscraper.security.netpolicy import NetworkPolicy, parse_url
from jobscraper.security.redaction import redact_headers
from jobscraper.timeutil import utc_now_s

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5

SENSITIVE_REQ_HEADERS = {"cookie", "authorization", "proxy-authorization"}


@dataclass(frozen=True)
class HttpFetchResult:
    envelope: ResultEnvelope
    ok: bool


class HttpExecutor:
    """Executes validated read-acquisition HTTP requests."""

    def __init__(self, policy: NetworkPolicy, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.policy = policy
        self.timeout_s = timeout_s

    def execute(self, envelope_inputs: dict, plan: dict) -> HttpFetchResult:
        """Execute one RequestPlan (as dict) and return a ResultEnvelope.

        ``plan`` keys: method, url, headers (without secrets), validators
        (If-None-Match/If-Modified-Since), timeout_s, max_bytes,
        max_redirects, expected content types.
        """
        started = time.monotonic()
        method = (plan.get("method") or "GET").upper()
        url = plan["url"]
        max_bytes = int(plan.get("max_bytes") or DEFAULT_MAX_BYTES)
        max_redirects = int(plan.get("max_redirects") or DEFAULT_MAX_REDIRECTS)
        timeout_s = float(plan.get("timeout_s") or self.timeout_s)
        validators = dict(plan.get("validators") or {})
        headers = {
            k: v for k, v in (plan.get("headers") or {}).items()
            if k.lower() not in SENSITIVE_REQ_HEADERS
        }
        headers.setdefault("User-Agent", "WindowsJobScraper/0.3 (+local-first; respectful crawler)")
        headers.setdefault("Accept", plan.get("accept") or "*/*")

        redirect_chain: list[str] = []
        current_url = url
        current_method = method
        total_bytes = 0

        for hop in range(max_redirects + 1):
            failure = None
            try:
                decision = self.policy.check_url(current_url)
            except Exception as exc:
                envelope = self._envelope(
                    envelope_inputs, plan, url, current_url, None, None, None, None,
                    redirect_chain, started, total_bytes, None,
                    failure_kind="POLICY_REJECTED", failure_detail=str(exc), validators=validators,
                )
                return HttpFetchResult(envelope=envelope, ok=False)

            body, status, resp_headers, content_type, was_304, fetched_url = self._request_once(
                current_url, current_method, headers, decision, timeout_s, max_bytes, failure_box := {}
            )
            if failure_box.get("failure"):
                failure = failure_box["failure"]
                envelope = self._envelope(
                    envelope_inputs, plan, url, current_url, None, None, None, None,
                    redirect_chain, started, total_bytes, None,
                    failure_kind=failure[0], failure_detail=failure[1], validators=validators,
                )
                return HttpFetchResult(envelope=envelope, ok=False)

            total_bytes += len(body or b"")

            if status in (301, 302, 303, 307, 308) and not plan.get("no_follow"):
                location = resp_headers.get("location") or resp_headers.get("Location")
                if not location:
                    break
                redirect_chain.append(f"{status}:{location[:512]}")
                if hop >= max_redirects:
                    envelope = self._envelope(
                        envelope_inputs, plan, url, current_url, status, resp_headers,
                        content_type, body, redirect_chain, started, total_bytes, fetched_url,
                        failure_kind="UNSUPPORTED", failure_detail="too many redirects",
                        validators=validators,
                    )
                    return HttpFetchResult(envelope=envelope, ok=False)
                try:
                    next_url = self._resolve_redirect(location, current_url)
                    self.policy.check_redirect(location, current_url)
                except Exception as exc:
                    envelope = self._envelope(
                        envelope_inputs, plan, url, current_url, status, resp_headers,
                        content_type, body, redirect_chain, started, total_bytes, fetched_url,
                        failure_kind="POLICY_REJECTED",
                        failure_detail=f"redirect to forbidden destination: {exc}",
                        validators=validators,
                    )
                    return HttpFetchResult(envelope=envelope, ok=False)
                if status == 303 and current_method == "POST":
                    current_method = "GET"
                current_url = next_url
                continue

            # Terminal response.
            envelope = self._envelope(
                envelope_inputs, plan, url, current_url, status, resp_headers,
                content_type, body, redirect_chain, started, total_bytes, fetched_url,
                failure_kind=None, failure_detail=None, validators=validators,
                was_304=was_304,
            )
            ok = status is not None and 200 <= status < 400
            return HttpFetchResult(envelope=envelope, ok=ok)

        # Unreachable guard.
        envelope = self._envelope(
            envelope_inputs, plan, url, current_url, None, None, None, None,
            redirect_chain, started, total_bytes, None,
            failure_kind="UNSUPPORTED", failure_detail="redirect budget exhausted",
            validators=validators,
        )
        return HttpFetchResult(envelope=envelope, ok=False)

    # ------------------------------------------------------------------ internals
    def _request_once(
        self, url: str, method: str, headers: dict, decision, timeout_s: float, max_bytes: int, failure_box: dict
    ):
        scheme, host, port, path = parse_url(url)
        addr = decision.addresses[0]  # connect to the validated address
        was_304 = False
        try:
            sock = socket.create_connection((addr, port), timeout=timeout_s)
        except (socket.timeout, TimeoutError):
            failure_box["failure"] = ("TIMEOUT", f"connect timeout to {addr}")
            return None, None, None, None, was_304, None
        except OSError as exc:
            failure_box["failure"] = ("CONNECT_ERROR", f"{type(exc).__name__}: {exc}")
            return None, None, None, None, was_304, None
        try:
            if scheme == "https":
                # TLS with the original hostname (SNI + verification), while
                # the socket is pinned to the validated address.
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
            conn.sock = sock
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            content_type = resp_headers.get("content-type")
            if status == 304:
                body = b""
                was_304 = True
            else:
                # Stream with a hard byte cap.
                chunks: list[bytes] = []
                remaining = max_bytes
                while True:
                    chunk = resp.read(min(65536, remaining if remaining > 0 else 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
                body = b"".join(chunks)
            return body, status, resp_headers, content_type, was_304, url
        except (socket.timeout, TimeoutError):
            failure_box["failure"] = ("TIMEOUT", "read timeout")
            return None, None, None, None, was_304, None
        except ssl.SSLError as exc:
            failure_box["failure"] = ("TLS_ERROR", str(exc))
            return None, None, None, None, was_304, None
        except (http.client.HTTPException, OSError) as exc:
            failure_box["failure"] = ("CONNECT_ERROR", f"{type(exc).__name__}: {exc}")
            return None, None, None, None, was_304, None
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _resolve_redirect(self, location: str, base_url: str) -> str:
        if urlsplit(location).scheme or urlsplit(location).netloc:
            return location
        base = urlsplit(base_url)
        return urlunsplit((base.scheme, base.netloc, location or "/", "", ""))

    def _envelope(
        self,
        inputs: dict,
        plan: dict,
        requested_url: str,
        final_url: str | None,
        status: int | None,
        resp_headers: dict | None,
        content_type: str | None,
        body: bytes | None,
        redirect_chain: list[str],
        started: float,
        bytes_downloaded: int,
        fetched_url: str | None,
        *,
        failure_kind: str | None,
        failure_detail: str | None,
        validators: dict | None = None,
        was_304: bool = False,
    ) -> ResultEnvelope:
        body_hash = hashlib.sha256(body).hexdigest() if body else None
        normalized = None
        if body:
            normalized = hashlib.sha256(
                ((content_type or "") + "|" + (body_hash or "")).encode("utf-8")
            ).hexdigest()
        return ResultEnvelope(
            execution_plan_id=inputs["execution_plan_id"],
            request_id=inputs["request_id"],
            attempt_id=inputs["attempt_id"],
            run_source_plan_id=inputs["run_source_plan_id"],
            source_id=inputs["source_id"],
            binding_id=inputs["binding_id"],
            binding_revision_id=inputs["binding_revision_id"],
            adapter_id=inputs["adapter_id"],
            adapter_version=inputs["adapter_version"],
            strategy=inputs["strategy"],
            execution_class=inputs["execution_class"],
            requested_url=requested_url,
            final_url=(fetched_url or final_url),
            status_code=status,
            headers_redacted=redact_headers(resp_headers or {}),
            content_type=content_type,
            body=body,
            body_hash=body_hash,
            normalized_content_hash=normalized,
            fetched_at=utc_now_s(),
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_downloaded=bytes_downloaded,
            redirect_chain=list(redirect_chain),
            transport="HTTP",
            browser_used=False,
            robots_decision=plan.get("robots_decision"),
            validators_sent=dict(validators or {}),
            was_304=was_304,
            resource_blocking_applied=None,
            failure_kind=failure_kind,
            failure_detail=failure_detail,
        )
