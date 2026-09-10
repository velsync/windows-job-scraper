"""Built-in adapter registry (02 §9, ACQ-08 import boundary).

Only built-in, manifest-validated adapters are registered. There is no
dynamic import and no third-party plugin loading in v0.3.1.3; imported
declarative source config never gains executable-code privilege.

``build_adapter`` is the single construction path the host driver uses: the
adapter class comes from this fixed table and its config comes from the
pinned binding revision, so an adapter identity can never be selected or
parameterized by scraped content (ARC-04.3, ACQ-08).
"""

from __future__ import annotations

from typing import Any, Mapping

from jobscraper.adapters.ashby import AshbyAdapter
from jobscraper.adapters.feed_api import FeedApiAdapter
from jobscraper.adapters.generic_discovery import GenericDiscoveryAdapter
from jobscraper.adapters.greenhouse import GreenhouseAdapter
from jobscraper.adapters.lever import LeverAdapter

BUILTIN_ADAPTERS = {
    "json_api_feed": FeedApiAdapter,
    "generic_discovery": GenericDiscoveryAdapter,
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}


def get_adapter(adapter_id: str):
    """Return the built-in adapter class for ``adapter_id`` (KeyError if none)."""
    return BUILTIN_ADAPTERS[adapter_id]


def build_adapter(adapter_id: str, config: Mapping[str, Any] | None):
    """Construct one built-in adapter from its binding-revision config.

    ``KeyError`` for an unregistered identity, ``ValueError`` for a config the
    adapter does not accept (unknown keys, unusable values).  Both are typed
    refusals: there is no fallback adapter and no dynamic import.
    """
    adapter_cls = get_adapter(adapter_id)
    return adapter_cls.from_config(dict(config or {}))


__all__ = ["BUILTIN_ADAPTERS", "build_adapter", "get_adapter"]
