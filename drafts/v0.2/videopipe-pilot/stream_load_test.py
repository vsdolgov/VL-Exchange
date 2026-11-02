"""Real model load and deterministic overload through two VLSP hosts.

The VideoPipe ingress is a native vp_frame_meta/cv::Mat. The bounded policy
is applied in the worker immediately before the independent scenario host;
upstream VideoPipe queues are outside this policy. Synthetic test gates
deliberately pause a consumer and are not performance measurements.
"""
from pathlib import Path
import sys
PILOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PILOT / "python"))
import bootstrap
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from collections import Counter
import select
import signal
import subprocess
import threading
import time
import numpy as np
from runtime import Session, verify_fixtures
from run_pilot import source_paths
from vl_scenario_v02.stream_queue import StreamQueue

REPORT = PILOT / "independent/reports/stream-load.json"
IMAGE = np.load(PILOT / "fixtures/scenario/golden/frames/both.npy", allow_pickle=False).tobytes().hex()


class ProcessFailure(Exception):
    vlsp_no_commit = True  # Only a trusted failed Session response creates this marker.
    def __init__(self, error):
        self.error = deepcopy(error)
        super().__init__(error["message"])
    def to_dict(self):
        return deepcopy(self.error)


class Local:
    def __init__(self):
        self.session, self.lock, self.queues, self.futures = Session(), threading.Lock(), {}, {}
        assert self.session.handle({"id": 1, "op": "open"})["ok"]
    def open(self, policy, hold=True):
        key = (policy["source_id"], policy["run_id"])
        if key in self.queues and not self.queues[key]["queue"].snapshot()["stopped"]:
            raise ValueError("A live queue already exists for this source/run")
        gate, started = threading.Event(), threading.Event()
        if not hold: gate.set()
        def process(inputs, context):
            started.set()
            callback_started=time.monotonic_ns()
            if not gate.wait(30): raise TimeoutError("test gate timed out")
            cmd = deepcopy(inputs["command"]); cmd["op"] = "process"; cmd["context"] = context
            arrived=cmd.pop("_queue_ingress_ns")
            fail_after_commit=cmd.pop("_test_after_commit_exception",False)
            scenario_started=time.monotonic_ns()
            result = self.session.handle(cmd)
            ended=time.monotonic_ns()
            result["queue_timing"]={"worker_arrival_to_callback_ms":(callback_started-arrived)/1e6,
                                    "session_call_ms":(ended-scenario_started)/1e6,
                                    "test_gate_ms":(scenario_started-callback_started)/1e6}
            if not result["ok"]: raise ProcessFailure(result["error"])
            if fail_after_commit: raise RuntimeError("test-only lost reply after successful commit")
            return result
        queue = StreamQueue(policy, process, executor_lock=self.lock)
        self.queues[key] = {"queue": queue, "gate": gate, "started": started}
    def enqueue(self, cmd):
        key = (cmd["source_id"], cmd["run_id"])
        payload = deepcopy(cmd); payload.pop("source_id"); payload.pop("run_id")
        payload["_queue_ingress_ns"] = time.monotonic_ns()
        self.futures[cmd["id"]] = (key, self.queues[key]["queue"].submit({"command":payload}, cmd["context"]))
    def started(self, key): assert self.queues[key]["started"].wait(10)
    def release(self, key): self.queues[key]["gate"].set()
    def release_after(self, key, ms):
        timer = threading.Timer(ms/1000, self.queues[key]["gate"].set); timer.daemon=True; timer.start()
    def stop(self, key, timeout=0): return self.queues[key]["queue"].stop(timeout=timeout)
    def status(self, key):
        return {"stats": self.queues[key]["queue"].snapshot(),
                "completed": {str(k): f.result() for k,(owner,f) in self.futures.items() if owner == key and f.done()}}
    def snapshot(self): return self.session.handle({"id":0,"op":"snapshot"})
    def close(self):
        for row in self.queues.values(): row["gate"].set(); row["queue"].stop(timeout=10)
        self.session.shutdown()


class VideoPipe:
    def __init__(self):
        self.log = (PILOT / "independent/reports/stream-stderr.txt").open("w")
        self.child = subprocess.Popen([str(PILOT / "build/adapter/vlsp_videopipe"), "--python", sys.executable,
            "--worker", str(PILOT / "independent/streaming_worker.py")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1, start_new_session=True,
            env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1"})
        self.id = 0; self.frames = 0
        response = self.request({"op":"open"}); assert response["ok"]
    def request(self, cmd, *, allow_failure=False):
        self.id += 1
        cmd = {**cmd, "id": self.id}
        self.child.stdin.write(json.dumps(cmd, ensure_ascii=False, allow_nan=False)+"\n"); self.child.stdin.flush()
        if not select.select([self.child.stdout],[],[],40)[0]:
            os.killpg(self.child.pid, signal.SIGKILL); raise TimeoutError("VideoPipe RPC timeout; commit unknown")
        line = self.child.stdout.readline()
        if not line: raise RuntimeError("VideoPipe worker terminated")
        result = json.loads(line)
        if not result.get("ok") and not allow_failure: raise RuntimeError(str(result))
        t = result["transport"]
        assert t["engine"] == "VideoPipe" and t["source_seen"] == t["adapter_seen"] == t["sink_seen"] == self.id
        assert not result.get("independence", {}).get("loaded_reference_modules", [])
        if cmd["op"] == "enqueue":
            self.frames += 1
            assert t["meta_type"] == "FRAME" and t["frame_bytes"] == len(bytes.fromhex(cmd["image"]["bgr_hex"]))
            assert t["logical"]["frame_id"] == cmd["inputs"]["frame"]["frame_id"]
            h=14695981039346656037
            for byte in bytes.fromhex(cmd["image"]["bgr_hex"]): h=((h^byte)*1099511628211)&((1<<64)-1)
            assert t["frame_fnv1a64"] == f"{h:016x}"
        return result
    def open(self, policy, hold=True): self.request({"op":"queue_open","policy":policy,"test_hold":hold})
    def enqueue(self, cmd): return self.request({k:v for k,v in cmd.items() if k != "id"})["submitted"]
    def started(self, key): self.request({"op":"queue_wait_started","source_id":key[0],"run_id":key[1]})
    def release(self, key): self.request({"op":"queue_release","source_id":key[0],"run_id":key[1]})
    def release_after(self, key, ms): self.request({"op":"queue_release_after","source_id":key[0],"run_id":key[1],"delay_ms":ms})
    def stop(self, key, timeout=0): return self.request({"op":"queue_stop","source_id":key[0],"run_id":key[1],"timeout":timeout})["stats"]
    def status(self, key): return self.request({"op":"queue_status","source_id":key[0],"run_id":key[1]})
    def snapshot(self): return self.request({"op":"scenario_snapshot"})
    def close(self):
        try:
            self.request({"op":"shutdown"}); self.child.stdin.close()
            assert self.child.wait(timeout=15) == 0
        finally:
            if self.child.poll() is None:
                os.killpg(self.child.pid,signal.SIGKILL); self.child.wait(timeout=5)
            self.log.close()


def policy(source, mode, end, *, capacity=2, wait_ms=100):
    return {"format":"vlsp.stream-policy","version":"1.0.0","source_id":source,"run_id":"run-1",
            "max_queue_frames":capacity,"overload_policy":mode,"max_wait_ms":wait_ms,"on_end":end}


def frame(source, seq, ident, *, fault=None):
    f = {"frame_id":f"f{seq}","frame_ref":f"{source}/run-1/f{seq}","width":4,"height":2}
    c = {"source_id":source,"run_id":"run-1","sequence":seq,"event_time_ms":1700000000000+seq*1000}
    result = {"id":ident,"op":"enqueue","source_id":source,"run_id":"run-1",
              "inputs":{"frame":f},"context":c,"image":{"width":4,"height":2,"bgr_hex":IMAGE}}
    if fault is not None: result["fault"] = fault
    return result


def wait_done(side, key, count, *, timeout=40):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        row=side.status(key)
        if row["stats"]["stopped"] and len(row["completed"]) == count:
            return row
        time.sleep(.005)
    raise TimeoutError(f"queue failed to finish: {key}, last={row['stats']}")


def normalized(row, ids):
    output=[]
    for index, request_id in enumerate(ids, 1):
        result=row["completed"][str(request_id)]
        out={"offer":index,"status":result["status"]}
        if result["status"] in ("dropped","rejected"):
            out["reason"]=result["reason"]
        elif result["status"] == "error":
            if "error" in result:
                out["error"]={k:v for k,v in result["error"].items() if k != "message"}
            else:
                out.update(reason=result["reason"],commit=result["commit"])
        else:
            response=result["result"]
            if "independence" in response:
                assert response["independence"]["guard_active"]
                assert not response["independence"]["forbidden_import_attempts"]
                assert not response["independence"]["loaded_reference_modules"]
            trace=response["trace"]
            assert Counter(t["alias"] for t in trace)=={"detector":1,"tracker":1,"classifier_a":2,"classifier_b":2}
            digest=hashlib.sha256(bytes.fromhex(IMAGE)).hexdigest()
            assert all(t["frame_sha256"]==digest for t in trace if t["service"]=="model")
            out["result"]=response["result"]
            out["model_calls"]=[{"alias":t["alias"],"frame_id":t["request"]["frame_id"],
                                "frame_sha256":t.get("frame_sha256"),"actual_output":t["actual_output"]}
                               for t in response["trace"]]
        output.append(out)
    return output


def verify(stats, total, maximum):
    assert stats["stopped"] and stats["queued"] == stats["in_flight"] == 0
    assert stats["submitted"] == total == stats["committed"]+stats["dropped"]+stats["rejected"]+stats["errors"]
    assert stats["max_queue_depth"] <= maximum
    assert sum(stats["drop_reasons"].values()) == stats["dropped"]


def without_observed_peak(value):
    # Scheduling can change the measured peak while all outcomes agree.
    if isinstance(value, dict):
        return {k:without_observed_peak(v) for k,v in value.items() if k != "max_queue_depth"}
    if isinstance(value, list): return [without_observed_peak(v) for v in value]
    return value


def run_case(side, source, mode, end):
    key=(source,"run-1"); p=policy(source,mode,end)
    side.open(p,True); ids=[]
    for seq in (1,2,3,4):
        identifier=10000+seq
        returned=side.enqueue(frame(source,seq,identifier))
        ids.append(returned if isinstance(side,VideoPipe) else identifier)
        if seq==1: side.started(key)
    before=side.status(key)["stats"]
    assert before["max_queue_depth"] == 2 and before["in_flight"] == 1
    if end == "discard":
        stopping=side.stop(key,0); assert not stopping["stopped"]
    side.release(key)
    side.stop(key,0)
    row=wait_done(side,key,4); verify(row["stats"],4,2)
    assert side.stop(key,0) == row["stats"]
    values=normalized(row,ids)
    expected = (["committed","dropped","committed","committed"] if mode=="drop_oldest" else
                ["committed","committed","committed","dropped"] if mode=="drop_newest" else
                ["committed","committed","committed","rejected"])
    if end=="discard": expected=["committed"]+["rejected" if x=="rejected" else "dropped" for x in expected[1:]]
    assert [x["status"] for x in values] == expected, (mode,end,values)
    assert all(item.get("model_calls") for item in values if item["status"]=="committed")
    digest=hashlib.sha256(bytes.fromhex(IMAGE)).hexdigest()
    assert all(call["frame_sha256"] == digest for item in values if item["status"]=="committed"
               for call in item["model_calls"] if call["alias"] != "tracker")
    return {"policy":p,"stats":row["stats"],"outcomes":values}


def run_retry(side, source, *, release_before_deadline):
    key=(source,"run-1"); p=policy(source,"block","drain",wait_ms=500 if release_before_deadline else 60)
    side.open(p,True); ids=[]
    for seq in (1,2,3):
        identifier=30000+seq; got=side.enqueue(frame(source,seq,identifier)); ids.append(got if isinstance(side,VideoPipe) else identifier)
        if seq==1: side.started(key)
    if release_before_deadline: side.release_after(key,30)
    identifier=30004; got=side.enqueue(frame(source,4,identifier)); ids.append(got if isinstance(side,VideoPipe) else identifier)
    if not release_before_deadline:
        side.release(key)
        identifier=30005; got=side.enqueue(frame(source,4,identifier)); ids.append(got if isinstance(side,VideoPipe) else identifier)
    side.stop(key,0); row=wait_done(side,key,len(ids)); verify(row["stats"],len(ids),2)
    values=normalized(row,ids)
    assert values[3]["status"] == ("committed" if release_before_deadline else "rejected")
    if not release_before_deadline:
        assert values[3]["reason"] == "queue_timeout" and values[4]["status"] == "committed"
    return {"policy":p,"stats":row["stats"],"outcomes":values,
            "timing_note":"test gate only; no real-time upper bound is claimed"}


def run_isolation(side, source):
    a=(source+"-a","run-1"); b=(source+"-b","run-1")
    side.open(policy(a[0],"drop_newest","discard"),True)
    side.open(policy(b[0],"drop_newest","drain"),False)
    ids_a=[]; ids_b=[]
    for seq in (1,2,3):
        ident=40000+seq; got=side.enqueue(frame(a[0],seq,ident)); ids_a.append(got if isinstance(side,VideoPipe) else ident)
        if seq==1: side.started(a)
    for seq in (1,2):
        ident=50000+seq; got=side.enqueue(frame(b[0],seq,ident)); ids_b.append(got if isinstance(side,VideoPipe) else ident)
    side.stop(a,0); side.stop(b,0); side.release(a)
    ra,rb=wait_done(side,a,3),wait_done(side,b,2)
    verify(ra["stats"],3,2);verify(rb["stats"],2,2)
    aa,bb=normalized(ra,ids_a),normalized(rb,ids_b)
    assert [x["status"] for x in aa]==["committed","dropped","dropped"]
    assert [x["status"] for x in bb]==["committed","committed"]
    assert bb[1]["result"]["events"][0]["source_id"]==b[0]
    return {"a": {"stats":ra["stats"],"outcomes":aa},"b":{"stats":rb["stats"],"outcomes":bb}}


def run_failure(side, source):
    key=(source,"run-1"); side.open(policy(source,"block","drain",wait_ms=1000),False)
    fault={"alias":"classifier_b","mode":"exception","call":2}
    ident=60001; got=side.enqueue(frame(source,1,ident,fault=fault)); ids=[got if isinstance(side,VideoPipe) else ident]
    side.stop(key,0); row=wait_done(side,key,1); verify(row["stats"],1,2)
    values=normalized(row,ids)
    assert values[0]["status"]=="error" and values[0]["error"]["code"]=="service_failed"
    snapshot=side.snapshot()
    assert source not in snapshot["snapshot"]["state"] and source not in snapshot["snapshot"]["order"]
    assert all(event["source_id"]!=source for event in snapshot["published_events"])
    return {"stats":row["stats"],"outcomes":values,"no_state_or_events":True}


def run_lost_reply(side, source):
    """Demonstrate a callback failing after a real scenario commit."""
    key=(source,"run-1"); side.open(policy(source,"block","drain",wait_ms=1000),False)
    command=frame(source,1,61001); command["_test_after_commit_exception"]=True
    got=side.enqueue(command); ident=got if isinstance(side,VideoPipe) else 61001
    side.stop(key,0); row=wait_done(side,key,1); verify(row["stats"],1,2)
    outcome=normalized(row,[ident])[0]
    assert outcome["status"]=="error" and outcome["reason"]=="callback_outcome_unknown" and outcome["commit"]=="unknown"
    snapshot=side.snapshot()
    assert snapshot["snapshot"]["order"][source]["run-1"][0]==1
    return {"stats":row["stats"],"outcome":outcome,"actual_commit_observed_in_test":True}


def run_restart(side, source):
    key=(source,"run-1"); p=policy(source,"block","drain",wait_ms=1000)
    side.open(p,False)
    ident=side.enqueue(frame(source,1,62001)); ids=[ident if isinstance(side,VideoPipe) else 62001]
    side.stop(key,0); first=wait_done(side,key,1); verify(first["stats"],1,2)
    side.open(p,False)
    for seq, ident in ((1,62002),(2,62003)):
        got=side.enqueue(frame(source,seq,ident)); ids.append(got if isinstance(side,VideoPipe) else ident)
    side.stop(key,0); second=wait_done(side,key,3); verify(second["stats"],2,2)
    outcomes=normalized(second,ids)
    assert outcomes[0]["status"]=="committed"
    assert outcomes[1]["status"]=="error" and outcomes[1]["error"]["code"]=="input_order"
    assert outcomes[2]["status"]=="committed" and outcomes[2]["result"]["events"][0]["payload"]["count"]==2
    return {"first_stats":first["stats"],"restart_stats":second["stats"],"outcomes":outcomes}


def check_native_close_while_processing(side):
    """Closing starts the policy but cannot destroy an in-flight scenario."""
    source="camera-close-inflight"; key=(source,"run-1")
    side.open(policy(source,"block","drain"),True)
    ident=side.enqueue(frame(source,1,80001)); side.started(key)
    refused=side.request({"op":"close"},allow_failure=True)
    assert not refused["ok"] and refused["error"]["code"]=="queue_still_draining"
    side.release(key); row=wait_done(side,key,1); verify(row["stats"],1,2)
    assert normalized(row,[ident])[0]["status"]=="committed"
    assert side.request({"op":"close"})["ok"]
    assert side.request({"op":"open"})["ok"]
    return {"close_while_inflight":"rejected_then_succeeded_after_drain", "inflight_frame_committed":True}


def run_burst(side, source, count=300):
    key=(source,"run-1"); p=policy(source,"drop_oldest","drain",capacity=4,wait_ms=1000)
    side.open(p,False); ids=[]; start=time.monotonic()
    for seq in range(1,count+1):
        identifier=20000+seq; returned=side.enqueue(frame(source,seq,identifier))
        ids.append(returned if isinstance(side,VideoPipe) else identifier)
    side.stop(key,0); row=wait_done(side,key,count); elapsed=time.monotonic()-start
    verify(row["stats"],count,4)
    values=normalized(row,ids)
    assert row["stats"]["committed"] > 0
    committed=[x["result"]["state"] for x in values if x["status"]=="committed"]
    assert len(committed)==row["stats"]["committed"]
    return {"policy":p,"stats":row["stats"],"elapsed_seconds":elapsed,
            "end_to_end_offered_average_fps":count/elapsed,"scope":"synthetic 4x2 frame; queue and IPC, not an industrial benchmark"}


def run_sustained(side, source, count=120):
    key=(source,"run-1"); p=policy(source,"block","drain",capacity=4,wait_ms=5000)
    side.open(p,False); ids=[]; start=time.monotonic()
    for seq in range(1,count+1):
        identifier=70000+seq; returned=side.enqueue(frame(source,seq,identifier))
        ids.append(returned if isinstance(side,VideoPipe) else identifier)
    side.stop(key,0); row=wait_done(side,key,count); elapsed=time.monotonic()-start
    verify(row["stats"],count,4)
    values=normalized(row,ids)
    assert all(item["status"]=="committed" for item in values)
    assert row["stats"]["dropped"]==row["stats"]["rejected"]==row["stats"]["errors"]==0
    assert sum(len(item["result"]["events"]) for item in values)==1
    timings=[row["completed"][str(identifier)]["result"]["queue_timing"] for identifier in ids]
    def percentiles(name):
        points=np.percentile([item[name] for item in timings],[50,95,99])
        return {"p50":float(points[0]),"p95":float(points[1]),"p99":float(points[2])}
    return {"policy":p,"stats":row["stats"],"elapsed_seconds":elapsed,
            "committed_frames_per_second":count/elapsed,
            "latency_ms":{"worker_arrival_to_callback":percentiles("worker_arrival_to_callback_ms"),
                          "session_call":percentiles("session_call_ms")},
            "results":[item["result"] for item in values],
            "scope":"synthetic 4x2 CPU frames, includes queue/IPC; not an industrial benchmark"}


def main():
    pins=verify_fixtures(); binary=PILOT/"build/adapter/vlsp_videopipe"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.unlink(missing_ok=True)
    binary_hash=hashlib.sha256(binary.read_bytes()).hexdigest()
    before=hashlib.sha256((PILOT/"fixtures/scenario.zip").read_bytes()).hexdigest()
    sources = source_paths() + sorted((PILOT/"independent").rglob("*.py")) + [PILOT/"stream_load_test.py", PILOT/"cpp/adapter.cpp"]
    source_hashes={str(p.relative_to(bootstrap.WORKSPACE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    assert hashlib.sha256((bootstrap.WORKSPACE/"drafts/v0.2/scenario/examples/vlsp-color-cascade.zip").read_bytes()).hexdigest()==before
    local, native = Local(), VideoPipe()
    cases=[]
    try:
        for mode in ("block","drop_oldest","drop_newest"):
            for end in ("drain","discard"):
                source=f"camera-{mode}-{end}"
                a=run_case(local,source,mode,end); b=run_case(native,source,mode,end)
                assert a==b,(mode,end,a,b)
                cases.append({"name":f"{mode}-{end}","reference":a,"videopipe":b,"matched":True})
                print(json.dumps({"case":f"{mode}-{end}","status":"pass"}),flush=True)
        for release_before in (False,True):
            source="camera-retry-"+("release" if release_before else "timeout")
            a=run_retry(local,source,release_before_deadline=release_before)
            b=run_retry(native,source,release_before_deadline=release_before)
            assert a==b
            cases.append({"name":source,"reference":a,"videopipe":b,"matched":True})
        a=run_isolation(local,"camera-isolation");b=run_isolation(native,"camera-isolation")
        assert without_observed_peak(a)==without_observed_peak(b), (a,b)
        cases.append({"name":"multi-source-stop-isolation","reference":a,"videopipe":b,"matched":True})
        a=run_restart(local,"camera-restart");b=run_restart(native,"camera-restart")
        assert without_observed_peak(a)==without_observed_peak(b), (a,b)
        cases.append({"name":"same-source-queue-restart-preserves-host-state","reference":a,"videopipe":b,"matched":True})
        close_evidence=check_native_close_while_processing(native)
        bursts={"reference":run_burst(local,"camera-burst-reference"),
                "videopipe":run_burst(native,"camera-burst-videopipe")}
        sustained={"reference":run_sustained(local,"camera-sustained"),
                   "videopipe":run_sustained(native,"camera-sustained")}
        assert sustained["reference"]["results"] == sustained["videopipe"]["results"]
        a=run_lost_reply(local,"camera-lost-reply");b=run_lost_reply(native,"camera-lost-reply")
        assert a==b
        cases.append({"name":"callback-lost-after-commit","reference":a,"videopipe":b,"matched":True})
        a=run_failure(local,"camera-failure");b=run_failure(native,"camera-failure")
        assert a==b
        cases.append({"name":"service-failure-no-commit","reference":a,"videopipe":b,"matched":True})
        native_count=native.frames
    finally:
        local.close(); native.close()
    assert hashlib.sha256((PILOT/"fixtures/scenario.zip").read_bytes()).hexdigest()==before==pins["scenario_zip_sha256"]
    assert hashlib.sha256(binary.read_bytes()).hexdigest()==binary_hash
    assert {str(p.relative_to(bootstrap.WORKSPACE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}==source_hashes
    assert hashlib.sha256((bootstrap.WORKSPACE/"drafts/v0.2/scenario/examples/vlsp-color-cascade.zip").read_bytes()).hexdigest()==before
    verify_fixtures()
    report={"status":"pass","checked_at":datetime.now(timezone.utc).isoformat(),
            "scenario_zip_sha256":before,"native_binary_sha256":binary_hash,"environment":platform.platform(),
            "source_sha256":source_hashes,
            "cases":cases,"bursts":bursts,"sustained":sustained,"native_close_during_inflight":close_evidence,
            "native_enqueue_frames":native_count,
            "limitations":["Queue applies inside Python worker before scenario execution; upstream VideoPipe queues remain unbounded",
                "Test gates introduce artificial delay only for deterministic overload cases",
                "Synthetic 4x2 frames and untrained CPU models; results are not industrial throughput",
                "Single shared scenario executor is serialized; waiting producer memory is not bounded by queue capacity",
                "Generic callback or IPC failure can follow commit; absence of a reply does not prove rollback",
                "No RTSP, GPU, durable delivery, exactly-once or production sandbox tested"]}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    print(json.dumps({"status":"pass","cases":len(cases),"burst_frames_per_host":300,
                      "native_frames":native_count,"report":str(REPORT)}))


if __name__ == "__main__": main()
