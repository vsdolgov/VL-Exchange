"""Deterministic queue contract tests: callback is a fake, no ML inference."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import pytest

from independent.stream_queue import StreamQueue


def policy(mode, *, end="drain", wait=40, source="camera-a", run="run-1", capacity=1):
    return {"format": "vlsp.stream-policy", "version": "1.0.0", "source_id": source, "run_id": run,
            "max_queue_frames": capacity, "overload_policy": mode, "max_wait_ms": wait, "on_end": end}


def packet(seq, *, source="camera-a", run="run-1"):
    context = {"source_id": source, "run_id": run, "sequence": seq, "event_time_ms": 1000 + seq}
    return {"frame": {"frame_id": f"f{seq}", "frame_ref": f"ref{seq}", "width": 4, "height": 2}}, context


class Gate:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen = []

    def __call__(self, inputs, context):
        self.seen.append((deepcopy(inputs), deepcopy(context)))
        if len(self.seen) == 1:
            self.started.set()
            assert self.release.wait(5), "test did not release worker"
        return {"sequence": context["sequence"]}


@pytest.mark.parametrize("mode,dropped,kept", [("drop_oldest", 2, 3), ("drop_newest", 3, 2)])
def test_bounded_drop_modes_do_not_process_evicted_frames(mode, dropped, kept):
    process = Gate(); queue = StreamQueue(policy(mode), process)
    try:
        first = queue.submit(*packet(1)); assert process.started.wait(2)
        second = queue.submit(*packet(2)); third = queue.submit(*packet(3))
        victim = {2: second, 3: third}[dropped]
        assert victim.result(1) == {"status": "dropped", "reason": mode}
        assert queue.snapshot()["max_queue_depth"] == 1
        process.release.set()
        assert first.result(2)["status"] == "committed"
        assert {2: second, 3: third}[kept].result(2)["status"] == "committed"
        assert [x[1]["sequence"] for x in process.seen] == [1, kept]
        stats = queue.stop(2)
        assert stats["stopped"] and stats["committed"] == 2 and stats["dropped"] == 1
        assert stats["drop_reasons"][mode] == 1 and stats["queued"] == stats["in_flight"] == 0
    finally:
        process.release.set(); queue.stop(2)


def test_block_timeout_is_explicit_and_same_unaccepted_frame_can_retry():
    process = Gate(); queue = StreamQueue(policy("block", wait=25), process)
    try:
        first = queue.submit(*packet(1)); assert process.started.wait(2)
        second = queue.submit(*packet(2))
        assert queue.submit(*packet(3)).result(1) == {"status": "rejected", "reason": "queue_timeout"}
        process.release.set()
        assert first.result(2)["status"] == second.result(2)["status"] == "committed"
        assert queue.submit(*packet(3)).result(2)["status"] == "committed"
        stats = queue.stop(2)
        assert (stats["submitted"], stats["accepted"], stats["committed"], stats["rejected"]) == (4, 3, 3, 1)
        assert [x[1]["sequence"] for x in process.seen] == [1, 2, 3]
    finally:
        process.release.set(); queue.stop(2)


def test_block_waits_for_capacity_and_detaches_caller_data():
    process = Gate(); queue = StreamQueue(policy("block", wait=1000), process)
    try:
        first = queue.submit(*packet(1)); assert process.started.wait(2)
        inputs, context = packet(2)
        second = queue.submit(inputs, context)
        inputs["frame"]["frame_id"] = "caller-mutated"
        context["sequence"] = 999
        entered = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def blocked_submit():
                entered.set()
                return queue.submit(*packet(3))
            producer = pool.submit(blocked_submit)
            assert entered.wait(2)
            process.release.set()
            third = producer.result(2)
        assert [first.result(2)["status"], second.result(2)["status"], third.result(2)["status"]] == ["committed"] * 3
        assert process.seen[1][0]["frame"]["frame_id"] == "f2"
        assert [row[1]["sequence"] for row in process.seen] == [1, 2, 3]
    finally:
        process.release.set(); queue.stop(2)


@pytest.mark.parametrize("end", ["drain", "discard"])
def test_stop_respects_end_policy_without_cancelling_in_flight(end):
    process = Gate(); queue = StreamQueue(policy("block", end=end), process)
    first = queue.submit(*packet(1)); assert process.started.wait(2)
    second = queue.submit(*packet(2))
    try:
        assert queue.stop(0.01)["stopped"] is False
        assert queue.submit(*packet(3)).result(1) == {"status": "rejected", "reason": "closed"}
        if end == "discard":
            assert second.result(1) == {"status": "dropped", "reason": "discard"}
        process.release.set()
        assert first.result(2)["status"] == "committed"
        if end == "drain":
            assert second.result(2)["status"] == "committed"
        stats = queue.stop(2)
        assert stats["stopped"] and stats["queued"] == 0 and stats["in_flight"] == 0
        assert stats["drop_reasons"]["discard"] == (1 if end == "discard" else 0)
        assert queue.stop(2) == stats
    finally:
        process.release.set(); queue.stop(2)


def test_invalid_config_scope_and_order_do_not_reach_engine():
    with pytest.raises(ValueError): StreamQueue(policy("drop_oldest"), lambda *_: None, require_every_frame=True)
    with pytest.raises(ValueError): StreamQueue(policy("block", end="discard"), lambda *_: None, require_every_frame=True)
    with pytest.raises(ValueError): StreamQueue(policy("block"), lambda *_: None, can_block=False)
    with pytest.raises(ValueError): StreamQueue({**policy("block"), "max_queue_frames": 0}, lambda *_: None)
    with pytest.raises(ValueError): StreamQueue({**policy("block"), "max_queue_frames": 1.0}, lambda *_: None)
    with pytest.raises(ValueError): StreamQueue({**policy("block"), "max_wait_ms": True}, lambda *_: None)
    seen = []; queue = StreamQueue(policy("block"), lambda i, c: seen.append(c["sequence"]))
    try:
        assert queue.submit(*packet(1)).result(2)["status"] == "committed"
        assert queue.submit(*packet(1)).result(2) == {"status": "rejected", "reason": "input_order"}
        assert queue.submit(*packet(2, source="camera-b")).result(2) == {"status": "rejected", "reason": "source_mismatch"}
        broken = packet(2); broken[1]["sequence"] = True
        assert queue.submit(*broken).result(2) == {"status": "rejected", "reason": "invalid_input"}
        inputs, ctx = packet(2); inputs["context"] = {**ctx, "sequence": True}
        assert queue.submit(inputs, ctx).result(2) == {"status": "rejected", "reason": "invalid_input"}
        inputs, ctx = packet(2); inputs["bad"] = {1: "not-a-json-key"}
        assert queue.submit(inputs, ctx).result(2) == {"status": "rejected", "reason": "invalid_input"}
        inputs, ctx = packet(2); inputs["bad"] = (1, 2)
        assert queue.submit(inputs, ctx).result(2) == {"status": "rejected", "reason": "invalid_input"}
        assert seen == [1]
    finally:
        queue.stop(2)


def test_two_sources_share_executor_lock_and_keep_state_isolated():
    lock = threading.Lock(); gate = threading.Event(); first_in = threading.Event(); seen = []
    def process(inputs, context):
        seen.append(context["source_id"])
        if context["source_id"] == "camera-a":
            first_in.set(); assert gate.wait(5)
        return {"source": context["source_id"]}
    a = StreamQueue(policy("block"), process, executor_lock=lock)
    b = StreamQueue(policy("block", source="camera-b"), process, executor_lock=lock)
    try:
        fa = a.submit(*packet(1)); assert first_in.wait(2)
        fb = b.submit(*packet(1, source="camera-b"))
        gate.set()
        assert fa.result(2)["result"] == {"source": "camera-a"}
        assert fb.result(2)["result"] == {"source": "camera-b"}
        assert seen == ["camera-a", "camera-b"]
        assert a.stop(2)["committed"] == b.stop(2)["committed"] == 1
    finally:
        gate.set(); a.stop(2); b.stop(2)


def test_callback_exception_is_explicit_and_does_not_kill_worker():
    class Fault(Exception):
        vlsp_no_commit = True
        def to_dict(self):
            return {"stage": "execution", "code": "service_failed", "message": "test", "fatal": True, "retryable": False}
    def process(inputs, context):
        if context["sequence"] == 1: raise Fault()
        return {"ok": True}
    queue = StreamQueue(policy("block"), process)
    try:
        failed = queue.submit(*packet(1)).result(2)
        assert failed["status"] == "error" and failed["error"]["code"] == "service_failed"
        assert queue.submit(*packet(2)).result(2) == {"status": "committed", "result": {"ok": True}}
        assert queue.stop(2)["errors"] == 1
    finally:
        queue.stop(2)


def test_public_policy_cannot_mutate_active_queue_configuration():
    config = policy("block", end="drain")
    queue = StreamQueue(config, lambda *_: {})
    try:
        config["overload_policy"] = "drop_newest"
        returned = queue.policy
        returned["max_queue_frames"] = 99
        assert queue.policy["overload_policy"] == "block"
        assert queue.policy["max_queue_frames"] == 1
    finally:
        queue.stop(2)


def test_unstructured_callback_failure_does_not_claim_rollback():
    committed = []
    def callback(inputs, context):
        committed.append(context["sequence"])  # Model a host failure after commit.
        raise RuntimeError("do not leak arbitrary exception details")
    queue = StreamQueue(policy("block"), callback)
    try:
        result = queue.submit(*packet(1)).result(2)
        assert committed == [1]
        assert result == {"status": "error", "reason": "callback_outcome_unknown", "commit": "unknown",
                          "message": "Host callback failed: RuntimeError"}
        assert "error" not in result
        assert queue.stop(2)["errors"] == 1
    finally:
        queue.stop(2)


def test_structured_error_after_commit_without_receipt_is_unknown():
    committed = []
    class Fault(Exception):
        def to_dict(self):
            return {"stage": "execution", "code": "service_failed", "message": "test", "fatal": True, "retryable": False}
    def callback(inputs, context):
        committed.append(context["sequence"])
        raise Fault()
    queue = StreamQueue(policy("block"), callback)
    try:
        outcome = queue.submit(*packet(1)).result(2)
        assert committed == [1]
        assert outcome == {"status": "error", "reason": "callback_outcome_unknown", "commit": "unknown",
                           "message": "Host callback failed: Fault"}
    finally:
        queue.stop(2)


@pytest.mark.parametrize("marker", [True, 1, False, None])
def test_callback_requires_true_marker_and_valid_normative_envelope(marker):
    class Fault(Exception):
        vlsp_no_commit = marker
        def to_dict(self):
            # Even a true marker cannot make an invalid stage/code authoritative.
            return {"stage": "input", "code": "service_failed", "message": "test", "fatal": True, "retryable": False}
    def callback(inputs, context):
        raise Fault()
    queue = StreamQueue(policy("block"), callback)
    try:
        outcome = queue.submit(*packet(1)).result(2)
        assert outcome["reason"] == "callback_outcome_unknown" and outcome["commit"] == "unknown"
    finally:
        queue.stop(2)


@pytest.mark.parametrize("marker", [1, False, None])
def test_truthy_or_false_marker_does_not_attest_no_commit(marker):
    class Fault(Exception):
        vlsp_no_commit = marker
        def to_dict(self):
            return {"stage": "execution", "code": "service_failed", "message": "test", "fatal": True, "retryable": False}
    queue = StreamQueue(policy("block"), lambda *_: (_ for _ in ()).throw(Fault()))
    try:
        outcome = queue.submit(*packet(1)).result(2)
        assert outcome["reason"] == "callback_outcome_unknown" and outcome["commit"] == "unknown"
    finally:
        queue.stop(2)
