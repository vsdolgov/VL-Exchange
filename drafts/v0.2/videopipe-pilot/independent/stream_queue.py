"""Independent bounded ingress queue for VLSP 0.2 stream-policy v1.

One instance owns one logical source/run. Capacity counts waiting frames,
excluding the in-flight call. The queue itself is an out-of-band host adapter:
``inputs`` may be a frame envelope carrying host-owned media, and ``process``
is responsible for passing only the declared VLSP inputs/context to the engine.
The caller retains media until the returned Future reaches a terminal state.
There is no forced cancellation, persistence or exactly-once delivery.
"""
from collections import deque
from concurrent.futures import Future
from copy import deepcopy
import json
import math
from pathlib import Path
import threading
import time

from jsonschema import Draft202012Validator


_SCHEMA = Path(__file__).resolve().parents[2] / "scenario/spec/schemas/stream-policy.schema.json"
_ERROR_SCHEMA = _SCHEMA.with_name("error.schema.json")
_ERROR_VALIDATOR = Draft202012Validator(json.loads(_ERROR_SCHEMA.read_text()))


def _strict_json(value):
    kind = type(value)
    if kind is str:
        value.encode("utf-8", "strict")
    elif kind in (type(None), bool, int):
        return
    elif kind is float and math.isfinite(value):
        return
    elif kind is list:
        for child in value:
            _strict_json(child)
    elif kind is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _strict_json(key)
            _strict_json(child)
    else:
        raise ValueError("Expected finite JSON")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class _TerminalFuture(Future):
    """Cancellation is not offered: every retained frame has one outcome."""
    def cancel(self):
        return False


class StreamQueue:
    def __init__(self, policy, process, *, require_every_frame=False, can_block=True, executor_lock=None):
        try:
            _strict_json(policy)
            json.dumps(policy, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
            Draft202012Validator(json.loads(_SCHEMA.read_text())).validate(policy)
            if any(type(policy[key]) is not int for key in ("max_queue_frames", "max_wait_ms")):
                raise ValueError("queue bounds must be integer values")
        except Exception as exc:
            raise ValueError("invalid stream policy") from exc
        if type(require_every_frame) is not bool or type(can_block) is not bool or not callable(process):
            raise ValueError("invalid adapter configuration")
        if require_every_frame and (policy["overload_policy"] != "block" or policy["on_end"] != "drain"):
            raise ValueError("dropping is incompatible with require_every_frame")
        if not can_block and policy["overload_policy"] == "block":
            raise ValueError("source cannot implement block policy")
        if executor_lock is not None and not (hasattr(executor_lock, "acquire") and hasattr(executor_lock, "release")):
            raise ValueError("executor_lock must be a lock")
        self._policy = deepcopy(policy)
        self._process = process
        self._executor_lock = executor_lock
        self._condition = threading.Condition()
        self._waiting = deque()
        self._ending = False
        self._ended = False
        self._in_flight = False
        self._last_sequence = None
        self._last_time = None
        self._stats = {"submitted": 0, "accepted": 0, "committed": 0, "dropped": 0,
                       "rejected": 0, "errors": 0, "max_queue_depth": 0,
                       "drop_reasons": {"drop_oldest": 0, "drop_newest": 0, "discard": 0}}
        self._worker = threading.Thread(target=self._consume, name="vlsp-independent-ingress", daemon=True)
        self._worker.start()

    @property
    def policy(self):
        return deepcopy(self._policy)

    def _snapshot_locked(self):
        return {**deepcopy(self._stats), "queued": len(self._waiting),
                "in_flight": int(self._in_flight), "stopped": self._ended}

    def snapshot(self):
        with self._condition:
            return self._snapshot_locked()

    @staticmethod
    def _context_valid(inputs, context):
        if type(inputs) is not dict or type(context) is not dict:
            return False
        if set(context) != {"source_id", "run_id", "sequence", "event_time_ms"}:
            return False
        if any(type(context[k]) is not str or not 1 <= len(context[k]) <= 256 for k in ("source_id", "run_id")):
            return False
        if any(type(context[k]) is not int or context[k] < 0 for k in ("sequence", "event_time_ms")):
            return False
        # If the adapter carries the VLSP context in a larger message, reject a
        # disagreement rather than binding the wrong pixels to a source/run.
        try:
            _strict_json(inputs)
            _strict_json(context)
            if "context" in inputs and _canonical(inputs["context"]) != _canonical(context):
                return False
            json.dumps(inputs, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
            json.dumps(context, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return False
        return True

    def submit(self, inputs, context):
        future = _TerminalFuture()
        valid = self._context_valid(inputs, context)
        if valid:
            try:
                detached_inputs, detached_context = deepcopy(inputs), deepcopy(context)
            except Exception:
                valid = False
        completions = []
        with self._condition:
            self._stats["submitted"] += 1
            if not valid:
                reason = "invalid_input"
            elif self._ending:
                reason = "closed"
            elif (detached_context["source_id"], detached_context["run_id"]) != (self._policy["source_id"], self._policy["run_id"]):
                reason = "source_mismatch"
            elif self._last_sequence is not None and (detached_context["sequence"] <= self._last_sequence or detached_context["event_time_ms"] < self._last_time):
                reason = "input_order"
            else:
                reason = None
            if reason is not None:
                self._stats["rejected"] += 1
                completions.append((future, {"status": "rejected", "reason": reason}))
            else:
                timeout = self._policy["max_wait_ms"] / 1000
                end = time.monotonic() + timeout
                mode = self._policy["overload_policy"]
                while mode == "block" and len(self._waiting) >= self._policy["max_queue_frames"] and not self._ending:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                # Other producers may have been accepted while this call waited.
                if self._ending:
                    self._stats["rejected"] += 1
                    completions.append((future, {"status": "rejected", "reason": "closed"}))
                elif self._last_sequence is not None and (detached_context["sequence"] <= self._last_sequence or detached_context["event_time_ms"] < self._last_time):
                    self._stats["rejected"] += 1
                    completions.append((future, {"status": "rejected", "reason": "input_order"}))
                elif len(self._waiting) >= self._policy["max_queue_frames"] and mode == "block":
                    self._stats["rejected"] += 1
                    completions.append((future, {"status": "rejected", "reason": "queue_timeout"}))
                elif len(self._waiting) >= self._policy["max_queue_frames"] and mode == "drop_newest":
                    self._drop_locked(future, "drop_newest", completions)
                else:
                    if len(self._waiting) >= self._policy["max_queue_frames"]:
                        _, _, old_future = self._waiting.popleft()
                        self._drop_locked(old_future, "drop_oldest", completions)
                    self._waiting.append((detached_inputs, detached_context, future))
                    self._last_sequence, self._last_time = detached_context["sequence"], detached_context["event_time_ms"]
                    self._stats["accepted"] += 1
                    self._stats["max_queue_depth"] = max(self._stats["max_queue_depth"], len(self._waiting))
                    self._condition.notify_all()
        for item, outcome in completions:
            item.set_result(outcome)
        return future

    def _drop_locked(self, future, reason, completions):
        self._stats["dropped"] += 1
        self._stats["drop_reasons"][reason] += 1
        completions.append((future, {"status": "dropped", "reason": reason}))

    def _consume(self):
        try:
            while True:
                with self._condition:
                    while not self._waiting and not self._ending:
                        self._condition.wait()
                    if not self._waiting and self._ending:
                        return
                    inputs, context, future = self._waiting.popleft()
                    self._in_flight = True
                    self._condition.notify_all()
                try:
                    if self._executor_lock is None:
                        result = self._process(inputs, context)
                    else:
                        with self._executor_lock:
                            result = self._process(inputs, context)
                    outcome = {"status": "committed", "result": result}
                except BaseException as exc:
                    try:
                        if getattr(exc, "vlsp_no_commit", False) is not True:
                            raise ValueError("Callback gave no trusted no-commit receipt")
                        detail = exc.to_dict()
                        _strict_json(detail)
                        _ERROR_VALIDATOR.validate(detail)
                    except Exception:
                        detail = None
                    if detail is None:
                        outcome = {"status": "error", "reason": "callback_outcome_unknown", "commit": "unknown",
                                   "message": "Host callback failed: " + type(exc).__name__}
                    else:
                        outcome = {"status": "error", "error": detail}
                with self._condition:
                    self._in_flight = False
                    self._stats["committed" if outcome["status"] == "committed" else "errors"] += 1
                    self._condition.notify_all()
                future.set_result(outcome)
        finally:
            with self._condition:
                self._ended = True
                self._condition.notify_all()

    def stop(self, timeout=None):
        if timeout is not None and (type(timeout) not in (int, float) or timeout < 0):
            raise ValueError("timeout must be non-negative seconds or None")
        completions = []
        with self._condition:
            if not self._ending:
                self._ending = True
                if self._policy["on_end"] == "discard":
                    while self._waiting:
                        _, _, future = self._waiting.popleft()
                        self._drop_locked(future, "discard", completions)
                self._condition.notify_all()
        for future, outcome in completions:
            future.set_result(outcome)
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout)
        return self.snapshot()
