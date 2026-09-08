"""Direct application link (01 §39, PROD-05, PROD-06).

Opening the best direct application URL is supported. Automatic
submission is NOT: this module can only select and open a safelink-
approved URL in the user's browser. Acquisition/browser automation MUST
NOT silently perform the application itself (PROD-06), and only
http/https URLs may ever be emitted as clickable/openable external links
(PROD-05).
"""

from __future__ import annotations

import sqlite3
import webbrowser

from jobscraper.net.safelinks import safe_external_url


def best_application_url(conn: sqlite3.Connection, job_id: str) -> str | None:
    """The direct application URL of the canonical provenance winner,
    or None when no safe URL exists."""
    row = conn.execute(
        """
        SELECT js.application_url FROM jobs j
        JOIN job_sources js ON js.id = j.canonical_provenance_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return safe_external_url(row["application_url"])


def open_apply_url(url: str, *, opener=None) -> bool:
    """Open one application URL in the user's browser.

    Returns True when opened; refuses (False) any URL that is not
    safelink-approved. The optional ``opener`` exists for tests and host
    integration; the default is the OS browser.
    """
    safe = safe_external_url(url)
    if safe is None:
        return False
    open_url = opener or webbrowser.open
    open_url(safe)
    return True


__all__ = ["best_application_url", "open_apply_url"]
