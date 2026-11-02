"""Prove real source/adapter/sink, frame bytes, order, and graceful shutdown."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
binary = root / "build/adapter/vlsp_videopipe"
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
binary_before = sha(binary)
test_sources = [root / "cpp/smoke_transport.py", root / "cpp/echo_worker_test.py"]
test_source_hashes = {str(path.relative_to(root)): sha(path) for path in test_sources}
requests = [
    {"id": 1, "op": "open", "package": "transport-only"},
    {"id": 2, "op": "process", "image": {"width": 2, "height": 1, "bgr_hex": "001122aabbcc"},
     "inputs": {"frame": {"frame_id": "camera-a-frame-17"}},
     "context": {"source_id": "camera-a", "run_id": "run-1", "sequence": 17, "event_time_ms": 1793640000000}},
    {"id": 3, "op": "enqueue", "image": {"width": 2, "height": 1, "bgr_hex": "123456abcdef"},
     "inputs": {"frame": {"frame_id": "camera-b-frame-4"}},
     "context": {"source_id": "camera-b", "run_id": "run-2", "sequence": 4, "event_time_ms": 1793640000123}},
    {"id": 4, "op": "snapshot"},
    {"id": 5, "op": "reset"},
    {"id": 6, "op": "close"},
    {"id": 7, "op": "shutdown"},
]
completed = subprocess.run(
    [str(root / "build/adapter/vlsp_videopipe"), "--python", sys.executable,
     "--worker", str(root / "cpp/echo_worker_test.py")],
    input="".join(json.dumps(item) + "\n" for item in requests), text=True,
    capture_output=True, timeout=20, check=True,
)
responses = [json.loads(line) for line in completed.stdout.splitlines()]
assert len(responses) == len(requests)
for index, (request, response) in enumerate(zip(requests, responses), 1):
    assert response["echo"] == request
    transport = response["transport"]
    assert all(transport[field] == index for field in ("source_seen", "adapter_seen", "sink_seen"))
    assert len(set(transport["threads"].values())) == 3
    assert transport["hooks"]["request_source"]["leaving"]["count"] == index
    assert transport["hooks"]["vlsp_adapter"]["handling"]["count"] == index
    assert transport["hooks"]["response_sink"]["handling"]["count"] == index
assert responses[1]["transport"]["meta_type"] == "FRAME"
assert responses[1]["transport"]["frame_bytes"] == 6
assert responses[1]["transport"]["frame_fnv1a64"]
assert responses[1]["transport"]["native_frame_index"] == 0
assert responses[1]["transport"]["native_channel_index"] == 0
assert responses[1]["transport"]["logical"] == {**requests[1]["context"], "frame_id": "camera-a-frame-17"}
assert responses[2]["transport"]["meta_type"] == "FRAME"
assert responses[2]["transport"]["frame_bytes"] == 6
assert responses[2]["transport"]["frame_fnv1a64"] != responses[1]["transport"]["frame_fnv1a64"]
assert responses[2]["transport"]["native_frame_index"] == 1
assert responses[2]["transport"]["native_channel_index"] == 0
assert responses[2]["transport"]["logical"] == {**requests[2]["context"], "frame_id": "camera-b-frame-4"}
assert responses[0]["transport"]["meta_type"] == "CONTROL"
rejected_requests = [{"id": 1, "op": "shutdown", "deny_shutdown": True},
                     {"id": 2, "op": "snapshot"}, {"id": 3, "op": "shutdown"}]
rejected = subprocess.run(
    [str(binary), "--python", sys.executable, "--worker", str(root / "cpp/echo_worker_test.py")],
    input="".join(json.dumps(item) + "\n" for item in rejected_requests),
    text=True, capture_output=True, timeout=20, check=True,
)
rejected_responses = [json.loads(line) for line in rejected.stdout.splitlines()]
assert len(rejected_responses) == 3
assert [item["ok"] for item in rejected_responses] == [False, True, True]
for index, (request, response) in enumerate(zip(rejected_requests, rejected_responses), 1):
    assert response["echo"] == request
    assert all(response["transport"][field] == index for field in ("source_seen", "adapter_seen", "sink_seen"))
assert not rejected.stderr
abnormal = subprocess.run(
    [str(root / "build/adapter/vlsp_videopipe"), "--python", sys.executable,
     "--worker", str(root / "cpp/echo_worker_test.py")],
    input=json.dumps({"id": 1, "op": "shutdown", "exit_code": 7}) + "\n",
    text=True, capture_output=True, timeout=20,
)
assert abnormal.returncode == 7, (abnormal.returncode, abnormal.stderr)
assert json.loads(abnormal.stdout)["ok"] is True  # Ack does not prove successful teardown.
assert "worker exited with code 7" in abnormal.stderr
assert sha(binary) == binary_before, "Binary changed while transport smoke was running"
report = {"ok": True, "commands": len(responses), "stderr": completed.stderr,
          "binary_sha256": binary_before, "test_sources_sha256": test_source_hashes,
          "normal_exit_code": completed.returncode,
          "responses": responses,
          "rejected_shutdown": {"exit_code": rejected.returncode, "responses": rejected_responses},
          "abnormal_worker_exit": {"adapter_exit_code": abnormal.returncode,
                                   "shutdown_ack_received": True, "stderr": abnormal.stderr}}
output = root / "build/transport-smoke.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"ok": True, "commands": len(responses), "report": str(output)}))
