"""Built-in adapter registry (02 §9, ACQ-08 import boundary).

Only built-in, manifest-validated adapters are registered. There is no
dynamic import and no third-party plugin loading in v0.3.1.3; imported
declarative source config never gains executable-code privilege.
"""

from __future__ import annotations

from jobscraper.adapters.feed_api import FeedApiAdapter

BUILTIN_ADAPTERS = {
    "json_api_feed": FeedApiAdapter,
}


def get_adapter(adapter_id: str):
    """Return the built-in adapter class for ``adapter_id`` (KeyError if none)."""
    return BUILTIN_ADAPTERS[adapter_id]


__all__ = ["BUILTIN_ADAPTERS", "get_adapter"]
