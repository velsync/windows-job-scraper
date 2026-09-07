"""Development-time alias for the build metadata collector.

The authoritative implementation lives inside the package at
``jobscraper.build_metadata`` so packaged (frozen) builds always carry it.
This module keeps the plan's ``build/build_metadata.py`` path importable in
a source checkout.
"""

from jobscraper.build_metadata import collect_build_metadata, _NOT_INSTALLED

__all__ = ["collect_build_metadata", "_NOT_INSTALLED"]
