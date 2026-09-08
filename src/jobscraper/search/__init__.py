"""FTS5-backed local job search (01 §45).

Ownership rules:

* the FTS5 virtual table and its sync triggers are created **only** by this
  package, capability-gated on ``db.connection.fts5_available`` — never by a
  migration step, so a host without FTS5 migrates cleanly;
* capability honesty is structural: ``search_capability`` records
  ``FTS5_ACTIVE`` only after the FTS5 objects really exist, and records
  ``SUBSTRING_FALLBACK`` (with an explicit warning) otherwise; search
  responses always carry the active mode and never claim BM25 in fallback
  mode;
* index maintenance is host-owned (invoked from the fenced canonical
  pipeline) and revision-checked through ``job_search_state`` so re-indexing
  never duplicates or loses rows;
* structured filters (listing status, company, source, remote, discovery
  window) are applied outside FTS on the canonical tables.
"""
