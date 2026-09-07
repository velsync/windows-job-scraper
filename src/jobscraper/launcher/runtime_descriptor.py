"""Service runtime descriptor: signed, PID+start-identity bound.

Authority: docs/spec/v0.3.1.3/04_security_and_authentication.md SEC-01
(protected runtime descriptor; stale advertised ports/service impersonation
cannot pass launcher validation); module 05 WIN-04; plan S0.8.

The descriptor is published by the service only after its loopback listener
is live. It binds:

* the loopback endpoint (host, OS-assigned port);
* the service-instance ID and service epoch;
* the OS process identity: PID *and* process start identity (so a recycled
  PID cannot impersonate the service);
* an HMAC-SHA256 authenticator over the canonical serialization of all other
  fields, keyed by the protected install secret.

Validation additionally confirms the live /health/live instance ID so an
unrelated process bound to an old port cannot impersonate the service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

DESCRIPTOR_SCHEMA_VERSION = 1
DESCRIPTOR_FILENAME = "service_descriptor.json"


class DescriptorError(Exception):
    """Raised when a descriptor is malformed, stale or unauthentic."""


@dataclass(frozen=True)
class RuntimeDescriptor:
    schema_version: int
    service_instance_id: str
    pid: int
    process_start_identity: str
    host: str
    port: int
    service_epoch: str
    created_at_utc: str
    authenticator: str


def descriptor_payload(desc: RuntimeDescriptor) -> dict:
    """The descriptor as a canonical JSON-serializable dict without the
    authenticator."""
    data = asdict(desc)
    data.pop("authenticator", None)
    return data


def canonical_payload_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_descriptor(
    *,
    service_instance_id: str,
    pid: int,
    process_start_identity: str,
    host: str,
    port: int,
    service_epoch: str,
    created_at_utc: str,
) -> RuntimeDescriptor:
    payload = {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "service_instance_id": service_instance_id,
        "pid": int(pid),
        "process_start_identity": process_start_identity,
        "host": host,
        "port": int(port),
        "service_epoch": service_epoch,
        "created_at_utc": created_at_utc,
    }
    return RuntimeDescriptor(**payload, authenticator="")


def sign_runtime_descriptor(payload: dict, secret: bytes) -> str:
    """HMAC-SHA256 over the canonical serialization of the payload."""
    if not secret:
        raise DescriptorError("signing requires the install secret")
    return hmac.new(secret, canonical_payload_json(payload), hashlib.sha256).hexdigest()


def complete_descriptor(desc: RuntimeDescriptor, secret: bytes) -> RuntimeDescriptor:
    payload = descriptor_payload(desc)
    return replace(desc, authenticator=sign_runtime_descriptor(payload, secret))


def verify_runtime_descriptor(desc: RuntimeDescriptor, secret: bytes) -> bool:
    """Verify the descriptor authenticator (constant-time)."""
    if not desc or not isinstance(desc.authenticator, str) or not desc.authenticator:
        return False
    payload = descriptor_payload(desc)
    expected = sign_runtime_descriptor(payload, secret)
    return hmac.compare_digest(expected, desc.authenticator)


# ------------------------------------------------------------------ identity


def process_start_identity(pid: int) -> str:
    """A stable per-process start identity that defeats PID reuse.

    Windows: process creation time via pywin32 (PROCESS_QUERY_LIMITED_INFORMATION).
    POSIX: the kernel starttime field from /proc/<pid>/stat.
    Returns an empty string when the identity cannot be determined (the
    validator then still enforces HMAC + live-PID + health instance match).
    """
    pid = int(pid)
    if sys.platform == "win32":  # pragma: no cover - Windows native
        import win32api
        import win32con
        import win32process

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            return ""
        try:
            _create, _exit, _kernel, _user = win32process.GetProcessTimes(handle)
        finally:
            handle.Close()
        try:
            create = float(_create)
        except (TypeError, ValueError):
            return ""
        return f"win:{create:.6f}"
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    # field 22 (1-based) is starttime; fields after comm may contain spaces.
    tail = stat_text[stat_text.rindex(")") + 1 :].split()
    try:
        starttime = tail[19]  # ( ) comm state ppid pgrp session tty tpgid flags... -> index 19
    except IndexError:
        return ""
    return f"posix:{starttime}"


def pid_alive(pid: int) -> bool:
    pid = int(pid)
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows native
        import win32api
        import win32con

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            return False
        handle.Close()
        return True
    try:
        Path(f"/proc/{pid}").stat()
        # A zombie process is not a live service.
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        state = stat_text[stat_text.rindex(")") + 1 :].split()[0]
        return state != "Z"
    except OSError:
        return False


# ----------------------------------------------------------------- persistence


def descriptor_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / DESCRIPTOR_FILENAME


def write_runtime_descriptor(runtime_dir: Path, desc: RuntimeDescriptor) -> None:
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    data = asdict(desc)
    path = descriptor_path(runtime_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_runtime_descriptor(runtime_dir: Path) -> RuntimeDescriptor | None:
    path = descriptor_path(runtime_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DescriptorError("corrupt runtime descriptor")
    valid_fields = {f.name for f in fields(RuntimeDescriptor)}
    if not isinstance(data, dict) or not valid_fields <= set(data):
        raise DescriptorError("runtime descriptor missing required fields")
    try:
        return RuntimeDescriptor(
            schema_version=int(data["schema_version"]),
            service_instance_id=str(data["service_instance_id"]),
            pid=int(data["pid"]),
            process_start_identity=str(data["process_start_identity"]),
            host=str(data["host"]),
            port=int(data["port"]),
            service_epoch=str(data["service_epoch"]),
            created_at_utc=str(data["created_at_utc"]),
            authenticator=str(data["authenticator"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DescriptorError(f"malformed runtime descriptor: {exc}") from exc


def remove_runtime_descriptor(runtime_dir: Path) -> None:
    path = descriptor_path(runtime_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ validation


def validate_descriptor_freshness(desc: RuntimeDescriptor, max_age_s: float = 600.0, *, now=None) -> None:
    """Reject descriptors that are structurally too old (stale leftovers)."""
    from jobscraper.timeutil import parse_rfc3339, utc_now

    now_dt = now or utc_now()
    try:
        created = parse_rfc3339(desc.created_at_utc)
    except ValueError as exc:
        raise DescriptorError("descriptor timestamp unparseable") from exc
    age = (now_dt - created).total_seconds()
    if age < -300:
        raise DescriptorError("descriptor timestamp in the future (clock anomaly)")
    if age > max_age_s:
        raise DescriptorError(f"descriptor stale (age {age:.0f}s)")


def check_descriptor_identity(desc: RuntimeDescriptor, secret: bytes) -> None:
    """HMAC + PID liveness + start-identity checks (no network)."""
    if desc.schema_version != DESCRIPTOR_SCHEMA_VERSION:
        raise DescriptorError("unsupported descriptor schema version")
    if desc.host != "127.0.0.1":
        raise DescriptorError("descriptor is not loopback")
    if not (0 < desc.port < 65536):
        raise DescriptorError("descriptor port out of range")
    if not verify_runtime_descriptor(desc, secret):
        raise DescriptorError("descriptor authenticator mismatch")
    if not pid_alive(desc.pid):
        raise DescriptorError("descriptor PID is not alive")
    if desc.process_start_identity:
        current_identity = process_start_identity(desc.pid)
        if not current_identity or current_identity != desc.process_start_identity:
            raise DescriptorError("descriptor PID start-identity mismatch (PID reuse)")


def service_health_instance_id(desc: RuntimeDescriptor, *, timeout_s: float = 2.0) -> str | None:
    """Query /health/live on the descriptor endpoint; return its service
    instance ID (or None when unreachable/malformed)."""
    import json as _json
    import urllib.error
    import urllib.request

    url = f"http://{desc.host}:{desc.port}/health/live"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                return None
            payload = _json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict):
        return None
    instance_id = payload.get("service_instance_id")
    return str(instance_id) if instance_id else None
