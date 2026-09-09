from pathlib import Path

# Reconcile the legacy restart E2E test with the accepted S2.5 invariant:
# COMPLETE listing membership is durable before detail enrichment finishes;
# pending detail work keeps the run partial only until that work is drained.
p = Path("tests/integration/test_greenhouse_e2e.py")
s = p.read_text(encoding="utf-8")

old = '''    It also claims no terminal enumeration of its own: absence authority stays\n    with the generation that proved membership (RUN-13), so an interrupted run\n    can never expire postings.\n'''
new = '''    Listing membership is already durable and complete after the first pass;\n    accepted detail work keeps the run partial only until that work is drained.\n    Restart must reuse that same coverage generation and never refetch listing truth.\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

old = '''    # restart: the same run, driven again from durable state only\n    assert execute_run(db.conn, run_id) == "PARTIAL"\n'''
new = '''    # restart: the same run, driven again from durable state only\n    assert execute_run(db.conn, run_id) == "SUCCEEDED"\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

old = '''    generations = _coverage(db)\n    assert len(generations) == 2\n    assert generations[0]["completion_state"] == "BUDGET_EXHAUSTED"\n    assert generations[1]["completion_state"] == "PARTIAL"\n    assert generations[1]["terminal_enumeration_proven"] == 0\n    # nothing was aged toward expiry by the interrupted or resumed generation\n'''
new = '''    generations = _coverage(db)\n    assert len(generations) == 1\n    assert generations[0]["completion_state"] == "COMPLETE"\n    assert generations[0]["terminal_enumeration_proven"] == 1\n    assert generations[0]["pages_completed"] == 1\n    # nothing was aged toward expiry while accepted detail work was pending\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")

# The S2.5 parser now records rejected-membership count explicitly even when
# zero, so exact coverage-proposal assertions must include the field.
p = Path("tests/unit/test_greenhouse_adapter.py")
s = p.read_text(encoding="utf-8")

old = '''        assert outcome.coverage_proposal == {\n            "declared_total": 3,\n            "observed": 3,\n            "detail_tasks": 2,\n        }\n'''
new = '''        assert outcome.coverage_proposal == {\n            "declared_total": 3,\n            "observed": 3,\n            "detail_tasks": 2,\n            "rejected_members": 0,\n        }\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

old = '''        assert outcome.coverage_proposal == {\n            "declared_total": 5,\n            "observed": 2,\n            "detail_tasks": 2,\n        }\n'''
new = '''        assert outcome.coverage_proposal == {\n            "declared_total": 5,\n            "observed": 2,\n            "detail_tasks": 2,\n            "rejected_members": 0,\n        }\n'''
assert s.count(old) == 1
s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")
