"""Deterministic ingress-policy tests; no model or VideoPipe claims."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from vl_scenario_v02.stream_queue import StreamQueue
from vl_scenario_v02.errors import ScenarioError


def policy(mode="block", end="drain", source="a", *, size=2, wait=50):
    return {"format": "vlsp.stream-policy", "version": "1.0.0", "source_id": source,
            "run_id": "r", "max_queue_frames": size, "overload_policy": mode,
            "max_wait_ms": wait, "on_end": end}


def frame(n, source="a"):
    return {"frame": {"frame_id": str(n), "frame_ref": "opaque", "width": 4, "height": 2}}, {
        "source_id": source, "run_id": "r", "sequence": n, "event_time_ms": 1000+n}


class Controlled:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []

    def process(self, inputs, context):
        self.calls.append((inputs, context))
        if len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("test did not release first call")
        return {"events": [], "state": {"sequence": context["sequence"]}}


@pytest.mark.parametrize("mode,dropped,expected", [
    ("drop_newest", "drop_newest", [1, 2, 3]),
    ("drop_oldest", "drop_oldest", [1, 3, 4]),
])
def test_overflow_modes_preserve_fifo_and_report_every_drop(mode, dropped, expected):
    host = Controlled()
    config = policy(mode)
    queue = StreamQueue(config, host.process)
    try:
        config["max_queue_frames"] = 1000
        queue.policy["max_queue_frames"] = 1000
        assert queue.policy["max_queue_frames"] == 2
        one = queue.submit(*frame(1))
        assert host.entered.wait(2)
        two, three, four = [queue.submit(*frame(n)) for n in (2, 3, 4)]
        victim = four if mode == "drop_newest" else two
        assert victim.result(1) == {"status": "dropped", "reason": dropped}
        assert queue.snapshot()["queued"] == 2
        host.release.set()
        end = queue.stop(timeout=2)
        assert end["stopped"] is True
        assert [call[1]["sequence"] for call in host.calls] == expected
        for item in (one, three, two if mode == "drop_newest" else four):
            assert item.result(1)["status"] == "committed"
        assert (end["submitted"], end["accepted"], end["committed"], end["dropped"]) == (
            4, 3 if mode == "drop_newest" else 4, 3, 1)
        assert end["drop_reasons"][dropped] == 1 and end["max_queue_depth"] == 2
    finally:
        host.release.set()
        queue.stop(timeout=2)


def test_block_timeout_and_no_unobserved_inference():
    host = Controlled(); queue = StreamQueue(policy(), host.process)
    try:
        queue.submit(*frame(1)); assert host.entered.wait(2)
        two, three = queue.submit(*frame(2)), queue.submit(*frame(3))
        start = time.monotonic()
        timeout = queue.submit(*frame(4)).result(1)
        assert timeout == {"status": "rejected", "reason": "queue_timeout"}
        assert time.monotonic() - start >= 0.035
        host.release.set(); end = queue.stop(timeout=2)
        assert two.result(1)["status"] == three.result(1)["status"] == "committed"
        assert [call[1]["sequence"] for call in host.calls] == [1, 2, 3]
        assert end["rejected"] == 1 and end["stopped"]
    finally:
        host.release.set(); queue.stop(timeout=2)


@pytest.mark.parametrize("mode,expected", [("block", "rejected"), ("drop_newest", "dropped")])
def test_unaccepted_frame_can_be_retried_after_capacity_recovers(mode, expected):
    host = Controlled(); queue = StreamQueue(policy(mode, size=1), host.process)
    try:
        queue.submit(*frame(1)); assert host.entered.wait(2)
        second = queue.submit(*frame(2))
        assert queue.submit(*frame(3)).result(1)["status"] == expected
        host.release.set(); assert second.result(2)["status"] == "committed"
        assert queue.submit(*frame(3)).result(2)["status"] == "committed"
        assert [item[1]["sequence"] for item in host.calls] == [1, 2, 3]
    finally:
        host.release.set(); queue.stop(timeout=2)


def test_block_resumes_when_capacity_opens_and_snapshots_caller_data():
    host = Controlled(); queue = StreamQueue(policy(wait=1000, size=1), host.process)
    try:
        queue.submit(*frame(1)); assert host.entered.wait(2)
        inputs, context = frame(2)
        second = queue.submit(inputs, context)
        inputs["frame"]["frame_id"] = "changed-after-submit"
        context["source_id"] = "changed-after-submit"
        with ThreadPoolExecutor(max_workers=1) as pool:
            third_submit = pool.submit(queue.submit, *frame(3))
            assert not third_submit.done()
            host.release.set()
            third = third_submit.result(2)
        assert second.result(2)["status"] == third.result(2)["status"] == "committed"
        assert host.calls[1][0]["frame"]["frame_id"] == "2"
        assert host.calls[1][1]["source_id"] == "a"
    finally:
        host.release.set(); queue.stop(timeout=2)


@pytest.mark.parametrize("end", ["drain", "discard"])
def test_stop_never_preempts_in_flight_and_accounts_pending(end):
    host = Controlled(); queue = StreamQueue(policy(end=end), host.process)
    one = queue.submit(*frame(1)); assert host.entered.wait(2)
    two, three = queue.submit(*frame(2)), queue.submit(*frame(3))
    interim = queue.stop(timeout=0.01)
    assert interim["stopped"] is False and interim["in_flight"] == 1
    if end == "discard":
        assert two.result(1) == three.result(1) == {"status": "dropped", "reason": "discard"}
    assert queue.submit(*frame(4)).result(1) == {"status": "rejected", "reason": "closed"}
    host.release.set(); final = queue.stop(timeout=2)
    assert final["stopped"] and one.result(1)["status"] == "committed"
    assert final["dropped"] == (2 if end == "discard" else 0)
    assert [x[1]["sequence"] for x in host.calls] == ([1] if end == "discard" else [1, 2, 3])


def test_isolation_and_bad_configuration():
    with pytest.raises(ValueError): StreamQueue(policy("drop_newest"), lambda x, y: {}, require_every_frame=True)
    with pytest.raises(ValueError): StreamQueue(policy(end="discard"), lambda x, y: {}, require_every_frame=True)
    with pytest.raises(ValueError): StreamQueue(policy(), lambda x, y: {}, can_block=False)
    with pytest.raises(ValueError): StreamQueue(policy(), lambda x, y: {}, executor_lock=object())
    for invalid in (True, 1.0, 0):
        value = policy(); value["max_queue_frames"] = invalid
        with pytest.raises(ValueError): StreamQueue(value, lambda x, y: {})
    calls = []; lock = threading.Lock()
    def process(inputs, context):
        calls.append((context["source_id"], context["sequence"]))
        return {"events": [], "state": {}}
    a = StreamQueue(policy(source="a"), process, executor_lock=lock)
    b = StreamQueue(policy(source="b"), process, executor_lock=lock)
    try:
        assert a.submit(*frame(1, "b")).result(1) == {"status": "rejected", "reason": "source_mismatch"}
        assert a.submit(*frame(1, "a")).result(1)["status"] == "committed"
        assert b.submit(*frame(1, "b")).result(1)["status"] == "committed"
        assert a.submit(*frame(1, "a")).result(1) == {"status": "rejected", "reason": "input_order"}
        assert set(calls) == {("a", 1), ("b", 1)}
    finally:
        a.stop(timeout=2); b.stop(timeout=2)


def test_callback_error_is_returned_without_fabricated_empty_success():
    def fail(inputs, context):
        error = ScenarioError("service_failed", "synthetic", fatal=True)
        error.vlsp_no_commit = True  # Trusted callback's explicit no-commit assertion.
        raise error
    queue = StreamQueue(policy(), fail)
    try:
        result = queue.submit(*frame(1)).result(2)
        assert result["status"] == "error" and result["error"]["code"] == "service_failed"
        assert result["error"]["fatal"] is True and "result" not in result
    finally:
        stats = queue.stop(timeout=2)
        assert stats["errors"] == 1 and stats["committed"] == 0


def test_bad_ingress_never_advances_admission_order():
    calls = []
    def process(inputs, context):
        calls.append(context["sequence"])
        return {"events": [], "state": {}}
    queue = StreamQueue(policy(), process)
    try:
        inputs, context = frame(1)
        for bad_inputs, bad_context in (
            (inputs, {**context, "extra": 1}),
            ({"frame": {"x": float("nan")}}, context),
            ({"frame": {"x": "\ud800"}}, context),
            ({"frame": inputs, "context": {**context, "source_id": "b"}}, context),
            ({"frame": inputs, "context": {**context, "sequence": True}}, context),
            (inputs, {**context, "sequence": True}),
        ):
            assert queue.submit(bad_inputs, bad_context).result(1) == {"status": "rejected", "reason": "invalid_input"}
        assert queue.submit(*frame(1)).result(1)["status"] == "committed"
        assert calls == [1]
        assert queue.snapshot()["rejected"] == 6
    finally:
        queue.stop(timeout=2)


def test_unstructured_callback_exception_does_not_claim_rollback():
    committed = []
    def callback(inputs, context):
        committed.append(context["sequence"])
        raise RuntimeError("lost response after commit")
    queue = StreamQueue(policy(), callback)
    try:
        result = queue.submit(*frame(1)).result(2)
        assert committed == [1]
        assert result == {"status": "error", "reason": "callback_outcome_unknown",
                          "commit": "unknown", "message": "Host callback failed: RuntimeError"}
        assert "error" not in result
    finally:
        assert queue.stop(timeout=2)["errors"] == 1


def test_structured_exception_without_trusted_marker_can_follow_commit():
    committed = []
    def callback(inputs, context):
        committed.append(context["sequence"])
        raise ScenarioError("service_failed", "synthetic", fatal=True)
    queue = StreamQueue(policy(), callback)
    try:
        result = queue.submit(*frame(1)).result(2)
        assert committed == [1]
        assert result == {"status": "error", "reason": "callback_outcome_unknown",
                          "commit": "unknown", "message": "Host callback failed: ScenarioError"}
    finally:
        assert queue.stop(timeout=2)["errors"] == 1


@pytest.mark.parametrize("marker,code", [(False, "service_failed"), (1, "service_failed"), (True, "invalid-code")])
def test_marker_must_be_literal_true_and_error_envelope_valid(marker, code):
    def callback(inputs, context):
        error = ScenarioError(code, "synthetic", fatal=True)
        error.vlsp_no_commit = marker
        raise error
    queue = StreamQueue(policy(), callback)
    try:
        result = queue.submit(*frame(1)).result(2)
        assert result["reason"] == "callback_outcome_unknown" and result["commit"] == "unknown"
    finally:
        queue.stop(timeout=2)
