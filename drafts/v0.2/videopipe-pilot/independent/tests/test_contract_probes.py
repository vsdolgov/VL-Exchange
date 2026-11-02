"""Synthetic VLSP contract probes; these tests do not perform ML inference.

The oracle is VLSP v0.2 sections 2--6 and the inherited v0.1 event identity
and fatality policy.  Each probe gets a private package with a new synthetic
scenario identity and a complete, recomputed inventory.  Exact model identity
metadata is paired with explicitly fake provider callbacks to isolate host
contract enforcement.  This suite is not evidence of model interoperability,
real inference, protected delivery, or third-party platform compatibility.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import textwrap
from types import SimpleNamespace

import pytest

from independent.engine import Engine, Fault


PILOT = Path(__file__).resolve().parents[2]
EVENT_TYPE = "org.vlexchange.synthetic.contract-probe.v1"
DETECTION = {"object_id": "raw-0", "label": "person", "category_id": 1,
             "score": 0.8, "bbox": [0.0, 0.0, 0.5, 1.0]}


class FakeProviders:
    """Contract-only fakes: no weights, frame pixels, or model loaders."""

    def __init__(self, manifest, on_infer=None):
        self.counts = Counter()
        self.on_infer = on_infer
        self.models = {}
        self.trackers = {}
        for declaration in manifest["models"]:
            alias = declaration["alias"]
            self.models[alias] = SimpleNamespace(
                **{key: declaration[key] for key in ("kind", "manifest_sha256", "profile")},
                infer=lambda request, context, alias=alias: self._infer(alias, request, context),
            )
        for declaration in manifest["trackers"]:
            alias = declaration["alias"]
            self.trackers[alias] = SimpleNamespace(
                **{key: declaration[key] for key in ("implementation", "version")},
                track=lambda request, state, context, alias=alias: self._track(alias, state),
            )

    def _infer(self, alias, request, context):
        self.counts[alias] += 1
        if self.on_infer is not None:
            return deepcopy(self.on_infer(alias, request, context))
        if alias == "detector":
            return {"detections": [deepcopy(DETECTION)]}
        return {"classification": {"label": "red", "score": 0.5,
                                   "category_id": 1, "output_index": 0}}

    def _track(self, alias, state):
        self.counts[alias] += 1
        return {"tracks": [], "state": deepcopy(state)}


def make_probe_package(root, body, *, extra_methods="", import_code="", mutate_manifest=None):
    """Reusable plain helper; create a new, explicitly synthetic package."""
    root = Path(root)
    shutil.copytree(PILOT / "fixtures/scenario", root)
    manifest = json.loads((root / "scenario-package.json").read_text("utf-8"))
    manifest["version"] = "0.0.1"
    manifest["name"] = "synthetic-contract-probe"
    manifest["description"] = "Synthetic host contract test with fake providers; no ML inference."
    manifest["parameters"] = {}
    manifest["state"] = {
        "scope": "instance-source-run", "initial": {"n": 0},
        "schema": {"type": "object", "additionalProperties": False,
                   "required": ["n"], "properties": {"n": {"type": "integer", "minimum": 0}}},
    }
    manifest["event_contract"]["types"] = [{
        "event_type": EVENT_TYPE,
        "payload_schema": {"type": "object", "additionalProperties": False,
                           "required": ["n"], "properties": {"n": {"type": "integer"}}},
    }]
    source = (textwrap.dedent(import_code).strip() + "\n\nclass Scenario:\n"
              "    def __init__(self):\n        pass\n\n"
              "    def process(self, inputs, context, state, services):\n"
              + textwrap.indent(textwrap.dedent(body).strip(), "        ") + "\n")
    if extra_methods:
        source += "\n" + textwrap.indent(textwrap.dedent(extra_methods).strip(), "    ") + "\n"
    (root / "code/scenario.py").write_text(source, "utf-8")
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    # The original color-cascade process vectors describe a different state
    # and event contract.  A reset-only sequence is valid for every probe body
    # without inventing its process results.  These packages claim no golden
    # process coverage; the explicit pytest assertions below are their oracles.
    (root / "golden/basic.json").write_text(
        json.dumps({"steps": [{"reset": {}}]}, indent=2) + "\n", "utf-8")
    manifest["golden"]["sequences"] = [{"id": "synthetic-reset-only", "path": "golden/basic.json"}]
    manifest["files"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "scenario-package.json"
    }
    # Different probe contents never reuse a scenario_id/version pair.  This
    # covers parametrized tests and future edits as well as one pytest run.
    identity_content = {key: value for key, value in manifest.items() if key != "scenario_id"}
    content_hash = hashlib.sha256(json.dumps(identity_content, sort_keys=True,
                                           ensure_ascii=False).encode("utf-8")).hexdigest()
    manifest["scenario_id"] = "org.vlexchange.synthetic.contract-probe.p" + content_hash[:24]
    (root / "scenario-package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return root, manifest


@pytest.fixture
def probe_package(tmp_path):
    """Each test gets copies only; pinned fixtures are never modified."""
    serial = 0

    def make(body, **options):
        nonlocal serial
        serial += 1
        return make_probe_package(tmp_path / f"synthetic-probe-{serial}", body, **options)

    return make


def frame_call(sequence=1, *, source="camera-a", run="run-1"):
    inputs = {"frame": {"frame_id": f"f{sequence}", "frame_ref": f"opaque-{sequence}",
                        "width": 4, "height": 2}}
    context = {"source_id": source, "run_id": run, "sequence": sequence,
               "event_time_ms": 1700000000000 + sequence}
    return inputs, context


def _candidate(n):
    return {"event_type": EVENT_TYPE, "payload": {"n": n}}


def _detection_request_source():
    return """frame = inputs['frame']
request = {'frame_id': frame['frame_id'], 'frame_ref': frame['frame_ref'],
           'object_id': None, 'bbox': None}
"""


def _assert_atomic_fatal(engine, operation, *, code):
    before = deepcopy(engine.snapshot())
    committed = engine.lifecycle()["committed_calls"]
    with pytest.raises(Fault) as caught:
        operation()
    assert caught.value.code == code
    assert caught.value.fatal is True
    assert caught.value.retryable is False
    assert engine.snapshot() == before
    assert engine.lifecycle()["committed_calls"] == committed
    assert engine.lifecycle()["failed"] is True
    # Fatality belongs to the instance, including other source/run namespaces.
    with pytest.raises(Fault) as next_call:
        engine.process(*frame_call(3, source="other-camera", run="other-run"))
    assert next_call.value.code == "lifecycle"
    with pytest.raises(Fault) as reset:
        engine.reset()
    assert reset.value.code == "lifecycle"
    return caught.value


def test_caught_service_exception_cannot_commit_or_keep_instance_healthy(probe_package):
    # VLSP 0.2 section 4: catching a provider error cannot turn it into success.
    body = _detection_request_source() + f"""
if context['sequence'] > 1:
    try:
        services.infer('detector', request)
    except Exception:
        pass
return {{'events': [{{'event_type': {EVENT_TYPE!r}, 'payload': {{'n': state['n'] + 1}}}}],
        'state': {{'n': state['n'] + 1}}}}
"""
    root, manifest = probe_package(body)

    def failing_provider(alias, request, context):
        raise RuntimeError("synthetic provider failure; no ML executed")

    providers = FakeProviders(manifest, failing_provider)
    engine = Engine(root, providers)
    try:
        first = engine.process(*frame_call())
        assert first["events"][0]["payload"] == {"n": 1}
        _assert_atomic_fatal(engine, lambda: engine.process(*frame_call(2)), code="service_failed")
        assert providers.counts == {"detector": 1}
    finally:
        engine.close()


@pytest.mark.parametrize("rethrow", [False, True], ids=["swallow-mutated-fault", "rethrow-mutated-fault"])
def test_publisher_cannot_rewrite_caught_service_fault_policy(probe_package, rethrow):
    # Error objects are mutable Python objects; the normative failure is sticky.
    action = "raise failure" if rethrow else "pass"
    body = _detection_request_source() + f"""
if context['sequence'] > 1:
    try:
        services.infer('undeclared-alias', request)
    except Exception as failure:
        failure.fatal = False
        failure.retryable = True
        failure.code = 'input_contract'
        failure.stage = 'input'
        failure.message = 'publisher tried to replace the host failure'
        {action}
return {{'events': [], 'state': {{'n': state['n'] + 1}}}}
"""
    root, manifest = probe_package(body)
    providers = FakeProviders(manifest)
    engine = Engine(root, providers)
    try:
        engine.process(*frame_call())
        fault = _assert_atomic_fatal(engine, lambda: engine.process(*frame_call(2)),
                                     code="service_contract" if rethrow else "service_failed")
        assert fault.stage == "execution"
        assert not providers.counts
    finally:
        engine.close()


@pytest.mark.parametrize("mutation", ["inputs['frame']['width'] = 999", "context['source_id'] = 'forged'"])
def test_input_or_context_mutation_is_fatal_without_mutating_caller(probe_package, mutation):
    root, manifest = probe_package(f"""
if context['sequence'] > 1:
    {mutation}
return {{'events': [{{'event_type': {EVENT_TYPE!r}, 'payload': {{'n': 2}}}}],
        'state': {{'n': state['n'] + 1}}}}
""")
    engine = Engine(root, FakeProviders(manifest))
    try:
        engine.process(*frame_call())
        inputs, context = frame_call(2)
        original = deepcopy((inputs, context))
        _assert_atomic_fatal(engine, lambda: engine.process(inputs, context), code="input_mutation")
        assert (inputs, context) == original
    finally:
        engine.close()


def test_bad_candidate_state_and_unknown_event_are_atomic_fatal_errors(probe_package):
    # A valid event before the invalid candidate must not escape a failed call.
    for invalid_return, expected_code in (
        (f"{{'events': [{_candidate(2)!r}], 'state': {{'n': -1}}}}", "state_contract"),
        (f"{{'events': [{_candidate(2)!r}, {{'event_type': 'org.vlexchange.synthetic.unknown.v1', 'payload': {{}}}}], 'state': {{'n': 2}}}}", "event_contract"),
    ):
        root, manifest = probe_package(f"""
if context['sequence'] > 1:
    return {invalid_return}
return {{'events': [], 'state': {{'n': 1}}}}
""")
        engine = Engine(root, FakeProviders(manifest))
        try:
            engine.process(*frame_call())
            _assert_atomic_fatal(engine, lambda: engine.process(*frame_call(2)), code=expected_code)
        finally:
            engine.close()


def _published_event_identity(fields):
    # Independent transcription of the published length-prefixed wire algorithm.
    wire = bytearray(b"VLSP-EVENT-V1\x00")
    for field in fields:
        encoded = field.encode("utf-8", "strict")
        wire += len(encoded).to_bytes(4, "big") + encoded
    return bytes(wire), "sha256:" + hashlib.sha256(wire).hexdigest()


def test_multiple_event_ordinals_follow_published_vector_and_replay_identity(probe_package):
    vector = json.loads((PILOT.parent / "scenario/spec/event-id-test-vector.json").read_text("utf-8"))
    wire, identity = _published_event_identity(vector["fields"])
    assert wire.hex() == vector["hash_input_hex"]
    assert identity == vector["event_id"]
    root, manifest = probe_package(f"""
return {{'events': [{{'event_type': {EVENT_TYPE!r}, 'payload': {{'n': n}}}} for n in [2, 0, 1]],
        'state': {{'n': state['n'] + 1}}}}
""")
    engine = Engine(root, FakeProviders(manifest))
    try:
        inputs, context = frame_call(2, source="камера-🚧", run="сеанс-1")
        result = engine.process(inputs, context)
        assert [event["payload"]["n"] for event in result["events"]] == [2, 0, 1]
        expected_ids = []
        for ordinal, event in enumerate(result["events"]):
            fields = [manifest["scenario_id"], manifest["version"], context["source_id"],
                      context["run_id"], str(context["sequence"]), EVENT_TYPE, str(ordinal)]
            expected_ids.append(_published_event_identity(fields)[1])
            assert {key: event[key] for key in context} == context
        assert [event["event_id"] for event in result["events"]] == expected_ids
        assert len(set(expected_ids)) == 3
        engine.reset()
        assert engine.process(inputs, context) == result
        other_context = dict(context, run_id="сеанс-2")
        assert {event["event_id"] for event in engine.process(inputs, other_context)["events"]}.isdisjoint(expected_ids)
    finally:
        engine.close()


def test_retained_service_reference_rejects_calls_after_process_return(probe_package):
    root, manifest = probe_package(_detection_request_source() + """
self.retained_service = services
self.retained_request = request
return {'events': [], 'state': {'n': 1}}
""")
    providers = FakeProviders(manifest)
    engine = Engine(root, providers)
    try:
        engine.process(*frame_call())
        before = deepcopy(engine.snapshot())
        # Inspect only fields owned by this synthetic Scenario, to exercise its
        # escaped capability without depending on service implementation details.
        scenario = engine._scenario
        with pytest.raises(Fault) as caught:
            scenario.retained_service.infer("detector", scenario.retained_request)
        assert caught.value.fatal is True
        assert caught.value.retryable is False
        assert not providers.counts
        assert engine.snapshot() == before
    finally:
        engine.close()


def test_identical_repeated_detector_raw_id_is_allowed(probe_package):
    root, manifest = probe_package(_detection_request_source() + """
first = services.infer('detector', request)
second = services.infer('detector', request)
assert first == second
assert len(first['detections']) == 1
return {'events': [], 'state': {'n': 2}}
""")
    providers = FakeProviders(manifest)
    engine = Engine(root, providers)
    try:
        assert engine.process(*frame_call()) == {"events": [], "state": {"n": 2}}
        assert providers.counts == {"detector": 2}
        assert engine.lifecycle()["failed"] is False
    finally:
        engine.close()


def test_conflicting_repeated_detector_raw_id_is_fatal(probe_package):
    root, manifest = probe_package(_detection_request_source() + """
if context['sequence'] > 1:
    services.infer('detector', request)
    services.infer('detector', request)
return {'events': [], 'state': {'n': state['n'] + 1}}
""")
    calls = 0

    def conflicting_provider(alias, request, context):
        nonlocal calls
        calls += 1
        return {"detections": [dict(DETECTION, score=0.8 if calls == 1 else 0.7)]}

    providers = FakeProviders(manifest, conflicting_provider)
    engine = Engine(root, providers)
    try:
        engine.process(*frame_call())
        _assert_atomic_fatal(engine, lambda: engine.process(*frame_call(2)), code="service_contract")
        assert providers.counts == {"detector": 2}
    finally:
        engine.close()


def test_required_missing_or_incompatible_binding_is_rejected_before_import(probe_package, tmp_path):
    for variation in ("missing-model", "wrong-model-digest", "wrong-tracker-version"):
        marker = tmp_path / f"{variation}.imported"
        root, manifest = probe_package("return {'events': [], 'state': state}", import_code=f"""
from pathlib import Path
Path({str(marker)!r}).write_text('synthetic import ran', encoding='utf-8')
""")
        providers = FakeProviders(manifest)
        if variation == "missing-model":
            del providers.models["classifier_b"]
        elif variation == "wrong-model-digest":
            providers.models["classifier_b"].manifest_sha256 = "0" * 64
        else:
            providers.trackers["tracker"].version = "999.0.0"
        with pytest.raises(Fault) as caught:
            Engine(root, providers)
        assert caught.value.code == {"missing-model": "dependency_missing", "wrong-model-digest": "model_link_mismatch", "wrong-tracker-version": "dependency_mismatch"}[variation]
        assert caught.value.fatal is False and caught.value.retryable is False
        assert not marker.exists(), variation
        assert not providers.counts


def test_unknown_required_capability_or_extension_is_rejected_before_import(probe_package, tmp_path):
    for variation in ("capability", "extension"):
        marker = tmp_path / f"{variation}.imported"

        def mutate(manifest):
            if variation == "capability":
                manifest["required_capabilities"].append("synthetic-unsupported-capability-v1")
            else:
                manifest["extensions"]["org.vlexchange.synthetic.unsupported"] = {
                    "required": True, "data": {}}

        root, manifest = probe_package("return {'events': [], 'state': state}",
                                       import_code=f"from pathlib import Path\nPath({str(marker)!r}).touch()",
                                       mutate_manifest=mutate)
        with pytest.raises(Fault) as caught:
            Engine(root, FakeProviders(manifest))
        assert caught.value.code == "capability_missing", variation
        assert not marker.exists(), variation


def test_expired_original_lease_allows_new_authorized_retry_without_lost_state(probe_package):
    """The original lease expires during inference; only a fresh call may recover."""
    body = _detection_request_source() + """
services.infer('detector', request)
return {'events': [], 'state': {'n': state['n'] + 1}}
"""
    root, manifest = probe_package(body)
    current = {"expire_active": False, "expire_once": True, "lease_count": 0}

    class Lease:
        def __init__(self):
            self.expired = False

        def assert_current(self):
            if self.expired:
                raise ValueError("synthetic original lease expired")

    class Authority:
        claims = {"manifest_sha256": hashlib.sha256((root / "scenario-package.json").read_bytes()).hexdigest(),
                  "package_format": "vl.scenario-package", "manifest_path": "scenario-package.json"}
        package_sha256 = "0" * 64

        def assert_current(self):
            return None

        def before_process(self):
            current["lease_count"] += 1
            current["lease"] = Lease()
            return current["lease"]

    def detector(alias, request, context):
        if context["sequence"] == 2 and current["expire_once"]:
            current["lease"].expired = True
            current["expire_once"] = False
        return {"detections": [deepcopy(DETECTION)]}

    engine = Engine(root, FakeProviders(manifest, detector), authorization=Authority())
    try:
        assert engine.process(*frame_call())["state"] == {"n": 1}
        before = engine.snapshot()
        with pytest.raises(Fault) as failed:
            engine.process(*frame_call(2))
        assert failed.value.to_dict() | {"message": ""} == {
            "code": "authorization_failed", "stage": "security", "fatal": False,
            "retryable": True, "message": ""}
        assert engine.snapshot() == before
        assert engine.lifecycle()["failed"] is False
        assert engine.lifecycle()["committed_calls"] == 1
        assert engine.process(*frame_call(2))["state"] == {"n": 2}
        assert current["lease_count"] == 3
        assert engine.lifecycle()["committed_calls"] == 2
    finally:
        engine.close()
