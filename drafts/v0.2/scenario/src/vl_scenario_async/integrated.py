"""SQLite-backed async-capable scenario execution and transactional outbox.

This is an experimental single-node reference, not a sandbox or a production
distributed store. The trusted host owns media snapshots and model permissions.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from time import perf_counter

from jsonschema import Draft202012Validator
from vl_scenario_v02 import ScenarioError
from vl_scenario_v02.common import json_copy, read_json, same_json
from vl_scenario_v02.contracts import SCHEMAS, materialize_events, validate_input, validate_value
from vl_scenario_v02.executor import ScenarioExecutor, _contains_frame_ref
from vl_scenario_v02.services import CallServices, _box, _sticky
from vl_scenario_v02.validation import validate_package, _self_contained_schema

from .store import AsyncStore, QueueError, _job, _json, _sha

EXTENSION = "org.vlexchange.async-mllm.v1"
HARDWARE_EXTENSION = "org.vlexchange.hardware-async-models.v1"
MAX_STAGED_JOBS_PER_CALL = 8  # Explicit reference-host limit; policy is negotiated separately.


@dataclass(frozen=True)
class AsyncModelBinding:
    model_id: str
    revision: str
    model_digest: str | None
    input_types: tuple[str, ...]
    output_types: tuple[str, ...]
    max_input_bytes: int
    max_output_bytes: int


class _AsyncServices(CallServices):
    def __init__(self, host, manifest, frame, context, store, models, snapshot_media, discard_media):
        super().__init__(host, manifest, frame, context)
        self._store, self._async_models = store, models
        self._snapshot_media, self._discard_media = snapshot_media, discard_media
        self.staged, self.media_to_discard = [], []

    @_sticky
    def submit_mllm(self, request):
        self._check()
        if len(self.staged) >= MAX_STAGED_JOBS_PER_CALL:
            raise ScenarioError("service_contract", "Reference limit of 8 MLLM jobs per call exceeded", fatal=True)
        request = json_copy(request, code="service_contract", max_bytes=self._limits["max_input_bytes"])
        fields = {"model_alias", "frame_id", "frame_ref", "bbox", "tracker_alias", "track_id", "payload", "deadline_ms"}
        if type(request) is not dict or set(request) != fields:
            raise ScenarioError("service_contract", "Invalid MLLM request fields", fatal=True)
        alias = request["model_alias"]
        if type(alias) is not str or alias not in self._async_models:
            raise ScenarioError("service_contract", "Undeclared async model", fatal=True)
        model = self._async_models[alias]
        if request["frame_id"] != self._frame["frame_id"]:
            raise ScenarioError("service_contract", "MLLM request frame differs from current frame", fatal=True)
        ref = request["frame_ref"]
        if ref is not None and (ref != self._frame["frame_ref"] or "image" not in model.input_types):
            raise ScenarioError("service_contract", "Frame reference not authorized for this model", fatal=True)
        track, tracker, bbox = request["track_id"], request["tracker_alias"], request["bbox"]
        if (track is None) != (tracker is None) or (track is not None and (tracker, track) not in self._active_tracks):
            raise ScenarioError("service_contract", "Track was not produced by this frame and tracker", fatal=True)
        if bbox is not None:
            crop = _box(bbox)
            bound = self._active_tracks.get((tracker, track))
            if ref is None or bound is None or crop[0] < bound[0] or crop[1] < bound[1] or crop[2] > bound[2] or crop[3] > bound[3]:
                raise ScenarioError("service_contract", "MLLM crop must be within the current tracked object", fatal=True)
        payload = request["payload"]
        if type(payload) is not dict or set(payload) != {"text", "response_schema", "parameters"}:
            raise ScenarioError("service_contract", "Invalid MLLM payload", fatal=True)
        if payload["text"] is not None and (type(payload["text"]) is not str or "text" not in model.input_types):
            raise ScenarioError("service_contract", "Text is not supported by this model", fatal=True)
        if ref is None and not payload["text"]:
            raise ScenarioError("service_contract", "Text or an image is required", fatal=True)
        if type(payload["parameters"]) is not dict or len(_json(payload)) > model.max_input_bytes:
            raise ScenarioError("service_contract", "MLLM request exceeds model contract", fatal=True)
        if payload["response_schema"] is not None:
            if "structured" not in model.output_types or type(payload["response_schema"]) is not dict:
                raise ScenarioError("service_contract", "Structured output is not supported", fatal=True)
            _self_contained_schema(payload["response_schema"])
            try:
                Draft202012Validator.check_schema(payload["response_schema"])
            except Exception as exc:
                raise ScenarioError("service_contract", "Invalid structured result schema", fatal=True) from exc
        deadline = request["deadline_ms"]
        if type(deadline) is not int or deadline <= self._store._now():
            raise ScenarioError("service_contract", "MLLM deadline is expired", fatal=True)
        media = None
        if ref is not None:
            if not callable(self._snapshot_media) or not callable(self._discard_media):
                raise ScenarioError("dependency_missing", "A trusted media snapshot and cleanup service is required", fatal=True)
            media = self._snapshot_media(json_copy(self._frame, code="input_contract"),
                                         json_copy(self._context, code="input_contract"), bbox, deadline)
            self.media_to_discard.append(media)
        data = {"frame_id": request["frame_id"], "track_id": track, "tracker_alias": tracker,
                "source_event_time_ms": self._context["event_time_ms"], "model_alias": alias,
                "model_revision": model.revision, "model_digest": model.model_digest,
                "payload": payload, "media": media, "deadline_ms": deadline}
        if self._store._hardware is not None and alias in self._store._hardware[2]:
            data["hardware_binding"] = self._store._hardware[2][alias]
        job = _job(json.loads(self._store._namespace), self._context["source_id"], self._context["run_id"], data)
        self._store._model_guard(job)
        if any(previous["job_id"] == job["job_id"] for previous in self.staged):
            raise ScenarioError("service_contract", "Duplicate MLLM job in one call", fatal=True)
        self.staged.append(job)
        return {"job_id": job["job_id"], "status": "staged"}

    def discard_staged_media(self):
        failures = []
        for media in self.media_to_discard:
            try:
                self._discard_media(media)
            except Exception as exc:
                failures.append(type(exc).__name__)
        self.media_to_discard.clear()
        if failures:
            raise ScenarioError("service_failed", "Host failed to discard staged media after rollback", fatal=True)


class _CompletionServices:
    def __init__(self):
        self._failure = None
        self._failure_fields = None

    def __getattr__(self, _name):
        message = "This reference does not start services from a completion callback"
        if self._failure_fields is None:
            self._failure_fields = ("service_contract", message, "execution")
            self._failure = ScenarioError("service_contract", message, fatal=True)
        raise ScenarioError("service_contract", message, fatal=True)


def _invoke_scenario(scenario, inputs, context, state, services):
    """Host-owned sticky failure and lifecycle policy for frame/completion calls."""
    try:
        result = scenario.process(inputs, context, state, services)
    except ScenarioError as exc:
        fields = services._failure_fields
        if fields is not None:
            code, message, stage = fields
            if code == "authorization_failed":
                raise ScenarioError(code, message, stage="security", fatal=False, retryable=True) from exc
            raise ScenarioError(code, message, stage=stage, fatal=True, retryable=False) from exc
        raise ScenarioError(exc.code, exc.message, stage=exc.stage, fatal=True, retryable=False) from exc
    except BaseException as exc:
        raise ScenarioError("scenario_exception", f"Scenario process failed: {type(exc).__name__}: {exc}", fatal=True) from exc
    if services._failure is not None:
        fields = services._failure_fields
        if fields is not None and fields[0] == "authorization_failed":
            raise ScenarioError(fields[0], fields[1], stage="security", fatal=False, retryable=True) from services._failure
        raise ScenarioError("service_failed", "A service failed; call cannot commit", fatal=True) from services._failure
    return result


class AsyncScenarioExecutor(ScenarioExecutor):
    """Async profile; state, order, jobs and event outbox share one SQLite DB."""
    _supports_async = True
    _supports_hardware_async = True

    def __init__(self, root, *, policy="protected", parameters=None, host=None, async_models=None,
                 store=None, snapshot_media=None, discard_media=None, hardware_adapter=None, hardware_authorities=None, sync_hardware=None):
        if policy != "development":
            raise ScenarioError("policy_rejected", "Protected delivery requires open_protected")
        self._setup(root, parameters, host, async_models, store, snapshot_media, discard_media, None, None, hardware_adapter, hardware_authorities, sync_hardware)

    @classmethod
    @contextmanager
    def open_protected(cls, envelope_path, *, verifier, host, async_models, store, installation_signing_key,
                       parameters=None, snapshot_media=None, discard_media=None, hardware_adapter=None, hardware_authorities=None, sync_hardware=None):
        if not callable(verifier):
            raise ScenarioError("policy_rejected", "A trusted protected package verifier is required")
        with verifier(Path(envelope_path)) as authorized:
            instance = cls.__new__(cls)
            instance._setup(authorized.root, parameters, host, async_models, store, snapshot_media,
                            discard_media, authorized, installation_signing_key, hardware_adapter, hardware_authorities, sync_hardware)
            try:
                yield instance
            finally:
                instance.close()

    def _setup(self, root, parameters, host, async_models, store, snapshot_media, discard_media, authorized, signer, hardware_adapter, hardware_authorities, sync_hardware):
        if not isinstance(store, AsyncStore) or type(async_models) is not dict:
            raise ScenarioError("dependency_missing", "AsyncStore and trusted model bindings are required")
        package = validate_package(root, parameters=parameters, host=None if sync_hardware else host, allow_async=True, allow_hardware_async=hardware_adapter is not None)
        if authorized is not None:
            try:
                authorized.assert_current()
                claims = authorized.claims
                if (claims.get('package_format') != 'vl.scenario-package' or claims.get('manifest_path') != 'scenario-package.json'
                        or claims.get('manifest_sha256') != package.manifest_sha256):
                    raise ValueError('Scenario license does not cover this exact manifest')
            except Exception as exc:
                raise ScenarioError('policy_rejected', 'A current license for this exact scenario is required before model binding') from exc
        namespace = json.loads(store._namespace)
        if (namespace["scenario_id"] != package.manifest["scenario_id"] or namespace["scenario_version"] != package.manifest["version"]
                or namespace["scenario_manifest_sha256"] != package.manifest_sha256):
            raise ScenarioError("dependency_mismatch", "Store namespace is not bound to this scenario")
        declared = package.manifest["extensions"][EXTENSION]["data"]["models"]
        bindings = dict(async_models)
        for model in declared:
            actual = bindings.get(model["alias"])
            expected = AsyncModelBinding(model["model_id"], model["revision"], model["model_digest"],
                tuple(model["input_types"]), tuple(model["output_types"]), model["max_input_bytes"], model["max_output_bytes"])
            if actual != expected:
                raise ScenarioError("dependency_mismatch", "MLLM binding differs from required declaration")
        if set(bindings) != {m["alias"] for m in declared}:
            raise ScenarioError("dependency_mismatch", "Unknown MLLM binding")
        hardware_extension = package.manifest["extensions"].get(HARDWARE_EXTENSION)
        if hardware_extension is not None:
            from vl_scenario_v02.hardware import HardwareAdapter
            if not isinstance(hardware_adapter, HardwareAdapter) or type(hardware_authorities) is not dict:
                raise ScenarioError("dependency_missing", "Async hardware requires trusted selection and model authorities")
            scenario_bytes = (package.root / "scenario-package.json").read_bytes()
            if hashlib.sha256(scenario_bytes).hexdigest() != package.manifest_sha256:
                raise ScenarioError("dependency_mismatch", "Scenario manifest changed after validation")
            selected = hardware_adapter.prepare(scenario_bytes)
            expected_roles = {x["alias"] for x in hardware_extension["data"]["models"]}
            if set(selected) != expected_roles or set(hardware_authorities) != expected_roles:
                raise ScenarioError("dependency_mismatch", "Selected async hardware roles differ from scenario")
            store.bind_hardware(hardware_adapter, scenario_bytes, selected, hardware_authorities)
        elif hardware_adapter is not None or hardware_authorities is not None:
            raise ScenarioError("dependency_mismatch", "Scenario does not declare hardware-bound async models")
        store._model_contracts = bindings
        if authorized is not None:
            store.bind_security(authorized, signer)
        else:
            with store._db() as db:
                store._require_security(db)
        self._store, self._async_models = store, bindings
        self._snapshot_media, self._discard_media = snapshot_media, discard_media
        self._initialize(root, parameters=parameters, host=host, authorized=authorized, hardware=sync_hardware)

    def _read_state(self, source, run):
        revision, envelope = self._store.state(source, run)
        if revision == 0:
            envelope = {"state": self.manifest["state"]["initial"], "order": None}
        self._validate_envelope(envelope)
        return revision, envelope

    def _validate_envelope(self, envelope):
        if type(envelope) is not dict or set(envelope) != {"state", "order"}:
            raise ScenarioError("state_contract", "Invalid persisted scenario envelope", fatal=True)
        if (envelope["order"] is not None and
            (type(envelope["order"]) is not list or len(envelope["order"]) != 2 or
             any(type(n) is not int or n < 0 for n in envelope["order"]))):
            raise ScenarioError("state_contract", "Invalid persisted ordering state", fatal=True)
        validate_value(envelope["state"], self.manifest["state"]["schema"],
                       code="state_contract", max_bytes=self.manifest["limits"]["max_state_bytes"])

    @staticmethod
    def _order_check(context, previous):
        if previous is not None and (context["sequence"] <= previous[0] or context["event_time_ms"] < previous[1]):
            raise ScenarioError("input_order", "Sequence must increase and event time must not decrease")

    def _result(self, raw, original_inputs, context, limits):
        if type(raw) is not dict or set(raw) != {"events", "state"}:
            raise ScenarioError("event_contract", "Scenario must return events and state", fatal=True)
        raw = json_copy(raw, code="event_contract", max_bytes=limits["max_state_bytes"] + limits["max_event_bytes"] * limits["max_events_per_call"] + 128)
        if "frame" in original_inputs and _contains_frame_ref(raw, original_inputs["frame"]["frame_ref"]):
            raise ScenarioError("event_contract", "Ephemeral frame_ref may not escape", fatal=True)
        return (materialize_events(raw["events"], self.manifest, context),
                validate_value(raw["state"], self.manifest["state"]["schema"], code="state_contract", max_bytes=limits["max_state_bytes"]))

    @staticmethod
    def _events(db, source, run, events):
        for event in events:
            raw = _json(event)
            prior = db.execute("SELECT event_json FROM vlsp_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if prior is not None and prior[0] != raw:
                raise ScenarioError("event_contract", "Existing event identity conflicts with new payload", fatal=True)
            if prior is None:
                db.execute("INSERT INTO vlsp_events VALUES(?,?,?,?)", (event["event_id"], source, run, raw))

    def process(self, inputs, context):
        self._healthy()
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Concurrent calls on one executor are not supported")
        services = None
        committed = False
        hardware_token = None
        try:
            self._healthy()
            manifest, limits = self.manifest, self.manifest["limits"]
            context = validate_value(context, read_json(SCHEMAS / "context.schema.json"), code="input_contract", max_bytes=limits["max_input_bytes"])
            inputs = validate_input(inputs, manifest)
            original, original_context = json_copy(inputs, code="input_contract"), json_copy(context, code="input_contract")
            source, run = context["source_id"], context["run_id"]
            lease = self._authorize(before_process=True)
            if self._hardware is not None:
                hardware_token = self._hardware.begin_call()
            revision, stored = self._read_state(source, run)
            self._order_check(context, stored["order"])
            frame_id = inputs["frame"]["frame_id"]
            with self._store._db() as db:
                self._store._require_security(db)
                if db.execute("SELECT 1 FROM vlsp_frames WHERE source_id=? AND run_id=? AND frame_id=?", (source,run,frame_id)).fetchone():
                    raise ScenarioError("input_order", "Duplicate frame ID")
            services = _AsyncServices(self._host, manifest, original["frame"], original_context, self._store,
                                      self._async_models, self._snapshot_media, self._discard_media)
            start = perf_counter()
            try:
                raw = _invoke_scenario(self._scenario, inputs, context, json_copy(stored["state"], code="state_contract"), services)
            finally:
                services.close()
            if (perf_counter() - start) * 1000 > limits["max_process_ms"]:
                raise ScenarioError("process_timeout", "Scenario call exceeded deadline", fatal=True)
            if not same_json(inputs, original) or not same_json(context, original_context):
                raise ScenarioError("input_mutation", "Scenario changed inputs or context", fatal=True)
            events, state = self._result(raw, original, original_context, limits)
            next_envelope = {"state": state, "order": [context["sequence"], context["event_time_ms"]]}
            # Capture may renew online grants; it must happen without a SQLite writer lock.
            job_leases = {job["job_id"]: self._store._capture(f"vlsp-enqueue:{job['job_id']}")
                          for job in services.staged}
            with self._store._db() as db:
                self._store._begin(db); self._store._require_security(db)
                previous = db.execute("SELECT revision FROM states WHERE source_id=? AND run_id=?", (source, run)).fetchone()
                if (previous["revision"] if previous else 0) != revision:
                    raise QueueError("revision_conflict", "Scenario state changed during execution")
                if lease is not None:
                    lease.assert_current()
                state_raw = _json(next_envelope)
                db.execute("INSERT INTO states VALUES(?,?,?,?) ON CONFLICT(source_id,run_id) DO UPDATE SET revision=excluded.revision,state_json=excluded.state_json", (source,run,revision+1,state_raw))
                db.execute("INSERT INTO vlsp_frames VALUES(?,?,?)", (source,run,frame_id))
                for job in services.staged:
                    self._store._model_guard(job)
                    expiry = job["media"]["expires_at_ms"] if job["media"] else job["identity"]["deadline_ms"]
                    if self._store._now() >= min(expiry, job["identity"]["deadline_ms"]):
                        raise QueueError("expired", "Job or media expired before commit")
                    self._store._authorize("enqueue", job)
                    binding = f"vlsp-enqueue:{job['job_id']}"
                    snapshot = job_leases[job["job_id"]]
                    if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone():
                        raise QueueError("idempotency_conflict", "A job already exists for this request")
                    db.execute("INSERT INTO jobs(job_id,identity_json,job_json,status,source_id,run_id,deadline,media_expires_at,expected_revision,committed_state_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (job["job_id"],_json(job["identity"]),_json(job),"pending",source,run,job["identity"]["deadline_ms"],expiry,revision,_sha(b"VLSP-ASYNC-STATE-V1\0",state_raw)))
                    if snapshot is not None:
                        db.execute("INSERT INTO vlsp_enqueue_leases VALUES(?,?)", (job["job_id"], _json(snapshot)))
                        self._store._verify(snapshot,binding)
                    self._store._authorize("enqueue_commit", job)
                self._events(db,source,run,events)
                if lease is not None: lease.assert_current()
                if self._hardware is not None: self._hardware.assert_current()
                for job in services.staged:
                    self._store._model_guard(job)
                    if self._store._now() >= min(job["identity"]["deadline_ms"], job["media"]["expires_at_ms"] if job["media"] else job["identity"]["deadline_ms"]):
                        raise QueueError("expired", "Job or media expired during commit")
                    if job_leases[job["job_id"]] is not None:
                        self._store._verify(job_leases[job["job_id"]], f"vlsp-enqueue:{job['job_id']}")
                db.commit(); committed = True
            return {"events":events,"state":state,"job_ids":[job["job_id"] for job in services.staged]}
        except ScenarioError as exc:
            if exc.fatal:
                self._failed = True
            raise
        except QueueError:
            raise
        except Exception as exc:
            self._failed = True
            raise ScenarioError("scenario_exception", f"Async scenario failed: {type(exc).__name__}: {exc}", fatal=True) from exc
        finally:
            try:
                if services is not None and not committed:
                    services.discard_staged_media()
            finally:
                try:
                    if hardware_token is not None:
                        self._hardware.end_call(hardware_token)
                finally:
                    self._busy.release()

    def process_completion(self, job_id, context):
        """Atomically apply a verified terminal result and mark it consumed."""
        self._healthy()
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Concurrent calls on one executor are not supported")
        hardware_token = None
        try:
            limits = self.manifest["limits"]
            context = validate_value(context, read_json(SCHEMAS / "context.schema.json"), code="input_contract", max_bytes=limits["max_input_bytes"])
            source, run = context["source_id"], context["run_id"]
            lease = self._authorize(before_process=True)
            if self._hardware is not None:
                hardware_token = self._hardware.begin_call()
            with self._store._db() as db:
                self._store._require_security(db)
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None: raise QueueError("unknown_job", "Unknown job")
                self._store._verify_enqueue(db, job_id)
                job = self._store._validated_row(row); self._store._authorize("apply", job)
                self._store._model_guard(job)
                original_attempt = self._store._verify_hardware_delivery(db, row, job)
                if row["source_id"] != source or row["run_id"] != run:
                    raise QueueError("correlation_mismatch", "Completion belongs to another source or run")
                if row["status"] == "delivered":
                    return {"duplicate":True,"events":[]}
                if row["status"] not in ("completed", "failed", "timed_out", "cancelled"):
                    raise QueueError("invalid_status", "Completion is not terminal")
                state_row = db.execute("SELECT revision,state_json FROM states WHERE source_id=? AND run_id=?",(source,run)).fetchone()
                if state_row is None: raise QueueError("invalid_state", "Scenario state is missing")
                envelope = json.loads(state_row["state_json"]); self._validate_envelope(envelope)
                self._order_check(context, envelope["order"])
                identity = job["identity"]
                expected_status, expected_result = row["status"], row["result_json"]
                expected_revision = state_row["revision"]
                if expected_status == "completed" and (expected_result is None or
                        _sha(b"VLSP-ASYNC-RESULT-V1\0", expected_result) != row["result_hash"]):
                    raise QueueError("correlation_mismatch", "Stored result digest differs")
                keys = ("namespace", "source_id", "run_id", "frame_id", "tracker_alias", "track_id", "source_event_time_ms", "model_alias", "model_revision")
                if row["status"] == "completed":
                    result = json.loads(expected_result)
                    self._store._model_guard(job, result)
                    response_schema = job["payload"]["response_schema"]
                    if response_schema is not None and (result.get("structured") is None or list(Draft202012Validator(response_schema).iter_errors(result["structured"]))):
                        raise QueueError("invalid_result", "Stored response does not match its declared schema")
                    try:
                        if self._store.validate_result(json_copy(result, code="input_contract"), json_copy(job, code="input_contract")) is False:
                            raise ValueError("Rejected by host result policy")
                    except Exception as exc:
                        raise QueueError("invalid_result", "Host rejected stored provider result") from exc
                    fields = {"status":"completed", "text":result["text"], "structured":result["structured"], "error":None}
                else:
                    fields = {"status":row["status"], "text":None, "structured":None, "error":{"code":row["status"]}}
            completion = {**{k:identity[k] for k in keys}, "job_id":job_id, **fields}
            schema = read_json(SCHEMAS / "async-mllm.schema.json")
            validate_value(completion, {"$defs":schema["$defs"],"$ref":"#/$defs/completion"}, code="input_contract", max_bytes=limits["max_input_bytes"])
            inputs = {"mllm_result":completion}; original = json_copy(inputs, code="input_contract")
            original_context = json_copy(context, code="input_contract")
            start = perf_counter()
            # The scenario runs without a SQLite writer lock. A concurrent frame
            # produces revision_conflict and the caller may retry on new state.
            completion_services = _CompletionServices()
            raw = _invoke_scenario(self._scenario, inputs, context, json_copy(envelope["state"], code="state_contract"), completion_services)
            if (perf_counter()-start)*1000 > limits["max_process_ms"]:
                raise ScenarioError("process_timeout", "Completion processing exceeded deadline", fatal=True)
            if not same_json(inputs,original) or not same_json(context,original_context):
                raise ScenarioError("input_mutation", "Scenario changed completion input or context", fatal=True)
            events, state = self._result(raw, original, original_context, limits)
            envelope["state"], envelope["order"] = state, [context["sequence"], context["event_time_ms"]]
            with self._store._db() as db:
                self._store._begin(db); self._store._require_security(db)
                current = db.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
                if current is None: raise QueueError("unknown_job", "Unknown job")
                self._store._verify_enqueue(db,job_id)
                if current["status"] == "delivered":
                    db.commit(); return {"duplicate":True,"events":[]}
                if (current["status"] != expected_status or current["result_json"] != expected_result
                    or self._store._validated_row(current) != job):
                    raise QueueError("correlation_mismatch", "Completion changed during scenario processing")
                if self._store._verify_hardware_delivery(db, current, job) != original_attempt:
                    raise QueueError("correlation_mismatch", "Original attempt authorization changed during delivery")
                current_state = db.execute("SELECT revision FROM states WHERE source_id=? AND run_id=?",(source,run)).fetchone()
                if current_state is None or current_state["revision"] != expected_revision:
                    raise QueueError("revision_conflict", "Scenario state changed; retry completion")
                self._events(db,source,run,events)
                self._store._authorize("apply_commit",job)
                self._store._model_guard(job)
                if lease is not None: lease.assert_current()
                if self._hardware is not None: self._hardware.assert_current()
                db.execute("UPDATE states SET revision=?,state_json=? WHERE source_id=? AND run_id=?", (expected_revision+1,_json(envelope),source,run))
                db.execute("INSERT INTO vlsp_delivered_status VALUES(?,?)",(job_id,expected_status))
                db.execute("UPDATE jobs SET status='delivered' WHERE job_id=?",(job_id,))
                db.commit()
            return {"duplicate":False,"events":events,"state":state}
        except ScenarioError as exc:
            if exc.fatal:
                self._failed = True
            raise
        except QueueError:
            raise
        except Exception as exc:
            self._failed = True
            raise ScenarioError("scenario_exception", f"Completion failed: {type(exc).__name__}: {exc}", fatal=True) from exc
        finally:
            try:
                if hardware_token is not None:
                    self._hardware.end_call(hardware_token)
            finally:
                self._busy.release()

    def snapshot(self, source_id, run_id):
        with self._busy:
            revision, envelope = self._read_state(source_id, run_id)
            return {"revision":revision, **json_copy(envelope, code="state_contract")}

    def reset(self, source_id=None, run_id=None):
        """Reset only namespaces without any historical jobs; use a new run otherwise."""
        self._healthy()
        if source_id is not None and (type(source_id) is not str or not source_id):
            raise ScenarioError("input_contract", "Invalid reset source")
        if run_id is not None and (source_id is None or type(run_id) is not str or not run_id):
            raise ScenarioError("input_contract", "Reset run requires a source")
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Concurrent reset is not supported")
        try:
            lease = self._authorize(before_process=True)
            predicate, args = ("1=1", ()) if source_id is None else (("source_id=?", (source_id,)) if run_id is None else ("source_id=? AND run_id=?", (source_id,run_id)))
            with self._store._db() as db:
                self._store._begin(db); self._store._require_security(db)
                if db.execute("SELECT 1 FROM jobs WHERE "+predicate+" LIMIT 1",args).fetchone():
                    raise ScenarioError("lifecycle", "Cannot reset a run with historical jobs; create a new run_id")
                db.execute("DELETE FROM vlsp_frames WHERE "+predicate,args)
                db.execute("DELETE FROM states WHERE "+predicate,args)
                if lease is not None: lease.assert_current()
                db.commit()
        finally:
            self._busy.release()
