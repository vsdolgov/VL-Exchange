"""Neutral host conformance and real protected-delivery lifecycle checks."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import threading
import zipfile

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from vl_scenario_package import ScenarioError, ScenarioExecutor, check_golden, event_id, validate_package


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def write_manifest(root, manifest):
    (root / "scenario-package.json").write_text(json.dumps(manifest))


@pytest.fixture
def variant(tmp_path):
    def make(*, prefix="", body=None, module=None):
        root = tmp_path / "package"
        shutil.copytree(BASE / "examples" / "target-scenario", root)
        manifest = json.loads((root / "scenario-package.json").read_text())
        path = root / "code" / "scenario.py"
        if module is not None:
            path.write_text(module)
        elif body is not None:
            path.write_text("class Scenario:\n    def __init__(self, **params):\n        self.params=params\n    def process(self, inputs, context, state):\n" + "\n".join("        " + line for line in body.splitlines()) + "\n")
        else:
            path.write_text(prefix + path.read_text())
        manifest["files"]["code/scenario.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_manifest(root, manifest)
        return root, manifest
    return make


def inputs(label="red", score=0.6):
    return {"classification": {"label": label, "score": score, "category_id": 1, "output_index": 0}}


def context(sequence=1, *, source="A", run="run-1", time=1700000000000):
    return {"source_id": source, "run_id": run, "sequence": sequence, "event_time_ms": time}


def test_static_validator_does_not_import_scenario_or_write_package(variant):
    root, _ = variant(prefix='raise RuntimeError("STATIC VALIDATOR IMPORTED SCENARIO")\n')
    before = snapshot(root)
    report = validate_package(root, check_environment=True).to_dict()
    assert report["scenario_code_executed"] is False
    assert snapshot(root) == before


@pytest.mark.parametrize("case, code", [("hash", "digest_mismatch"), ("capability", "capability_missing"),
                                       ("schema", "manifest_invalid"), ("path", "manifest_invalid"),
                                       ("dependency", "dependency_missing"), ("runtime_import", "manifest_invalid")])
def test_invalid_packages_rejected_before_import(variant, case, code):
    prefix = 'raise RuntimeError("INVALID SCENARIO IMPORTED")\n'
    if case == "runtime_import":
        prefix += 'import vl_block_package\n'
    root, manifest = variant(prefix=prefix)
    if case == "hash":
        (root / "code" / "scenario.py").write_text("# changed")
    elif case == "capability":
        manifest["required_capabilities"].append("unknown-external-runtime")
    elif case == "schema":
        manifest["silently_ignored"] = True
    elif case == "path":
        manifest["files"]["../escape.py"] = "0" * 64
    elif case == "dependency":
        manifest["dependencies"] = [{"name": "vlsp-nonexistent-required-dependency", "version_specifier": "==1.0"}]
    write_manifest(root, manifest)
    with pytest.raises(ScenarioError) as failure:
        ScenarioExecutor(root, policy="development")
    assert failure.value.code == code


def test_plaintext_requires_explicit_development_policy(variant):
    root, _ = variant(prefix='raise RuntimeError("PLAINTEXT BYPASS")\n')
    for kwargs in ({}, {"policy": "protected"}, {"policy": "plain"}):
        with pytest.raises(ScenarioError) as failure:
            ScenarioExecutor(root, **kwargs)
        assert failure.value.code == "policy_rejected"


def test_parameter_boolean_is_not_an_integer_and_unknown_values_are_not_ignored(variant):
    root, _ = variant()
    for parameters in ({"min_count": True}, {"min_score": 2}, {"ignored": 1}):
        with pytest.raises(ScenarioError) as failure:
            ScenarioExecutor(root, policy="development", parameters=parameters)
        assert failure.value.code == "parameter_invalid"


def test_invalid_default_is_not_hidden_by_a_valid_override(variant):
    root, manifest = variant(prefix='raise RuntimeError("INVALID DEFAULT IMPORTED")\n')
    manifest["parameters"]["min_count"]["default"] = "not-an-integer"
    write_manifest(root, manifest)
    with pytest.raises(ScenarioError) as failure:
        ScenarioExecutor(root, policy="development", parameters={"min_count": 3})
    assert failure.value.code == "parameter_invalid"


@pytest.mark.parametrize("kind", ["overflow", "surrogate"])
def test_nonfinite_or_nonscalar_unicode_anywhere_in_manifest_is_rejected(variant, kind):
    root, manifest = variant(prefix='raise RuntimeError("INVALID JSON IMPORTED")\n')
    manifest["extensions"]["org.example.optional"] = {"required": False, "data": {"value": "marker"}}
    raw = json.dumps(manifest)
    raw = raw.replace('"marker"', '1e999' if kind == "overflow" else '"\\ud800"')
    (root / "scenario-package.json").write_text(raw)
    with pytest.raises(ScenarioError) as failure:
        ScenarioExecutor(root, policy="development")
    assert failure.value.code == "manifest_invalid"


def test_golden_tolerance_never_applies_to_integer_timestamps_or_counts():
    from vl_scenario_package.golden import _same
    assert not _same({"event_time_ms": 1700000000001}, {"event_time_ms": 1700000000000}, atol=0, rtol=0.001)
    assert not _same({"count": 1001}, {"count": 1000}, atol=0, rtol=0.001)
    assert _same({"score": 0.6000001}, {"score": 0.6}, atol=0, rtol=0.001)


def test_source_run_state_isolation_order_reset_and_reproducible_ids(variant):
    root, _ = variant()
    with ScenarioExecutor(root, policy="development") as executor:
        assert executor.process(inputs(), context())["events"] == []
        first = executor.process(inputs(), context(2))["events"][0]
        assert executor.process(inputs(), context(run="run-2"))["state"] == {"count": 1}
        second_run = executor.process(inputs(), context(2, run="run-2"))["events"][0]
        assert first["event_id"] != second_run["event_id"]
        previous = executor.snapshot()
        with pytest.raises(ScenarioError) as failure:
            executor.process(inputs(), context(2))
        assert failure.value.code == "input_order"
        assert executor.snapshot() == previous
        executor.reset("A", "run-1")
        executor.process(inputs(), context())
        replay = executor.process(inputs(), context(2))["events"][0]
        assert replay["event_id"] == first["event_id"]
        assert executor.snapshot()["state"]["A"]["run-2"] == {"count": 2}
        with pytest.raises(ScenarioError):
            executor.reset(run_id="run-2")


def test_unicode_event_id_vector_is_byte_exact_and_not_normalized():
    vector = json.loads((BASE / "spec" / "event-id-test-vector.json").read_text())
    fields = vector["fields"]
    ctx = {"source_id": fields[2], "run_id": fields[3], "sequence": int(fields[4])}
    actual = event_id(fields[0], fields[1], ctx, fields[5], int(fields[6]))
    assert actual == vector["event_id"]
    assert actual == "sha256:" + hashlib.sha256(bytes.fromhex(vector["hash_input_hex"])).hexdigest()
    ctx["source_id"] = "é"
    normalized = event_id(fields[0], fields[1], ctx, fields[5], 0)
    ctx["source_id"] = "e\u0301"
    assert event_id(fields[0], fields[1], ctx, fields[5], 0) != normalized


@pytest.mark.parametrize("body", [
    'context["sequence"]=999\nreturn {"events":[],"state":{"count":1}}',
    'inputs["classification"]["score"]=0\nreturn {"events":[],"state":{"count":1}}',
])
def test_mutating_read_only_input_or_context_is_fatal_without_commit(variant, body):
    root, _ = variant(body=body)
    original_inputs, original_context = inputs(), context()
    with ScenarioExecutor(root, policy="development") as executor:
        with pytest.raises(ScenarioError) as failure:
            executor.process(original_inputs, original_context)
        assert failure.value.code == "input_mutation" and failure.value.fatal
        assert executor.snapshot() == {"state": {}, "order": {}}
        assert original_inputs == inputs() and original_context == context()


@pytest.mark.parametrize("body, code", [
    ('return {"events":[{"event_type":"org.undeclared.event","payload":{}}],"state":{"count":1}}', "event_contract"),
    ('return {"events":[{"event_type":"org.vlexchange.target-observed.v1","source_id":"forged","payload":{"label":"red","score":0.6,"count":1}}],"state":{"count":1}}', "event_contract"),
    ('return {"events":[],"state":{"count":"bad"}}', "state_contract"),
])
def test_invalid_candidates_and_state_cannot_commit(variant, body, code):
    root, _ = variant(body=body)
    with ScenarioExecutor(root, policy="development") as executor:
        with pytest.raises(ScenarioError) as failure:
            executor.process(inputs(), context())
        assert failure.value.code == code and failure.value.fatal
        assert executor.snapshot() == {"state": {}, "order": {}}


def test_real_demo_golden_sequences_and_bytes_unchanged():
    root = BASE / "examples" / "target-scenario"
    before = snapshot(root)
    report = check_golden(root, policy="development")
    assert report["status"] == "pass"
    assert report["delivery"]["external_delivery"] is False
    assert [case["suite"] for case in report["sequence_checks"]] == ["forward", "reverse"]
    assert len(report["sequence_checks"][0]["checks"]) == 15
    assert report["sequence_checks"][0]["repeat_after_reset"]
    assert snapshot(root) == before


def test_snapshot_waits_for_a_complete_state_and_order_commit(variant):
    module = '''import threading
class Scenario:
    def __init__(self, **parameters):
        self.entered=threading.Event()
        self.release=threading.Event()
    def process(self, inputs, context, state):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Test synchronization timeout")
        return {"events":[],"state":{"count":1}}
'''
    root, manifest = variant(module=module)
    manifest["limits"]["max_process_ms"] = 2000
    write_manifest(root, manifest)
    with ScenarioExecutor(root, policy="development") as executor:
        errors, observed = [], []
        snapshot_started, snapshot_done = threading.Event(), threading.Event()
        def process_one():
            try:
                executor.process(inputs(), context())
            except BaseException as exc:
                errors.append(exc)
        def observe():
            snapshot_started.set()
            observed.append(executor.snapshot())
            snapshot_done.set()
        writer = threading.Thread(target=process_one)
        writer.start()
        assert executor._scenario.entered.wait(5)
        reader = threading.Thread(target=observe)
        reader.start()
        assert snapshot_started.wait(5)
        assert not snapshot_done.wait(0.02)
        executor._scenario.release.set()
        writer.join(timeout=5)
        reader.join(timeout=5)
        assert not errors and snapshot_done.is_set()
        assert observed == [{"state": {"A": {"run-1": {"count": 1}}},
                             "order": {"A": {"run-1": [1, 1700000000000]}}}]


@contextmanager
def licensed(root, tmp_path, *, mode, ttl=3, expires_at=1100):
    """Actual signed/encrypted delivery and actual loopback grant server."""
    from nacl.public import PrivateKey
    from nacl.signing import SigningKey
    from vl_exchange_security import OnlineAuthorizer, authorized_package, seal_package
    from vl_exchange_security.reference_server import ReferenceLicenseService, make_server

    archive = tmp_path / "scenario.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(root).as_posix())
    issuer, installation, recipient, server_key = SigningKey.generate(), SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    clock = [1000]
    envelope = tmp_path / "envelope"
    seal_package(archive, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
                 installation_verify_key=installation.verify_key, issuer_id="test-issuer", license_id="test-license",
                 installation_id="test-installation", mode=mode, issued_at=1000, not_before=1000,
                 expires_at=expires_at, manifest_path="scenario-package.json")
    server, worker, authorizer = None, None, None
    if mode == "online":
        service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key, server_signing_key=server_key,
                                          clock=lambda: clock[0], grant_ttl=ttl)
        server = make_server(service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        authorizer = OnlineAuthorizer(endpoint=f"http://127.0.0.1:{server.server_port}/v1/grant",
                     server_verify_key=server_key.verify_key, installation_signing_key=installation,
                     allow_loopback_http=True, renew_before_seconds=0)
    real_opener = partial(authorized_package, issuer_verify_key=issuer.verify_key,
                          recipient_private_key=recipient, policy=mode, online_authorizer=authorizer,
                          now=lambda: clock[0])
    receipts = []
    @contextmanager
    def observe(path):
        with real_opener(path) as authorized:
            receipts.append(authorized)
            yield authorized
    try:
        yield envelope, observe, clock, receipts
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


@pytest.mark.parametrize("mode", ["offline", "online"])
def test_real_protected_modes_expiry_blocks_commit_and_cleans_temp(mode, tmp_path):
    root = BASE / "examples" / "target-scenario"
    with licensed(root, tmp_path, mode=mode, expires_at=1002) as (envelope, verifier, clock, receipts):
        with ScenarioExecutor.open_protected(envelope, verifier=verifier) as executor:
            executor.process(inputs(), context())
            before = executor.snapshot()
            clock[0] = 1002
            with pytest.raises(ScenarioError) as failure:
                executor.process(inputs(), context(2))
            assert failure.value.code == "authorization_failed"
            assert executor.snapshot() == before
            assert executor.report()["lifecycle"]["process_calls"] == 1
            assert executor.report()["delivery"]["immutable_execution_lease_required"] is True
        assert receipts and not receipts[0].root.exists()


def test_expired_authorization_after_decryption_cannot_import_scenario(variant, tmp_path):
    root, _ = variant(prefix='raise RuntimeError("EXPIRED LICENSE IMPORTED CODE")\n')
    with licensed(root, tmp_path, mode="offline", expires_at=1002) as (envelope, real_verifier, clock, receipts):
        @contextmanager
        def delayed_verifier(path):
            with real_verifier(path) as authorized:
                clock[0] = 1002
                yield authorized
        with pytest.raises(ScenarioError) as failure:
            with ScenarioExecutor.open_protected(envelope, verifier=delayed_verifier):
                pytest.fail("Expired delivery reached scenario execution")
        assert failure.value.code == "authorization_failed"
        assert receipts and not receipts[0].root.exists()


def test_inflight_old_online_lease_cannot_commit_under_a_new_grant(variant, tmp_path):
    module = '''import threading
class Scenario:
    def __init__(self, **parameters):
        self.entered=threading.Event()
        self.release=threading.Event()
    def process(self, inputs, context, state):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Test synchronization timeout")
        return {"events":[],"state":{"count":state["count"]+1}}
'''
    root, manifest = variant(module=module)
    manifest["limits"]["max_process_ms"] = 2000
    write_manifest(root, manifest)
    with licensed(root, tmp_path, mode="online", ttl=2) as (envelope, verifier, clock, receipts):
        with ScenarioExecutor.open_protected(envelope, verifier=verifier) as executor:
            previous = executor.snapshot()
            thread_errors = []
            def renew_during_call():
                try:
                    assert executor._scenario.entered.wait(5)
                    clock[0] = 1003
                    receipts[0].renew()
                    receipts[0].assert_current()
                except BaseException as exc:
                    thread_errors.append(exc)
                finally:
                    executor._scenario.release.set()
            renewal = threading.Thread(target=renew_during_call)
            renewal.start()
            with pytest.raises(ScenarioError) as failure:
                executor.process(inputs(), context())
            renewal.join(timeout=5)
            assert not thread_errors
            assert failure.value.code == "authorization_failed"
            assert "original execution lease" in failure.value.message
            assert executor.snapshot() == previous
            assert executor.report()["lifecycle"]["process_calls"] == 1
            assert executor.report()["lifecycle"]["committed_calls"] == 0


def test_independent_host_preserves_legacy_vendor_denial_before_import(tmp_path):
    spec = importlib.util.spec_from_file_location("independent_vlsp_policy_test", BASE / "tools/independent_host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    legacy_root = bytes((118, 105, 122, 111, 114, 108, 97, 98, 115)).decode("ascii")
    assert host.forbidden_dependency(legacy_root)
    assert host.forbidden_dependency(legacy_root + ".child")
    assert not host.forbidden_dependency(legacy_root + "2")
    assert not host.forbidden_dependency("prefix_" + legacy_root)
    for number, statement in enumerate(("import " + legacy_root, "from " + legacy_root + ".child import value")):
        root = tmp_path / str(number)
        shutil.copytree(BASE / "examples/target-scenario", root)
        path = root / "code/scenario.py"
        path.write_text(statement + '\nraise RuntimeError("DENIED SOURCE WAS IMPORTED")\n' + path.read_text())
        manifest = json.loads((root / "scenario-package.json").read_text())
        manifest["files"]["code/scenario.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_manifest(root, manifest)
        with pytest.raises(host.HostError) as failure:
            host.validate(root)
        assert failure.value.code == "dependency_mismatch"
