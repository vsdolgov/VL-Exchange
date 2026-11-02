"""VideoPipe bridge for independently implemented bounded VLSP ingress.

Control messages are synchronous, while enqueue returns after admission and a
separate queue thread calls the real independent Session with retained pixels.
The hold/release gate is exclusively a deterministic overload test instrument.
"""
import bootstrap
from copy import deepcopy
import json
import sys
import threading
import time

from independent.worker import Session
from independent.stream_queue import StreamQueue


class ProcessFailure(Exception):
    # Constructed solely from the trusted local host's explicit failed result,
    # which carries the host's no-commit guarantee; not inferred from to_dict.
    vlsp_no_commit = True
    def __init__(self, error):
        super().__init__(error["message"])
        self.error = deepcopy(error)

    def to_dict(self):
        return deepcopy(self.error)


class Adapter:
    def __init__(self):
        self.session = Session()
        self.lock = threading.Lock()
        self.queues = {}
        self.futures = {}

    def handle(self, command):
        op, request_id = command["op"], command.get("id")
        if op == "open":
            if any(not row["queue"].snapshot()["stopped"] for row in self.queues.values()):
                return {"id": request_id, "ok": False, "error": {"code": "queue_still_running",
                        "message": "Stop and join all queues before opening another scenario"}}
            return self.session.handle(command)
        if op == "close":
            # Initiate each configured drain/discard policy, but never dispose
            # the scenario while an already accepted call remains in flight.
            stopped = [row["queue"].stop(timeout=0)["stopped"] for row in self.queues.values()]
            if not all(stopped):
                return {"id": request_id, "ok": False, "error": {"code": "queue_still_draining",
                        "message": "Queue shutdown is in progress; retry close after all workers stop"}}
            return self.session.handle(command)
        if op == "shutdown":
            for q in self.queues.values():
                q["gate"].set()
                if not q["queue"].stop(timeout=10)["stopped"]:
                    return {"id": request_id, "ok": False, "error": {"code": "queue_still_draining",
                            "message": "Scenario remains open while an accepted call is in flight"}}
            return self.session.handle(command)
        if op == "scenario_snapshot":
            return self.session.handle({"id": request_id, "op": "snapshot"})
        if op == "queue_open":
            policy = command["policy"]
            key = (policy["source_id"], policy["run_id"])
            if key in self.queues and not self.queues[key]["queue"].snapshot()["stopped"]:
                raise ValueError("A live queue already exists for this source/run")
            gate, started = threading.Event(), threading.Event()
            if not command.get("test_hold", False):
                gate.set()
            def process(inputs, context):
                started.set()
                callback_started = time.monotonic_ns()
                if not gate.wait(timeout=30):
                    raise TimeoutError("test gate was not released")
                frame_command = deepcopy(inputs["command"])
                arrived = frame_command.pop("_queue_ingress_ns")
                fail_after_commit = frame_command.pop("_test_after_commit_exception", False)
                frame_command["op"] = "process"
                frame_command["context"] = context
                # The descriptor becomes valid only for this process call;
                # pixels were retained from the native frame crossing the graph.
                scenario_started = time.monotonic_ns()
                result = self.session.handle(frame_command)
                ended = time.monotonic_ns()
                result["queue_timing"] = {"worker_arrival_to_callback_ms": (callback_started-arrived)/1e6,
                                          "session_call_ms": (ended-scenario_started)/1e6,
                                          "test_gate_ms": (scenario_started-callback_started)/1e6}
                if not result["ok"]:
                    raise ProcessFailure(result["error"])
                if fail_after_commit:
                    raise RuntimeError("test-only lost reply after successful commit")
                return result
            queue = StreamQueue(policy, process, require_every_frame=command.get("require_every_frame", False),
                                can_block=command.get("can_block", True), executor_lock=self.lock)
            self.queues[key] = {"queue": queue, "gate": gate, "started": started}
            return {"id": request_id, "ok": True, "queue": list(key)}
        key = (command["source_id"], command["run_id"])
        item = self.queues[key]
        queue = item["queue"]
        if op == "enqueue":
            payload = deepcopy(command)
            payload.pop("source_id", None)
            payload.pop("run_id", None)
            payload["_queue_ingress_ns"] = time.monotonic_ns()
            self.futures[request_id] = (key, queue.submit({"command": payload}, command["context"]))
            return {"id": request_id, "ok": True, "submitted": request_id}
        if op == "queue_wait_started":
            return {"id": request_id, "ok": item["started"].wait(timeout=10)}
        if op == "queue_release":
            item["gate"].set()
            return {"id": request_id, "ok": True}
        if op == "queue_release_after":
            timer = threading.Timer(command["delay_ms"] / 1000, item["gate"].set)
            timer.daemon = True
            timer.start()
            return {"id": request_id, "ok": True}
        if op == "queue_stop":
            return {"id": request_id, "ok": True, "stats": queue.stop(timeout=command.get("timeout", 0))}
        if op == "queue_status":
            return {"id": request_id, "ok": True, "stats": queue.snapshot(),
                    "completed": {str(k): future.result() for k, (owner, future) in self.futures.items()
                                  if owner == key and future.done()}}
        raise ValueError("Unknown streaming operation")


def main():
    adapter = Adapter()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            command = json.loads(line)
            response = adapter.handle(command)
            print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
            if command["op"] == "shutdown" and response.get("ok"):
                break
    finally:
        for item in adapter.queues.values():
            item["gate"].set()
            item["queue"].stop(timeout=10)
        adapter.session.shutdown()


if __name__ == "__main__":
    main()
