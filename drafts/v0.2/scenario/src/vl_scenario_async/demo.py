"""Run `python -m vl_scenario_async.demo`: local fake provider, no network.

Illustrates an application with its *own* SQLite state, not an integration with
the in-memory ScenarioExecutor. The media bytes are synthetic test data.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import copy
import tempfile
import threading

from .store import AsyncStore


def main():
    namespace = {"tenant_id":"example-tenant", "installation_id":"example-box",
                 "scenario_id":"org.vlexchange.example.async", "scenario_version":"1.0.0",
                 "scenario_manifest_sha256":"1" * 64}
    image = b"synthetic frame bytes; not a real image"
    media = {"handle":"local-opaque-example", "sha256":hashlib.sha256(image).hexdigest(),
             "expires_at_ms":2_000_000, "source_id":"cam-a", "run_id":"run-a", "frame_id":"frame-1"}
    job = {"frame_id":"frame-1", "track_id":"track-1", "tracker_alias":"tracker-a",
           "source_event_time_ms":999_000, "model_alias":"mllm-a", "model_revision":"test-only-1",
           "model_digest":"2" * 64, "payload":{"text":"Synthetic prompt", "response_schema":None, "parameters":{}},
           "media":media, "deadline_ms":1_900_000}
    started, finish = threading.Event(), threading.Event()
    def provider(request, image_bytes):
        assert image_bytes == image
        started.set()
        if not finish.wait(5): raise RuntimeError("demo timed out")
        return {"text":"simulated response", "structured":None}
    def authorize(phase, request): return True  # Test authorizer; no crypto or production licensing.
    def validate_result(result, request): return type(result.get("text")) is str
    with tempfile.TemporaryDirectory(prefix="vlsp-async-demo-") as temp:
        db = Path(temp) / "queue.sqlite"
        config = {"namespace":namespace,"authorize":authorize,"validate_result":validate_result,"clock":lambda:1_000_000}
        store = AsyncStore(db, **config)
        first = store.enqueue_and_commit("cam-a","run-a",0,{"frames_seen":1},job)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(store.dispatch_one,provider,lambda media, identity:image)
            assert started.wait(5)
            # Commit a second frame while the *test* provider is still pending.
            next_job = copy.deepcopy(job)
            next_job["frame_id"] = "frame-2"
            next_job["media"]["frame_id"] = "frame-2"
            next_job["media"]["handle"] = "local-opaque-example-2"
            next_result = store.enqueue_and_commit("cam-a","run-a",1,{"frames_seen":2},next_job)
            assert store.status(next_result["job_id"]) == "pending"
            finish.set()
            assert pending.result(timeout=5)["status"] == "completed"
        reopened = AsyncStore(db, **config)  # Recovery from persisted state after creating a new instance.
        def on_completion(state, result, identity):
            return {**state, "answer":result["text"], "for_frame":identity["frame_id"]}
        receipt = reopened.apply_result(first["job_id"],2,on_completion)
        replay = reopened.apply_result(first["job_id"],2,on_completion)
        print(json.dumps({"status":"pass","provider":"simulated; no network","delivery":receipt,
                          "second_frame_accepted_while_pending":True,"duplicate_suppressed":replay["duplicate"],"state":reopened.state("cam-a","run-a"),
                          "integrated_with_sync_executor":False,"real_mllm_tested":False},ensure_ascii=False))


if __name__ == "__main__":
    main()
