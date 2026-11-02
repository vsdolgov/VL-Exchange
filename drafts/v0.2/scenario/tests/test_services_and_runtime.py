"""Host service contract tests use labelled in-memory fakes, not model inference."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile
from functools import partial
import time

import pytest

from vl_scenario_v02 import Host, ModelBinding, TrackerBinding, ScenarioError, ScenarioExecutor, object_id, validate_package
from vl_scenario_v02.services import CallServices

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT.parents[2] / "scenario"
FRAME = {"frame_id": "frame-1", "frame_ref": "opaque-1", "width": 4, "height": 2}
CONTEXT = {"source_id": "cam", "run_id": "run", "sequence": 1, "event_time_ms": 1700000000000}
CAPS = ["python-scenario-services-json-v1", "frame-ref-v1", "model-inference-v1", "pure-tracker-v1", "event-json-v1", "source-run-state-v1", "event-id-sha256-v1"]

SCENARIO = '''class Scenario:
    def __init__(self, **params): pass
    def process(self, inputs, context, state, services):
        frame=inputs["frame"]
        req={"frame_id":frame["frame_id"],"frame_ref":frame["frame_ref"],"object_id":None,"bbox":None}
        a=services.infer("d1",req)["detections"]
        b=services.infer("d2",req)["detections"]
        output=services.track("t1",{"frame_id":frame["frame_id"],"detections":a+b},state["tracker"])
        labels=[]
        for obj in output["tracks"]:
            req={"frame_id":frame["frame_id"],"frame_ref":frame["frame_ref"],"object_id":obj["object_id"],"bbox":obj["bbox"]}
            labels.append(services.infer("c1",req)["classification"]["label"])
            labels.append(services.infer("c2",req)["classification"]["label"])
        state["tracker"]=output["state"]
        state["runs"]+=1
        return {"events":[{"event_type":"org.vlexchange.test.v1","payload":{"labels":labels,"tracks":output["tracks"]}}],"state":state}
'''


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "package"
    (root / "code").mkdir(parents=True)
    (root / "golden").mkdir()
    (root / "code/__init__.py").write_text("")
    (root / "code/scenario.py").write_text(SCENARIO)
    (root / "golden/basic.json").write_text(json.dumps({"steps": [{"inputs":{"frame":FRAME},"context":CONTEXT,"expected":{"events":[],"state":{}}}]}))
    manifest = json.loads((OLD / "spec/example-manifest.json").read_text())
    manifest["spec_version"] = "0.2.0"
    manifest["entrypoint"]["contract"] = "python-scenario-services-json-v1"
    manifest["input_contract"] = {"name":"frame","message_type":"vl.frame-ref.v1","clock_domain":"unix-utc-ms"}
    manifest["models"] = [{"alias": alias, "kind": kind, "manifest_sha256": str(i)*64, "profile":"p1", "required":True}
                          for i,(alias,kind) in enumerate([("d1","detection"),("d2","detection"),("c1","classification"),("c2","classification")],start=1)]
    del manifest["model_links"]
    manifest["trackers"] = [{"alias":"t1","implementation":"pure-fake","version":"1","required":True}]
    manifest["required_capabilities"] = CAPS
    manifest["limits"].update({"max_model_calls":10,"max_tracker_calls":2,"max_process_ms":5000})
    manifest["event_contract"]["types"] = [{"event_type":"org.vlexchange.test.v1","payload_schema":{"type":"object"}}]
    manifest["parameters"] = {}
    manifest["state"] = {"scope":"instance-source-run","initial":{"tracker":{},"runs":0},
                         "schema":{"type":"object","additionalProperties":False,"required":["tracker","runs"],
                                   "properties":{"tracker":{"type":"object"},"runs":{"type":"integer","minimum":0}}}}
    manifest["files"] = {name:_sha(root/name) for name in ("code/__init__.py","code/scenario.py","golden/basic.json")}
    _write(root,manifest)
    return root, manifest


def _write(root, manifest):
    (root / "scenario-package.json").write_text(json.dumps(manifest, ensure_ascii=False),encoding="utf-8")


def _set_scenario(root, manifest, code):
    (root / "code/scenario.py").write_text(code)
    manifest["files"]["code/scenario.py"] = _sha(root / "code/scenario.py")
    _write(root,manifest)


def _fake_detection(req, ctx):
    assert req["frame_ref"] == "opaque-1" and ctx["source_id"] == "cam"
    return {"detections":[{"object_id":"0","label":"person","category_id":1,"score":0.9,"bbox":[0,0,1,1]}]}


def _classification(label):
    def callback(req,ctx):
        return {"classification":{"label":label,"score":0.9,"category_id":1,"output_index":0}}
    return callback


def _tracker(req,state,ctx):
    return {"tracks":[{"object_id":row["object_id"],"track_id":"t"+str(i),"bbox":row["bbox"]} for i,row in enumerate(req["detections"])],
            "state":{"calls":state.get("calls",0)+1}}


@pytest.fixture
def host(package):
    _, manifest = package
    callbacks = {"d1":_fake_detection,"d2":_fake_detection,"c1":_classification("vest"),"c2":_classification("helmet")}
    models = {m["alias"]:ModelBinding(m["kind"],m["manifest_sha256"],m["profile"],callbacks[m["alias"]]) for m in manifest["models"]}
    return Host(models,{"t1":TrackerBinding("pure-fake","1",_tracker)})


def test_object_identity_vectors_and_collisions():
    data = json.loads((ROOT / "spec/object-id-test-vectors.json").read_text())
    for case in data["cases"]:
        assert object_id(case["model_alias"],case["raw_object_id"]) == case["object_id"]
    assert object_id("d1","0") != object_id("d2","0")


def test_full_frame_correlations_and_rollback_on_duplicate_frame(package,host):
    root,manifest = package
    with ScenarioExecutor(root,host=host,policy="development") as executor:
        a = executor.process({"frame":FRAME},CONTEXT)
        payload = a["events"][0]["payload"]
        assert len(payload["tracks"]) == 2
        assert payload["tracks"][0]["object_id"] != payload["tracks"][1]["object_id"]
        assert payload["labels"] == ["vest","helmet","vest","helmet"]
        assert a["state"] == {"tracker":{"calls":1},"runs":1}
        before = executor.snapshot()
        with pytest.raises(ScenarioError, match="frame_id"):
            executor.process({"frame":FRAME},{**CONTEXT,"sequence":2})
        assert executor.snapshot() == before
        executor.reset("cam","run")
        assert executor.process({"frame":FRAME},CONTEXT)["state"]["runs"] == 1


def test_services_reject_bad_provenance_aliases_and_crop(package,host):
    _,manifest = package
    services = CallServices(host,manifest,FRAME,CONTEXT)
    req = {"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    detection = services.infer("d1",req)["detections"][0]
    obj = detection["object_id"]
    ok = {"frame_id":"frame-1","frame_ref":"opaque-1","object_id":obj,"bbox":[0,0,0.5,0.5]}
    assert services.infer("c1",ok)["classification"]["label"] == "vest"
    for alias,request in (("missing",req),("c1",{**ok,"frame_ref":"other"}), ("c1",{**ok,"object_id":"forged"}),
                          ("c1",{**ok,"bbox":[-0.1,0,1,1]})):
        with pytest.raises(ScenarioError):
            services.infer(alias,request)


def test_tracker_must_not_invent_or_duplicate_track_and_objects(package,host):
    _,manifest = package
    req = {"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    svc = CallServices(host,manifest,FRAME,CONTEXT)
    detection = svc.infer("d1",req)["detections"]
    def bad(request,state,context):
        row={"object_id":request["detections"][0]["object_id"],"track_id":"same","bbox":[0,0,1,1]}
        return {"tracks":[row,row],"state":{}}
    broken = Host(host.models,{"t1":TrackerBinding("pure-fake","1",bad)})
    svc = CallServices(broken,manifest,FRAME,CONTEXT)
    detection = svc.infer("d1",req)["detections"]
    with pytest.raises(ScenarioError):
        svc.track("t1",{"frame_id":"frame-1","detections":detection},{})


def test_tracker_rejects_tampered_detection_metadata(package,host):
    _,manifest = package
    svc=CallServices(host,manifest,FRAME,CONTEXT)
    req={"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    detection=svc.infer("d1",req)["detections"]
    altered=[{**detection[0],"label":"car","category_id":42,"score":0.1}]
    with pytest.raises(ScenarioError):
        svc.track("t1",{"frame_id":"frame-1","detections":altered},{})


def test_multiple_trackers_retain_each_independent_bbox(package,host):
    _,manifest = package
    manifest["trackers"].append({"alias":"t2","implementation":"pure-fake","version":"2","required":True})
    def small_detection(req,ctx):
        return {"detections":[{"object_id":"0","label":"person","category_id":1,"score":0.9,"bbox":[0.25,0.25,0.75,0.75]}]}
    def bound(box):
        return lambda req,state,ctx:{"tracks":[{"object_id":req["detections"][0]["object_id"],"track_id":"x","bbox":box}],"state":{}}
    bindings={**host.models,"d1":ModelBinding("detection","1"*64,"p1",small_detection)}
    trackers={"t1":TrackerBinding("pure-fake","1",bound([0,0,0.5,0.5])),"t2":TrackerBinding("pure-fake","2",bound([0.5,0.5,1,1]))}
    svc=CallServices(Host(bindings,trackers),manifest,FRAME,CONTEXT)
    req={"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    detections=svc.infer("d1",req)["detections"]
    first=svc.track("t1",{"frame_id":"frame-1","detections":detections},{})["tracks"][0]
    second=svc.track("t2",{"frame_id":"frame-1","detections":detections},{})["tracks"][0]
    for track in (first,second):
        assert svc.infer("c1",{**req,"object_id":track["object_id"],"bbox":track["bbox"]})["classification"]["label"] == "vest"


def test_repeated_detector_id_requires_identical_full_result(package,host):
    _,manifest = package
    state={"counter":0}
    def callback(req,ctx):
        state["counter"]+=1
        return {"detections":[{"object_id":"0","label":"person","category_id":1,"score":0.9 if state["counter"] < 3 else 0.4,"bbox":[0,0,1,1]}]}
    svc=CallServices(Host({**host.models,"d1":ModelBinding("detection","1"*64,"p1",callback)},host.trackers),manifest,FRAME,CONTEXT)
    req={"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    assert svc.infer("d1",req)==svc.infer("d1",req)
    with pytest.raises(ScenarioError,match="conflicting"):
        svc.infer("d1",req)


def test_model_and_tracker_call_budgets_are_enforced(package,host):
    _,manifest=package
    manifest["limits"]["max_model_calls"]=1
    manifest["limits"]["max_tracker_calls"]=1
    svc=CallServices(host,manifest,FRAME,CONTEXT)
    request={"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None}
    rows=svc.infer("d1",request)["detections"]
    with pytest.raises(ScenarioError,match="max_model_calls"):
        svc.infer("d1",request)
    track_req={"frame_id":"frame-1","detections":rows}
    svc.track("t1",track_req,{})
    with pytest.raises(ScenarioError,match="max_tracker_calls"):
        svc.track("t1",track_req,{})
    svc.close()
    with pytest.raises(ScenarioError,match="current process"):
        svc.infer("d1",request)


def test_callback_receives_detached_request_and_state(package,host):
    _,manifest=package
    def mutating_tracker(req,state,ctx):
        obj=req["detections"][0]["object_id"]
        req["detections"][0]["label"]="mutated"
        state["hidden"]=True
        ctx["source_id"]="changed"
        return {"tracks":[{"object_id":obj,"track_id":"1","bbox":[0,0,1,1]}],"state":{"count":1}}
    svc=CallServices(Host(host.models,{"t1":TrackerBinding("pure-fake","1",mutating_tracker)}),manifest,FRAME,CONTEXT)
    rows=svc.infer("d1",{"frame_id":"frame-1","frame_ref":"opaque-1","object_id":None,"bbox":None})["detections"]
    state={}
    result=svc.track("t1",{"frame_id":"frame-1","detections":rows},state)
    assert rows[0]["label"] == "person" and state == {} and CONTEXT["source_id"] == "cam"
    assert result["state"] == {"count":1}


def test_golden_fixture_schema_is_checked_before_scenario_import(package,host):
    root,manifest=package
    (root / "golden/basic.json").write_text('{"steps":[{"inputs":{"frame":{}},"context":{},"expected":{}}]}')
    manifest["files"]["golden/basic.json"]=_sha(root / "golden/basic.json")
    _write(root,manifest)
    with pytest.raises(ScenarioError) as exc:
        validate_package(root,host=host)
    assert exc.value.code == "manifest_invalid"


def test_static_model_identity_report_never_claims_file_verification(package,host):
    root,_=package
    static=validate_package(root,host=None).to_dict()
    bound=validate_package(root,host=host).to_dict()
    assert len(static["model_bindings"]) == 4
    assert all(x["status"] == "declared_unverified" for x in static["model_bindings"])
    assert all(x["status"] == "host_assertion_verified" for x in bound["model_bindings"])
    assert static["model_package_bytes_verified"] is bound["model_package_bytes_verified"] is False


def test_model_registry_is_fixed_when_executor_is_created(package,host):
    root,_ = package
    with ScenarioExecutor(root,host=host,policy="development") as executor:
        host.models["d1"] = ModelBinding("detection","wrong","wrong",lambda req,ctx: {})
        assert executor.process({"frame":FRAME},CONTEXT)["state"]["runs"] == 1


def test_swallowed_service_failure_is_fatal_and_cannot_commit(package,host):
    root,manifest=package
    code='''class Scenario:
    def __init__(self, **params): pass
    def process(self, inputs, context, state, services):
        try:
            services.infer("missing",{})
        except Exception:
            pass
        return {"events":[],"state":state}
'''
    _set_scenario(root,manifest,code)
    with ScenarioExecutor(root,host=host,policy="development") as executor:
        with pytest.raises(ScenarioError) as exc:
            executor.process({"frame":FRAME},CONTEXT)
        assert exc.value.code=="service_failed"
        assert executor.snapshot()=={"state":{},"order":{}}


def test_opaque_frame_ref_is_not_exported_in_events_or_state(package,host):
    root,manifest=package
    code='''class Scenario:
    def __init__(self, **params): pass
    def process(self, inputs, context, state, services):
        return {"events":[{"event_type":"org.vlexchange.test.v1","payload":{"leak":inputs["frame"]["frame_ref"]}}],"state":state}
'''
    _set_scenario(root,manifest,code)
    with ScenarioExecutor(root,host=host,policy="development") as executor:
        with pytest.raises(ScenarioError,match="frame_ref"):
            executor.process({"frame":FRAME},CONTEXT)
        assert executor.snapshot()=={"state":{},"order":{}}


def test_manifest_integrity_and_model_binding_rejected_before_import(package,host):
    root,manifest = package
    wrong = Host({**host.models,"d1":ModelBinding("detection","0"*64,"p1",_fake_detection)},host.trackers)
    with pytest.raises(ScenarioError) as e:
        ScenarioExecutor(root,host=wrong,policy="development")
    assert e.value.code == "model_link_mismatch"
    (root / "code/scenario.py").write_text("raise RuntimeError('MUST NOT IMPORT')")
    with pytest.raises(ScenarioError) as e:
        validate_package(root,host=host)
    assert e.value.code == "digest_mismatch"


def test_scenario_cannot_require_host_executor_or_block_runtime(package,host):
    root,manifest=package
    _set_scenario(root,manifest,"import vl_scenario_v02\n" + SCENARIO)
    with pytest.raises(ScenarioError) as exc:
        validate_package(root,host=host)
    assert exc.value.code == "manifest_invalid"


def test_helper_cannot_require_vendor_block_runtime(package,host):
    root,manifest=package
    helper=root/"code/helper.py"
    helper.write_text("import vl_block_package\n")
    manifest["files"]["code/helper.py"]=_sha(helper)
    _write(root,manifest)
    with pytest.raises(ScenarioError) as exc:
        validate_package(root,host=host)
    assert exc.value.code == "manifest_invalid"


def test_missing_golden_path_is_rejected_before_import(package,host):
    root,manifest = package
    manifest["golden"]["sequences"][0]["path"]="golden/missing.json"
    _write(root,manifest)
    with pytest.raises(ScenarioError) as e:
        validate_package(root,host=host)
    assert e.value.code == "inventory_mismatch"


def test_development_must_be_explicit_and_failed_service_cannot_commit(package,host):
    root,manifest = package
    with pytest.raises(ScenarioError):
        ScenarioExecutor(root,host=host)
    bad = Host({**host.models,"d1":ModelBinding("detection","1"*64,"p1",lambda req,ctx: (_ for _ in ()).throw(ValueError("boom")))},host.trackers)
    with ScenarioExecutor(root,host=bad,policy="development") as executor:
        with pytest.raises(ScenarioError):
            executor.process({"frame":FRAME},CONTEXT)
        assert executor.snapshot() == {"state":{},"order":{}}


def test_offline_protected_v02_uses_shared_crypto(package,host,tmp_path):
    """Real keys and encryption; the host models remain explicit fakes."""
    from nacl.public import PrivateKey
    from nacl.signing import SigningKey
    from vl_exchange_security import authorized_package, seal_package
    root,_ = package
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive,"w") as zf:
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path,path.relative_to(root).as_posix())
    issuer, recipient, installation = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate()
    now = int(time.time())
    envelope = tmp_path / "protected"
    seal_package(archive,envelope,issuer_signing_key=issuer,recipient_public_key=recipient.public_key,
                 installation_verify_key=installation.verify_key,issuer_id="test",license_id="test",
                 installation_id="test",mode="offline",issued_at=now,not_before=now,expires_at=now+3600,
                 manifest_path="scenario-package.json")
    verifier=partial(authorized_package,issuer_verify_key=issuer.verify_key,recipient_private_key=recipient,policy="offline")
    with ScenarioExecutor.open_protected(envelope,host=host,verifier=verifier) as executor:
        assert executor.process({"frame":FRAME},CONTEXT)["state"]["runs"] == 1
        assert executor.report()["delivery"]["protected"] is True


def test_expiry_during_model_inference_discards_events_and_state(package,host,tmp_path):
    from nacl.public import PrivateKey
    from nacl.signing import SigningKey
    from vl_exchange_security import authorized_package, seal_package
    root,_=package
    archive=tmp_path/"p.zip"
    with zipfile.ZipFile(archive,"w") as zf:
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path,path.relative_to(root).as_posix())
    issuer,recipient,installation=SigningKey.generate(),PrivateKey.generate(),SigningKey.generate()
    clock={"now":1000}
    envelope=tmp_path/"protected"
    seal_package(archive,envelope,issuer_signing_key=issuer,recipient_public_key=recipient.public_key,
                 installation_verify_key=installation.verify_key,issuer_id="test",license_id="test",
                 installation_id="test",mode="offline",issued_at=1000,not_before=1000,expires_at=1010,
                 manifest_path="scenario-package.json")
    verifier=partial(authorized_package,issuer_verify_key=issuer.verify_key,recipient_private_key=recipient,
                     policy="offline",now=lambda:clock["now"])
    def advance(req,ctx):
        clock["now"]=1010
        return _fake_detection(req,ctx)
    bindings={**host.models,"d1":ModelBinding("detection","1"*64,"p1",advance)}
    with ScenarioExecutor.open_protected(envelope,host=Host(bindings,host.trackers),verifier=verifier) as executor:
        with pytest.raises(ScenarioError) as exc:
            executor.process({"frame":FRAME},CONTEXT)
        assert exc.value.code=="authorization_failed"
        assert executor.snapshot()=={"state":{},"order":{}}


@pytest.mark.parametrize("rethrow", [False, True], ids=["swallow-edited-service-error", "rethrow-edited-service-error"])
def test_publisher_cannot_rewrite_service_failure_metadata(package, host, rethrow):
    """Synthetic contract probe: no model inference, original host error wins."""
    root, manifest = package
    action = "raise failure" if rethrow else "pass"
    code = f'''class Scenario:
    def __init__(self, **parameters): pass
    def process(self, inputs, context, state, services):
        if context["sequence"] > 1:
            frame = inputs["frame"]
            try:
                services.infer("undeclared-alias", {{"frame_id":frame["frame_id"],
                    "frame_ref":frame["frame_ref"], "object_id":None, "bbox":None}})
            except Exception as failure:
                failure.fatal = False
                failure.retryable = True
                failure.code = "input_contract"
                failure.stage = "input"
                failure.message = "publisher rewrote a host-owned failure"
                {action}
        return {{"events":[], "state":{{"tracker":state["tracker"], "runs":state["runs"]+1}}}}
'''
    _set_scenario(root, manifest, code)
    with ScenarioExecutor(root, policy="development", host=host) as executor:
        executor.process({"frame": FRAME}, CONTEXT)
        before = executor.snapshot()
        inputs = {"frame": {**FRAME, "frame_id": "frame-2", "frame_ref": "opaque-2"}}
        context = {**CONTEXT, "sequence": 2}
        with pytest.raises(ScenarioError) as caught:
            executor.process(inputs, context)
        error = caught.value
        assert error.fatal is True and error.retryable is False
        assert error.code == ("service_contract" if rethrow else "service_failed")
        assert error.stage == "execution"
        assert "publisher rewrote" not in error.message
        assert executor.snapshot() == before
        assert executor.report()["lifecycle"]["failed"] is True
        assert executor.report()["lifecycle"]["committed_calls"] == 1
        with pytest.raises(ScenarioError) as retry:
            executor.process(inputs, context)
        assert retry.value.code == "lifecycle"
        with pytest.raises(ScenarioError) as reset:
            executor.reset()
        assert reset.value.code == "lifecycle"


def test_failed_construction_has_no_fatal_instance_and_service_errors_have_stable_stages(package, host):
    """Opening failure does not mark a nonexistent instance as failed."""
    root, manifest = package
    _set_scenario(root, manifest, '''class Scenario:
    def __init__(self, **parameters): raise RuntimeError("init failed")
    def process(self, inputs, context, state, services): pass
''')
    with pytest.raises(ScenarioError) as caught:
        ScenarioExecutor(root, policy="development", host=host)
    assert caught.value.to_dict() == {"stage": "execution", "code": "scenario_exception",
        "message": "Scenario initialization failed: RuntimeError: init failed", "fatal": False, "retryable": False}
    for code in ("service_failed", "service_contract"):
        assert ScenarioError(code, "fixture", fatal=True).stage == "execution"


def test_closed_and_failed_instances_report_distinct_lifecycle_policy(package, host):
    root, _ = package
    executor = ScenarioExecutor(root, policy="development", host=host)
    executor.close()
    with pytest.raises(ScenarioError) as caught:
        executor.process({"frame": FRAME}, CONTEXT)
    assert (caught.value.stage, caught.value.code, caught.value.fatal, caught.value.retryable) == (
        "lifecycle", "lifecycle", False, False)
