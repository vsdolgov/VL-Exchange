"""Bounded per-source ingress adapter for VLSP stream-policy.v1.

The queue's limit applies to *waiting* frames; one additional frame can be in
flight. It does not bound producer-owned memory, callback memory or execution
time, and cannot preempt an already started call. Use one shared executor_lock
when multiple sources share a non-concurrent scenario executor.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from copy import deepcopy
import json
import math
from pathlib import Path
import threading
import time

from jsonschema import Draft202012Validator, validators


_SCHEMA = Path(__file__).resolve().parents[2] / "spec/schemas/stream-policy.schema.json"
_ERROR_SCHEMA = Path(__file__).resolve().parents[2] / "spec/schemas/error.schema.json"
_STRICT = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda checker, value: type(value) is int))


def _finite_json(value):
    """Accept only finite, strict UTF-8 JSON; reject Python-only objects."""
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is str:
        value.encode("utf-8", "strict")
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_finite_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _finite_json(key) and _finite_json(item)
                   for key, item in value.items())
    return False


def _valid_envelope(inputs, context):
    try:
        return (type(inputs) is dict and type(context) is dict and
                set(context) == {"source_id", "run_id", "sequence", "event_time_ms"} and
                all(type(context[key]) is str and 1 <= len(context[key]) <= 256
                    for key in ("source_id", "run_id")) and
                type(context["sequence"]) is int and context["sequence"] >= 0 and
                type(context["event_time_ms"]) is int and context["event_time_ms"] >= 0 and
                _finite_json(inputs) and _finite_json(context) and
                ("context" not in inputs or
                 json.dumps(inputs["context"], sort_keys=True, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":")) ==
                 json.dumps(context, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
    except (UnicodeError, RecursionError):
        return False


class StreamQueue:
    """One worker and one bounded FIFO scoped to one source/run.

    submit returns a non-cancellable Future with a terminal result for every
    offered frame. Queue rejection is not a scenario exception: the host-side
    result contains status/reason; error is reserved for a callback failure.
    A structured error is exposed only if a trusted callback explicitly sets
    ``vlsp_no_commit is True`` and its envelope validates against the VLSP
    error schema. The marker is a callback assertion, not a security boundary.
    stop is idempotent and may be called again if its timeout expires.
    """

    def __init__(self, policy, process, *, require_every_frame=False, can_block=True, executor_lock=None):
        schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
        try:
            if type(policy) is not dict or not _finite_json(policy):
                raise ValueError("Policy must be finite strict UTF-8 JSON")
            _STRICT(schema).validate(policy)
        except Exception as exc:
            raise ValueError("Invalid vlsp.stream-policy.v1 configuration") from exc
        if not callable(process):
            raise ValueError("process must be callable")
        if executor_lock is not None and not all(callable(getattr(executor_lock, name, None)) for name in ("acquire", "release")):
            raise ValueError("executor_lock must provide acquire and release")
        if type(require_every_frame) is not bool or type(can_block) is not bool:
            raise ValueError("require_every_frame and can_block must be booleans")
        if require_every_frame and policy["overload_policy"] != "block":
            raise ValueError("A deployment requiring every frame cannot drop input")
        if require_every_frame and policy["on_end"] == "discard":
            raise ValueError("A deployment requiring every frame cannot discard queued input")
        if not can_block and policy["overload_policy"] == "block":
            raise ValueError("This source cannot implement block")
        self._policy = deepcopy(policy)
        self._process = process
        self._executor_lock = executor_lock
        self._error_validator = _STRICT(json.loads(_ERROR_SCHEMA.read_text(encoding="utf-8")))
        self._cv = threading.Condition()
        self._waiting = deque()
        self._closing = False
        self._last = None
        self._counts = {"submitted": 0, "accepted": 0, "committed": 0,
                        "dropped": 0, "rejected": 0, "errors": 0,
                        "in_flight": 0, "max_queue_depth": 0,
                        "drop_reasons": {"drop_oldest": 0, "drop_newest": 0, "discard": 0}}
        self._worker = threading.Thread(target=self._run, name="vlsp-ingress-reference", daemon=True)
        self._worker.start()

    @staticmethod
    def _future():
        result = Future()
        # Cancellation of an accepted Future must not silently discard a frame.
        result.set_running_or_notify_cancel()
        return result

    @property
    def policy(self):
        return deepcopy(self._policy)

    def _reject_locked(self, reason):
        self._counts["rejected"] += 1
        return {"status": "rejected", "reason": reason}

    def _drop_locked(self, reason):
        self._counts["dropped"] += 1
        self._counts["drop_reasons"][reason] += 1
        return {"status": "dropped", "reason": reason}

    def submit(self, inputs, context):
        future = self._future()
        pending = []
        try:
            snapshot_inputs, snapshot_context = deepcopy(inputs), deepcopy(context)
        except Exception:
            snapshot_inputs, snapshot_context = None, None
        with self._cv:
            self._counts["submitted"] += 1
            if self._closing:
                answer = self._reject_locked("closed")
            elif not _valid_envelope(snapshot_inputs, snapshot_context):
                answer = self._reject_locked("invalid_input")
            elif (snapshot_context.get("source_id") != self._policy["source_id"] or
                  snapshot_context.get("run_id") != self._policy["run_id"]):
                answer = self._reject_locked("source_mismatch")
            else:
                sequence, timestamp = snapshot_context["sequence"], snapshot_context["event_time_ms"]
                deadline = time.monotonic() + self._policy["max_wait_ms"] / 1000.0
                answer = None
                while True:
                    if self._closing:
                        answer = self._reject_locked("closed")
                        break
                    if self._last is not None and (sequence <= self._last[0] or timestamp < self._last[1]):
                        answer = self._reject_locked("input_order")
                        break
                    if len(self._waiting) < self._policy["max_queue_frames"]:
                        break
                    mode = self._policy["overload_policy"]
                    if mode == "drop_newest":
                        answer = self._drop_locked("drop_newest")
                        break
                    if mode == "drop_oldest":
                        old = self._waiting.popleft()
                        pending.append((old[2], self._drop_locked("drop_oldest")))
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        answer = self._reject_locked("queue_timeout")
                        break
                    self._cv.wait(remaining)
                if answer is None:
                    self._waiting.append((snapshot_inputs, snapshot_context, future))
                    self._last = (sequence, timestamp)
                    self._counts["accepted"] += 1
                    self._counts["max_queue_depth"] = max(self._counts["max_queue_depth"], len(self._waiting))
                    self._cv.notify_all()
        for old_future, outcome in pending:
            old_future.set_result(outcome)
        if answer is not None:
            future.set_result(answer)
        return future

    def _run(self):
        while True:
            with self._cv:
                while not self._waiting and not self._closing:
                    self._cv.wait()
                if not self._waiting and self._closing:
                    return
                inputs, context, future = self._waiting.popleft()
                self._counts["in_flight"] = 1
                self._cv.notify_all()
            try:
                if self._executor_lock is None:
                    result = self._process(inputs, context)
                else:
                    self._executor_lock.acquire()
                    try:
                        result = self._process(inputs, context)
                    finally:
                        self._executor_lock.release()
                answer, metric = {"status": "committed", "result": deepcopy(result)}, "committed"
            except BaseException as exc:
                error = None
                try:
                    if getattr(exc, "vlsp_no_commit", False) is True and callable(getattr(exc, "to_dict", None)):
                        error = deepcopy(exc.to_dict())
                        if type(error) is not dict or not _finite_json(error) or not self._error_validator.is_valid(error):
                            error = None
                except Exception:
                    error = None
                if type(error) is dict:
                    answer = {"status": "error", "error": error}
                else:
                    # A transport or callback can fail after the host committed.
                    # The adapter cannot infer rollback from such an exception.
                    answer = {"status": "error", "reason": "callback_outcome_unknown",
                              "commit": "unknown", "message": "Host callback failed: " + type(exc).__name__}
                metric = "errors"
            with self._cv:
                self._counts[metric] += 1
                self._counts["in_flight"] = 0
                self._cv.notify_all()
            future.set_result(answer)

    def snapshot(self):
        with self._cv:
            return {**deepcopy(self._counts), "queued": len(self._waiting),
                    "stopped": self._closing and not self._worker.is_alive()}

    def stop(self, timeout=None):
        pending = []
        with self._cv:
            if not self._closing:
                self._closing = True
                if self._policy["on_end"] == "discard":
                    while self._waiting:
                        _, _, future = self._waiting.popleft()
                        pending.append((future, self._drop_locked("discard")))
                self._cv.notify_all()
        for future, answer in pending:
            future.set_result(answer)
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout)
        return self.snapshot()
