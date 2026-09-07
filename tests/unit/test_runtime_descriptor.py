"""Unit tests for the signed runtime descriptor (S0.8)."""

import os
import time

import pytest

from jobscraper.launcher.runtime_descriptor import (
    DescriptorError,
    check_descriptor_identity,
    complete_descriptor,
    descriptor_payload,
    load_runtime_descriptor,
    make_descriptor,
    pid_alive,
    process_start_identity,
    remove_runtime_descriptor,
    sign_runtime_descriptor,
    verify_runtime_descriptor,
    write_runtime_descriptor,
)
from jobscraper.timeutil import utc_now_s

SECRET = b"\x02" * 32


def make_test_descriptor(**overrides) -> object:
    values = dict(
        service_instance_id="svc-test",
        pid=os.getpid(),
        process_start_identity=process_start_identity(os.getpid()),
        host="127.0.0.1",
        port=54321,
        service_epoch="epoch-abc",
        created_at_utc=utc_now_s(),
    )
    values.update(overrides)
    return complete_descriptor(make_descriptor(**values), SECRET)


class TestSignVerify:
    def test_roundtrip(self):
        desc = make_test_descriptor()
        assert desc.authenticator
        assert verify_runtime_descriptor(desc, SECRET) is True

    def test_wrong_secret_rejected(self):
        desc = make_test_descriptor()
        assert verify_runtime_descriptor(desc, b"\x03" * 32) is False

    def test_missing_authenticator_rejected(self):
        desc = make_test_descriptor()
        assert verify_runtime_descriptor(
            type(desc)(**{**descriptor_payload(desc), "authenticator": ""}), SECRET
        ) is False

    @pytest.mark.parametrize(
        "field,value",
        [
            ("port", 54322),
            ("pid", 424242),
            ("service_instance_id", "svc-evil"),
            ("host", "localhost"),
            ("service_epoch", "epoch-evil"),
        ],
    )
    def test_tampered_field_rejected(self, field, value):
        desc = make_test_descriptor()
        payload = descriptor_payload(desc)
        payload[field] = value
        tampered = type(desc)(**payload, authenticator=desc.authenticator)
        assert verify_runtime_descriptor(tampered, SECRET) is False

    def test_canonical_serialization_is_deterministic(self):
        payload = descriptor_payload(make_test_descriptor())
        a = sign_runtime_descriptor(payload, SECRET)
        b = sign_runtime_descriptor(dict(reversed(list(payload.items()))), SECRET)
        assert a == b


class TestIdentity:
    def test_own_process_identity_is_stable_and_alive(self):
        ident = process_start_identity(os.getpid())
        assert process_start_identity(os.getpid()) == ident
        assert pid_alive(os.getpid()) is True

    def test_dead_pid_not_alive(self):
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert pid_alive(proc.pid) is False

    def test_start_identity_differs_between_processes(self):
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, %r); "
            "from jobscraper.launcher.runtime_descriptor import process_start_identity; "
            "import os; print(process_start_identity(os.getpid()))"
            % ("src",)
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd="."
        )
        if out.returncode != 0 or not out.stdout.strip():  # pragma: no cover
            pytest.skip("platform without start-identity support")
        assert out.stdout.strip() != process_start_identity(os.getpid())


class TestCheckIdentity:
    def test_valid_identity_passes(self):
        desc = make_test_descriptor()
        check_descriptor_identity(desc, SECRET)

    def test_wrong_host_rejected(self):
        desc = make_test_descriptor(host="localhost")
        with pytest.raises(DescriptorError):
            check_descriptor_identity(desc, SECRET)

    def test_bad_port_rejected(self):
        desc = make_test_descriptor(port=0)
        with pytest.raises(DescriptorError):
            check_descriptor_identity(desc, SECRET)

    def test_dead_pid_rejected(self):
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            ident = process_start_identity(proc.pid)
            desc = make_test_descriptor(pid=proc.pid, process_start_identity=ident)
        finally:
            proc.kill()
            proc.wait()
        with pytest.raises(DescriptorError):
            check_descriptor_identity(desc, SECRET)

    def test_pid_reuse_rejected(self):
        # Live PID but a start identity from a different process epoch.
        desc = make_test_descriptor(process_start_identity="win:1.000000")
        with pytest.raises(DescriptorError):
            check_descriptor_identity(desc, SECRET)


class TestPersistence:
    def test_write_load_remove_roundtrip(self, tmp_path):
        desc = make_test_descriptor()
        write_runtime_descriptor(tmp_path, desc)
        loaded = load_runtime_descriptor(tmp_path)
        assert loaded == desc
        remove_runtime_descriptor(tmp_path)
        assert load_runtime_descriptor(tmp_path) is None

    def test_corrupt_descriptor_rejected(self, tmp_path):
        (tmp_path / "service_descriptor.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(DescriptorError):
            load_runtime_descriptor(tmp_path)

    def test_missing_fields_rejected(self, tmp_path):
        (tmp_path / "service_descriptor.json").write_text('{"pid": 1}', encoding="utf-8")
        with pytest.raises(DescriptorError):
            load_runtime_descriptor(tmp_path)
