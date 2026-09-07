"""Domain identifier generation.

Authority: docs/plans/slice-1-worker-implementation-plan-v0313.md S1.1.

IDs are TEXT primary keys shaped as ``<prefix>_<ulid-like>``:

* 10 chars Crockford-base32 timestamp (millisecond precision, UTC) —
  time-sortable for evidence/ordering debugging;
* 16 chars cryptographic randomness (80 bits) — collision-safe without any
  coordination;
* a short human-readable prefix identifying the entity family
  (``run``, ``req``, ``att``, ``src``, ``bnd``, ``job``, ``obs`` …).

The timestamp portion is *not* used for correctness: durable ordering uses
database UTC timestamps and explicit revision/evidence ordering (RUN-20/21).
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a fresh prefixed, time-sortable, collision-safe id."""
    if not prefix or not prefix.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"invalid id prefix: {prefix!r}")
    ts = _encode(int(time.time() * 1000) & ((1 << 50) - 1), 10)
    rand = _encode(secrets.randbits(80), 16)
    return f"{prefix}_{ts}{rand}"
