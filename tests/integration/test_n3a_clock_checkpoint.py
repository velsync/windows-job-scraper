"""R2-F11: exercise the same clock seam and assertions used on Windows."""
import pytest
from scripts.n3a import n3a_check


@pytest.mark.parametrize('direction', ['FORWARD', 'BACKWARD'])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('budget', [1, 3])
def test_n3a_clock_proof(tmp_path, monkeypatch, direction, expired, budget):
    monkeypatch.setattr(n3a_check, 'WORK', tmp_path)
    proof = n3a_check.clock_anomaly_proof(
        direction, expired_before_jump=expired, max_attempts=budget)
    assert proof['result'] == 'PASS'
    assert proof['stale_durable_mutations'] == 0
    assert proof['system_clock_changed'] is False
