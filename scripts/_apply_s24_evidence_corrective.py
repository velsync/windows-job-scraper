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
