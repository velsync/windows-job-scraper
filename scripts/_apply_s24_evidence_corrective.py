from pathlib import Path

p = Path("src/jobscraper/runtime/provisioning.py")
s = p.read_text(encoding="utf-8")

anchor = '''def _registered_definition_state(\n'''
helpers = '''def _require_existing_source(conn: sqlite3.Connection, source_id: str) -> None:\n    if conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:\n        raise ProvisioningError(f"Source {source_id!r} does not exist; refusing orphan evidence")\n\n\ndef _validate_fingerprint_decision(fingerprint, decision) -> None:\n    if decision is None:\n        return\n    if fingerprint is None:\n        raise ProvisioningError("route decision requires fingerprint evidence")\n    if decision.fingerprint_family != fingerprint.family:\n        raise ProvisioningError("route decision fingerprint family does not match fingerprint evidence")\n    if decision.fingerprint_confidence != fingerprint.confidence:\n        raise ProvisioningError("route decision fingerprint confidence does not match fingerprint evidence")\n\n\n'''
if helpers not in s:
    assert s.count(anchor) == 1
    s = s.replace(anchor, helpers + anchor, 1)

old = '''    ts = now or db_utc_now(conn)\n    row_id = new_id("fp")\n'''
new = '''    _require_existing_source(conn, source_id)\n    ts = now or db_utc_now(conn)\n    row_id = new_id("fp")\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

old = '''    \"\"\"Insert append-only route-decision evidence.\"\"\"\n    ts = now or db_utc_now(conn)\n    row_id = new_id("rd")\n'''
new = '''    \"\"\"Insert append-only route-decision evidence with durable causal authority.\"\"\"\n    _require_existing_source(conn, source_id)\n    _validate_fingerprint_decision(fingerprint, decision)\n    prior = conn.execute(\n        "SELECT 1 FROM ats_fingerprints WHERE source_id = ? AND family = ? AND confidence = ? LIMIT 1",\n        (source_id, fingerprint.family, fingerprint.confidence),\n    ).fetchone()\n    if prior is None:\n        raise ProvisioningError("route decision requires a matching persisted fingerprint first")\n    ts = now or db_utc_now(conn)\n    row_id = new_id("rd")\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

old = '''    ts = now or db_utc_now(conn)\n    config_json = _canonical_json(config)\n\n    adapter_def = conn.execute(\n'''
new = '''    ts = now or db_utc_now(conn)\n    config_json = _canonical_json(config)\n    # Reject impossible evidence pairs before any Source/Binding mutation.\n    _validate_fingerprint_decision(fingerprint, decision)\n\n    adapter_def = conn.execute(\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")

# Reconcile only the legacy evidence tests that previously bypassed the
# durable Source/fingerprint causal prerequisites. Do not weaken the runtime
# guard and do not seed Sources globally, because other provisioning tests
# intentionally start from an empty Sources table.
t = Path("tests/unit/test_provisioning.py")
ts = t.read_text(encoding="utf-8")

fixture_anchor = '''\n\n# ---------------------------------------------------------------------------\n# Fingerprint recording (append-only evidence)\n# ---------------------------------------------------------------------------\n'''
seed_helper = '''\n\ndef _seed_source(conn, source_id: str) -> None:\n    now = "2026-09-08T00:00:00.000000Z"\n    conn.execute(\n        "INSERT INTO sources "\n        "(id, display_name, source_family, entry_url, canonical_host, created_at, updated_at) "\n        "VALUES (?, ?, 'ATS_BOARD', ?, 'boards.greenhouse.io', ?, ?)",\n        (source_id, f"Evidence {source_id}", f"https://boards.greenhouse.io/{source_id}", now, now),\n    )\n    conn.commit()\n'''
assert ts.count(fixture_anchor) == 1
if seed_helper not in ts:
    ts = ts.replace(fixture_anchor, seed_helper + fixture_anchor, 1)

seed_replacements = [
    (
        '''    def test_record_fingerprint_inserts_row(self, populated_db):\n        conn = populated_db.conn\n        fp = AtsFingerprint(\n''',
        '''    def test_record_fingerprint_inserts_row(self, populated_db):\n        conn = populated_db.conn\n        _seed_source(conn, "src-test")\n        fp = AtsFingerprint(\n''',
    ),
    (
        '''    def test_record_fingerprint_is_append_only(self, populated_db):\n        conn = populated_db.conn\n        fp = AtsFingerprint(\n''',
        '''    def test_record_fingerprint_is_append_only(self, populated_db):\n        conn = populated_db.conn\n        _seed_source(conn, "src-1")\n        fp = AtsFingerprint(\n''',
    ),
    (
        '''    def test_record_fingerprint_stores_evidence_json(self, populated_db):\n        conn = populated_db.conn\n        fp = AtsFingerprint(\n''',
        '''    def test_record_fingerprint_stores_evidence_json(self, populated_db):\n        conn = populated_db.conn\n        _seed_source(conn, "src-1")\n        fp = AtsFingerprint(\n''',
    ),
    (
        '''    def test_record_route_decision_inserts_row(self, populated_db):\n        conn = populated_db.conn\n        fp = AtsFingerprint(\n''',
        '''    def test_record_route_decision_inserts_row(self, populated_db):\n        conn = populated_db.conn\n        _seed_source(conn, "src-test")\n        fp = AtsFingerprint(\n''',
    ),
    (
        '''    def test_record_route_decision_is_append_only(self, populated_db):\n        conn = populated_db.conn\n        fp = AtsFingerprint(\n''',
        '''    def test_record_route_decision_is_append_only(self, populated_db):\n        conn = populated_db.conn\n        _seed_source(conn, "src-1")\n        fp = AtsFingerprint(\n''',
    ),
]
for old_text, new_text in seed_replacements:
    assert ts.count(old_text) == 1, old_text
    ts = ts.replace(old_text, new_text, 1)

old = '''        row_id = record_route_decision(\n            conn,\n            source_id="src-test",\n            fingerprint=fp,\n'''
new = '''        record_fingerprint(\n            conn, source_id="src-test", url="https://boards.greenhouse.io/acme",\n            fingerprint=fp, now="2026-09-08T00:00:00.500000Z",\n        )\n        row_id = record_route_decision(\n            conn,\n            source_id="src-test",\n            fingerprint=fp,\n'''
assert ts.count(old) == 1
ts = ts.replace(old, new, 1)

old = '''        r1 = record_route_decision(\n            conn, source_id="src-1", fingerprint=fp,\n'''
new = '''        record_fingerprint(\n            conn, source_id="src-1", url="https://boards.greenhouse.io/acme",\n            fingerprint=fp, now="2026-09-08T00:00:00.500000Z",\n        )\n        r1 = record_route_decision(\n            conn, source_id="src-1", fingerprint=fp,\n'''
assert ts.count(old) == 1
ts = ts.replace(old, new, 1)

t.write_text(ts, encoding="utf-8")
