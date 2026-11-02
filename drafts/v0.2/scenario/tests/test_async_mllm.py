"""Local simulated MLLM: durable outbox, not an actual language model."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator

from vl_scenario_async import AsyncStore, QueueError


class Clock:
    def __init__(self, now=1000000): self.now = now
    def __call__(self): return self.now


@pytest.fixture
def setup(tmp_path):
    namespace = {"tenant_id":"tenant-a","installation_id":"box-a","scenario_id":"org.example.scenario",
                 "scenario_version":"1.0.0","scenario_manifest_sha256":"1"*64}
    clock = Clock()
    denied = set()
    def authorize(phase, job): return phase not in denied
    def validate_result(result, job):
        return set(result) == {"text","structured"} and type(result["text"]) is str
    db = tmp_path / "outbox.sqlite"
    store = AsyncStore(db, namespace=namespace, authorize=authorize, validate_result=validate_result, clock=clock)
    image = b"synthetic frame, not a real image"
    def job(frame="f1", source="cam-a", run="run-a", track="track-a", prompt="question", deadline=1100, expires=1090):
        return {"frame_id":frame,"track_id":track,"tracker_alias":"tracker" if track is not None else None,
                "source_event_time_ms":999000,"model_alias":"mllm-a","model_revision":"v1","model_digest":"2"*64,
                "payload":{"text":prompt,"response_schema":None,"parameters":{}},"deadline_ms":deadline*1000,
                "media":{"handle":"fixture-"+frame,"sha256":hashlib.sha256(image).hexdigest(),"expires_at_ms":expires*1000,
                         "source_id":source,"run_id":run,"frame_id":frame}}
    return store, namespace, clock, denied, image, job, db, authorize, validate_result


def test_delayed_provider_does_not_block_new_frame_and_restart_exactly_once(setup):
    store, ns, clock, denied, image, make, db, auth, validate = setup
    first = store.enqueue_and_commit("cam-a","run-a",0,{"seen":["f1"]},make())
    assert store.enqueue_and_commit("cam-a","run-a",0,{"seen":["f1"]},make())["duplicate"]
    started, finish = threading.Event(), threading.Event()
    def provider(job, image_bytes):
        assert image_bytes == image
        started.set()
        assert finish.wait(5)
        return {"text":"yes","structured":None}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.dispatch_one, provider, lambda media, identity: image)
        assert started.wait(5)
        second = store.enqueue_and_commit("cam-a","run-a",1,{"seen":["f1","f2"]},make(frame="f2"))
        assert store.status(second["job_id"]) == "pending"
        finish.set()
        assert future.result(timeout=5)["status"] == "completed"
    reopened = AsyncStore(db, namespace=ns, authorize=auth, validate_result=validate, clock=clock)
    invocations = []
    def transition(state,result,identity):
        invocations.append(identity["frame_id"])
        return {**state,"answers":{identity["frame_id"]:result["text"]}}
    assert reopened.apply_result(first["job_id"],2,transition)=={"duplicate":False,"revision":3}
    assert reopened.apply_result(first["job_id"],2,transition)["duplicate"]
    assert invocations == ["f1"]
    assert reopened.state("cam-a","run-a") == (3,{"seen":["f1","f2"],"answers":{"f1":"yes"}})


def test_recovery_rejects_old_attempt_and_foreign_correlation(setup):
    store, _, clock, _, _, make, _, _, _ = setup
    job_id = store.enqueue_and_commit("cam-a","run-a",0,{},make())["job_id"]
    one = store.claim(lease_seconds=5); clock.now += 6000
    assert store.recover()["requeued"] == 1
    two = store.claim(lease_seconds=5)
    with pytest.raises(QueueError, match="match job") as stale:
        store.complete(job_id,one["attempt_id"],one["job"]["identity"],{"text":"stale","structured":None})
    assert stale.value.code == "correlation_mismatch"
    forged = {**two["job"]["identity"],"source_id":"other-camera"}
    with pytest.raises(QueueError): store.complete(job_id,two["attempt_id"],forged,{"text":"bad","structured":None})
    assert store.complete(job_id,two["attempt_id"],two["job"]["identity"],{"text":"ok","structured":None})["status"] == "completed"
    assert store.complete(job_id,two["attempt_id"],two["job"]["identity"],{"text":"ok","structured":None})["duplicate"]
    with pytest.raises(QueueError) as conflict:
        store.complete(job_id,two["attempt_id"],two["job"]["identity"],{"text":"changed","structured":None})
    assert conflict.value.code == "duplicate_conflict"


def test_timeout_late_response_and_cancel_cannot_commit(setup):
    store, _, clock, _, _, make, _, _, _ = setup
    one = store.enqueue_and_commit("cam-a","run-a",0,{"count":1},make(deadline=1010,expires=1015))["job_id"]
    claim = store.claim(lease_seconds=20); clock.now = 1010000
    assert store.recover()["timed_out"] == 1
    with pytest.raises(QueueError): store.complete(one,claim["attempt_id"],claim["job"]["identity"],{"text":"late","structured":None})
    with pytest.raises(QueueError): store.apply_result(one,1,lambda s,r,i:{"count":999})
    assert store.state("cam-a","run-a") == (1,{"count":1})
    two = store.enqueue_and_commit("cam-a","run-a",1,{"count":2},make(frame="f2",deadline=1050,expires=1045))["job_id"]
    started = store.claim(); assert store.cancel(two)
    with pytest.raises(QueueError): store.complete(two,started["attempt_id"],started["job"]["identity"],{"text":"cancelled","structured":None})
    assert store.status(two) == "cancelled"


def test_wrong_media_never_reaches_provider_and_expired_pending_times_out(setup):
    store, _, clock, _, _, make, _, _, _ = setup
    one = store.enqueue_and_commit("cam-a","run-a",0,{},make())["job_id"]
    invoked=[]
    with pytest.raises(QueueError) as fail:
        store.dispatch_one(lambda job, image: invoked.append(True), lambda media,identity:b"wrong")
    assert fail.value.code == "media_mismatch" and not invoked and store.status(one)=="failed"
    two = store.enqueue_and_commit("cam-a","run-a",1,{},make(frame="f2",expires=1003))["job_id"]
    clock.now = 1003000
    assert store.recover()["timed_out"] == 1 and store.status(two)=="timed_out"


def test_authorization_result_validation_and_transaction_rollback(setup):
    store, _, _, denied, _, make, _, _, _ = setup
    denied.add("enqueue_commit")
    with pytest.raises(QueueError) as rejected: store.enqueue_and_commit("cam-a","run-a",0,{"v":1},make())
    assert rejected.value.code == "unauthorized" and store.state("cam-a","run-a") == (0,{})
    denied.clear(); job_id=store.enqueue_and_commit("cam-a","run-a",0,{"v":1},make())["job_id"]
    claim=store.claim()
    with pytest.raises(QueueError) as invalid:
        store.complete(job_id,claim["attempt_id"],claim["job"]["identity"],{"text":None,"structured":{"unexpected":"x"}})
    assert invalid.value.code == "invalid_result" and store.status(job_id)=="running"
    store.complete(job_id,claim["attempt_id"],claim["job"]["identity"],{"text":"ok","structured":None})
    denied.add("apply_commit")
    with pytest.raises(QueueError) as blocked:
        store.apply_result(job_id,1,lambda s,r,i:{"v":2})
    assert blocked.value.code == "unauthorized" and store.state("cam-a","run-a")== (1,{"v":1}) and store.status(job_id)=="completed"


def test_namespace_and_image_binding_are_enforced(setup,tmp_path):
    store, ns, clock, _, image, make, db, auth, validate = setup
    with pytest.raises(QueueError) as mismatch:
        AsyncStore(db,namespace={**ns,"tenant_id":"other"},authorize=auth,validate_result=validate,clock=clock)
    assert mismatch.value.code == "namespace_mismatch"
    wrong = make(); wrong["media"]["source_id"]="other-camera"
    with pytest.raises(QueueError) as bad: store.enqueue_and_commit("cam-a","run-a",0,{},wrong)
    assert bad.value.code == "correlation_mismatch" and store.state("cam-a","run-a")== (0,{})
    whole_frame=make(track=None); result=store.enqueue_and_commit("cam-a","run-a",0,{},whole_frame)
    assert store.claim()["job"]["identity"]["tracker_alias"] is None and result["revision"]==1


def test_concurrent_state_updates_only_one_wins(setup):
    store, _, _, _, _, make, _, _, _ = setup
    def enqueue(frame):
        try: return store.enqueue_and_commit("cam-a","run-a",0,{"frame":frame},make(frame=frame))
        except QueueError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(enqueue,["f1","f2"]))
    assert sum(isinstance(x,dict) for x in results)==1
    assert results.count("revision_conflict")==1


def test_expiry_during_authorization_or_validation_cannot_commit(setup,tmp_path):
    _, ns, clock, _, _, make, _, _, validate = setup
    def slow_auth(phase, job):
        if phase == "enqueue_commit": clock.now = 1001000
        return True
    first = AsyncStore(tmp_path / "expiry-a.sqlite",namespace=ns,authorize=slow_auth,validate_result=validate,clock=clock)
    with pytest.raises(QueueError) as expired:
        first.enqueue_and_commit("cam-a","run-a",0,{"v":1},make(deadline=1001,expires=1002))
    assert expired.value.code == "expired" and first.state("cam-a","run-a")== (0,{})
    clock.now=1000000
    def slow_validation(result, job):
        clock.now=1003000
        return True
    second = AsyncStore(tmp_path / "expiry-b.sqlite",namespace=ns,authorize=lambda *_:True,validate_result=slow_validation,clock=clock)
    job_id = second.enqueue_and_commit("cam-a","run-a",0,{},make(deadline=1003,expires=1005))["job_id"]
    claim = second.claim(lease_seconds=10)
    with pytest.raises(QueueError) as expired:
        second.complete(job_id,claim["attempt_id"],claim["job"]["identity"],{"text":"too late","structured":None})
    assert expired.value.code == "expired" and second.status(job_id)=="running"
    assert second.recover()["timed_out"]==1


def test_text_only_and_exported_wire_shapes_validate_against_draft(setup):
    store, _, _, _, _, make, _, _, _ = setup
    request = make(track=None); request["media"] = None; request["model_digest"] = None
    job_id=store.enqueue_and_commit("cam-a","run-a",0,{},request)["job_id"]
    schema=json.loads((Path(__file__).parents[1]/"spec/schemas/async-mllm.schema.json").read_text())
    validator=Draft202012Validator(schema)
    wire=store.wire_job(job_id)
    assert validator.is_valid(wire)
    seen=[]
    def provider(job,image):
        seen.append((job["identity"]["model_revision"],job["identity"]["source_event_time_ms"],image))
        return {"text":"synthetic reply","structured":None}
    assert store.dispatch_one(provider,None)["status"]=="completed"
    assert seen==[("v1",999000,None)]
    assert validator.is_valid(store.wire_completion(job_id))
    assert store.wire_completion(job_id)["status"]=="completed"
