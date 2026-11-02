"""Isolated SQLite outbox prototype for asynchronous multimodal providers.

This is a separate host-side state store, NOT integrated into the existing
in-memory VLSP ScenarioExecutor. Provider/authorization/media callbacks are
trusted host infrastructure. Provider calls may repeat after worker crashes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid
from jsonschema import Draft202012Validator

MAX_JSON = 65536
MAX_IMAGE = 10 * 1024 * 1024


class QueueError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    def validate(x):
        if x is None or type(x) in (str, bool, int):
            return
        if type(x) is float and math.isfinite(x):
            return
        if type(x) is list:
            for y in x: validate(y)
            return
        if type(x) is dict and all(type(k) is str for k in x):
            for y in x.values(): validate(y)
            return
        raise ValueError("invalid native JSON type or non-finite number")
    try:
        validate(value)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")
        if len(raw) > MAX_JSON: raise ValueError("JSON size limit")
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise QueueError("invalid_json", "Finite, bounded UTF-8 JSON required") from exc


def _sha(domain, data):
    return hashlib.sha256(domain + data).hexdigest()


def _string(value, name):
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise QueueError("invalid_job", f"{name} must be a nonempty string up to 256 characters")
    try: value.encode("utf-8", "strict")
    except UnicodeError as exc: raise QueueError("invalid_job", f"invalid {name}") from exc
    return value


def _hex(value, name):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise QueueError("invalid_job", f"{name} must be lowercase SHA-256 hex")


def _timestamp(value, name):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise QueueError("invalid_job", f"{name} must be a nonnegative signed 64-bit integer")


def _namespace(value):
    value = json.loads(_json(value))
    if type(value) is not dict or set(value) != {"tenant_id", "installation_id", "scenario_id", "scenario_version", "scenario_manifest_sha256"}:
        raise QueueError("configuration", "Explicit tenant, installation and scenario namespace required")
    for key in ("tenant_id", "installation_id", "scenario_id", "scenario_version"): _string(value[key], key)
    _hex(value["scenario_manifest_sha256"], "scenario_manifest_sha256")
    return value


def _job(namespace, source_id, run_id, data):
    source_id, run_id = _string(source_id, "source_id"), _string(run_id, "run_id")
    data = json.loads(_json(data))
    fields = {"frame_id", "track_id", "tracker_alias", "source_event_time_ms", "model_alias", "model_revision", "model_digest", "payload", "media", "deadline_ms"}
    if type(data) is not dict or set(data) not in (fields, fields | {"hardware_binding"}):
        raise QueueError("invalid_job", "Unexpected job fields")
    hardware = data.get("hardware_binding")
    if "hardware_binding" in data:
        if (type(hardware) is not dict or set(hardware) != {"device_id", "hardware_manifest_sha256", "variant_id", "artifacts", "cpu_fallback", "quality", "placement"}
                or type(hardware.get("artifacts")) is not list or not hardware["artifacts"]):
            raise QueueError("invalid_job", "Invalid hardware binding")
        for field in ("device_id", "variant_id", "cpu_fallback", "quality", "placement"):
            _string(hardware[field], field)
        _hex(hardware["hardware_manifest_sha256"], "hardware_manifest_sha256")
        seen = set()
        for artifact in hardware["artifacts"]:
            if type(artifact) is not dict or set(artifact) != {"path", "sha256", "role"}:
                raise QueueError("invalid_job", "Invalid hardware artifact")
            _string(artifact["path"], "artifact path"); _string(artifact["role"], "artifact role"); _hex(artifact["sha256"], "artifact sha256")
            if artifact["path"] in seen: raise QueueError("invalid_job", "Duplicate hardware artifact")
            seen.add(artifact["path"])
    for name in ("frame_id", "model_alias", "model_revision"): _string(data[name], name)
    if data["track_id"] is not None:
        _string(data["track_id"], "track_id"); _string(data["tracker_alias"], "tracker_alias")
    elif data["tracker_alias"] is not None:
        raise QueueError("invalid_job", "tracker_alias requires track_id")
    if data["model_digest"] is not None: _hex(data["model_digest"], "model_digest")
    payload = data["payload"]
    if (type(payload) is not dict or set(payload) != {"text", "response_schema", "parameters"}
        or (payload["text"] is not None and type(payload["text"]) is not str)
        or (payload["response_schema"] is not None and type(payload["response_schema"]) is not dict)
        or type(payload["parameters"]) is not dict):
        raise QueueError("invalid_job", "Invalid text, response_schema or parameters")
    if payload["response_schema"] is not None:
        def reject_refs(value):
            if type(value) is dict:
                if {"$ref", "$dynamicRef", "$id"} & set(value):
                    raise ValueError("External or recursive schema references are not supported")
                for item in value.values(): reject_refs(item)
            elif type(value) is list:
                for item in value: reject_refs(item)
        try:
            reject_refs(payload["response_schema"])
            Draft202012Validator.check_schema(payload["response_schema"])
        except Exception as exc:
            raise QueueError("invalid_job", "Invalid or non-self-contained response schema") from exc
    _timestamp(data["source_event_time_ms"], "source_event_time_ms")
    _timestamp(data["deadline_ms"], "deadline_ms")
    media = data["media"]
    if media is None:
        if not payload["text"]: raise QueueError("invalid_job", "Text or image is required")
    else:
        if type(media) is not dict or set(media) != {"handle", "sha256", "expires_at_ms", "source_id", "run_id", "frame_id"}:
            raise QueueError("invalid_job", "Unexpected media fields")
        _string(media["handle"], "media handle"); _hex(media["sha256"], "media sha256"); _timestamp(media["expires_at_ms"], "media expires_at_ms")
        if (media["source_id"], media["run_id"], media["frame_id"]) != (source_id, run_id, data["frame_id"]):
            raise QueueError("correlation_mismatch", "Media scope must match source, run and frame")
    payload_hash = _sha(b"VLSP-ASYNC-PAYLOAD-V1\0", _json(data["payload"]))
    identity = {"namespace": namespace, "source_id": source_id, "run_id": run_id, "frame_id": data["frame_id"], "track_id": data["track_id"], "tracker_alias": data["tracker_alias"],
                "source_event_time_ms": data["source_event_time_ms"], "model_alias": data["model_alias"], "model_revision": data["model_revision"], "model_digest": data["model_digest"],
                "payload_hash": payload_hash, "media_sha256": media["sha256"] if media else None, "media_handle": media["handle"] if media else None,
                "media_expires_at_ms": media["expires_at_ms"] if media else None, "deadline_ms": data["deadline_ms"]}
    if "hardware_binding" in data:
        identity["hardware_binding"] = hardware
    job_id = _sha(b"VLSP-ASYNC-JOB-V1\0", _json(identity))
    return {"job_id": job_id, "identity": identity, "payload": data["payload"], "media": media}


class AsyncStore:
    def __init__(self, db_path, *, namespace, authorize, validate_result, clock=None):
        if not callable(authorize) or not callable(validate_result): raise QueueError("configuration", "Trusted authorization and result validator are required")
        self._namespace = _json(_namespace(namespace))
        self.authorize = authorize
        self.validate_result = validate_result
        self._security = None
        self._model_contracts = None
        self._hardware = None
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.path = Path(db_path)
        if str(self.path) == ":memory:" or self.path.is_symlink() or not self.path.parent.is_dir():
            raise QueueError("configuration", "A regular file in an existing private directory is required")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY CHECK(id=1), namespace_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS states(source_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL, state_json BLOB NOT NULL, PRIMARY KEY(source_id,run_id));
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, identity_json BLOB NOT NULL, job_json BLOB NOT NULL, status TEXT NOT NULL,
                  source_id TEXT NOT NULL, run_id TEXT NOT NULL, deadline INTEGER NOT NULL, media_expires_at INTEGER NOT NULL, expected_revision INTEGER NOT NULL,
                  committed_state_hash TEXT NOT NULL, attempt_id TEXT, lease_until INTEGER, result_json BLOB, result_hash TEXT);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,deadline);
                CREATE TABLE IF NOT EXISTS vlsp_attempt_leases(job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                  snapshot_json BLOB NOT NULL, PRIMARY KEY(job_id,attempt_id));
                CREATE TABLE IF NOT EXISTS vlsp_hardware_attempt_leases(job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                  snapshot_json BLOB NOT NULL, PRIMARY KEY(job_id,attempt_id));
                CREATE TABLE IF NOT EXISTS vlsp_enqueue_leases(job_id TEXT PRIMARY KEY, snapshot_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS vlsp_events(event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                  run_id TEXT NOT NULL, event_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS vlsp_security(id INTEGER PRIMARY KEY CHECK(id=1), required INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vlsp_frames(source_id TEXT NOT NULL, run_id TEXT NOT NULL,
                  frame_id TEXT NOT NULL, PRIMARY KEY(source_id,run_id,frame_id));
                CREATE TABLE IF NOT EXISTS vlsp_delivered_status(job_id TEXT PRIMARY KEY, terminal_status TEXT NOT NULL);
            ''')
            self._begin(db)
            db.execute("INSERT OR IGNORE INTO metadata(id,namespace_json) VALUES(1,?)", (self._namespace,))
            if db.execute("SELECT namespace_json FROM metadata WHERE id=1").fetchone()[0] != self._namespace:
                raise QueueError("namespace_mismatch", "Database belongs to another tenant or scenario installation")
            db.commit()

    def bind_security(self, authorized, installation_signing_key):
        """Use the trusted package's signed per-attempt lease, including after restart."""
        from vl_exchange_security import AuthorizedPackage
        from nacl.signing import SigningKey
        namespace = json.loads(self._namespace)
        if not isinstance(authorized, AuthorizedPackage) or not isinstance(installation_signing_key, SigningKey):
            raise QueueError("configuration", "A verified package and installation signing key are required")
        if authorized.claims.get("manifest_sha256") != namespace["scenario_manifest_sha256"]:
            raise QueueError("namespace_mismatch", "Store and licensed scenario manifest differ")
        if authorized.claims.get("installation_id") != namespace["installation_id"]:
            raise QueueError("namespace_mismatch", "Store and licensed installation differ")
        authorized.assert_current()
        try:
            verify_key = authorized.capture_persistent_lease("vlsp-bind-store", installation_signing_key)
        except Exception as exc:
            raise QueueError("unauthorized", "Installation key cannot sign for this licensed package") from exc
        with self._db() as db:
            self._begin(db)
            unbound = db.execute("SELECT COUNT(*) FROM jobs WHERE job_id NOT IN (SELECT job_id FROM vlsp_enqueue_leases)").fetchone()[0]
            if unbound:
                raise QueueError("configuration", "Cannot bind a protected runtime to pre-existing unprotected jobs")
            db.execute("INSERT INTO vlsp_security VALUES(1,1) ON CONFLICT(id) DO UPDATE SET required=1")
            authorized.assert_persistent_lease(verify_key, "vlsp-bind-store")
            db.commit()
        self._security = (authorized, installation_signing_key)

    def bind_hardware(self, adapter, scenario_manifest_bytes, bindings, authorities):
        """Attach trusted, already selected roles and their licensed model authorities.

        Rebind after every process restart. Adapters recheck signed selection; each
        authority signs and verifies a persistent lease for the precise attempt.
        Neither the selection nor its authority may come from a scenario job.
        """
        if (not isinstance(scenario_manifest_bytes, bytes)
                or hashlib.sha256(scenario_manifest_bytes).hexdigest() != json.loads(self._namespace)["scenario_manifest_sha256"]
                or not callable(getattr(adapter, "assert_current", None))
                or type(bindings) is not dict or not bindings or type(authorities) is not dict
                or set(bindings) != set(authorities)):
            raise QueueError("configuration", "Trusted scenario-bound hardware adapters and authorities required")
        copied = json.loads(_json(bindings))
        for alias, selection in copied.items():
            _string(alias, "model alias")
            authority = authorities[alias]
            if not all(callable(getattr(authority, method, None)) for method in ("capture_persistent_lease", "assert_persistent_lease", "matches_selection")):
                raise QueueError("configuration", "A persistent licensed model authority is required for each hardware role")
            try:
                if adapter.assert_current(scenario_manifest_bytes, alias, selection) != selection or authority.matches_selection(alias, selection) is not True:
                    raise ValueError("Selection differs")
            except Exception as exc:
                raise QueueError("unauthorized", "Hardware selection is not currently authorized") from exc
        self._hardware = (adapter, scenario_manifest_bytes, copied, dict(authorities))

    def _hardware_guard(self, job):
        identity = job["identity"]
        actual = identity.get("hardware_binding")
        role = identity["model_alias"]
        if self._hardware is None:
            if actual is not None:
                raise QueueError("unauthorized", "Hardware-bound job needs a trusted adapter and model authority")
            return
        adapter, scenario, bindings, authorities = self._hardware
        expected = bindings.get(role)
        if expected is None:
            if actual is not None:
                raise QueueError("unauthorized", "Unknown hardware role")
            return
        if actual != expected:
            raise QueueError("correlation_mismatch", "Hardware binding differs from selected identity")
        try:
            if adapter.assert_current(scenario, role, expected) != expected or authorities[role].matches_selection(role, expected) is not True:
                raise ValueError("Selection changed")
        except Exception as exc:
            raise QueueError("unauthorized", "Hardware selection or model license is no longer valid") from exc

    def _capture_hardware(self, job, binding):
        self._hardware_guard(job)
        if "hardware_binding" not in job["identity"]:
            return None
        try:
            return self._hardware[3][job["identity"]["model_alias"]].capture_persistent_lease(binding)
        except Exception as exc:
            raise QueueError("unauthorized", "Could not capture model license lease") from exc

    def _verify_hardware(self, job, snapshot, binding):
        self._hardware_guard(job)
        if "hardware_binding" not in job["identity"]:
            if snapshot is not None:
                raise QueueError("correlation_mismatch", "Unexpected hardware lease")
            return
        if snapshot is None:
            raise QueueError("unauthorized", "Missing signed model attempt lease")
        try:
            self._hardware[3][job["identity"]["model_alias"]].assert_persistent_lease(snapshot, binding)
        except Exception as exc:
            raise QueueError("unauthorized", "Original model license lease is no longer valid") from exc

    def _verify_hardware_delivery(self, db, row, job):
        """Completed hardware jobs retain both original per-attempt grants at delivery."""
        if "hardware_binding" not in job["identity"] or row["status"] != "completed":
            return None
        attempt = row["attempt_id"]
        if not isinstance(attempt, str) or not attempt:
            raise QueueError("unauthorized", "Completed hardware job has no original attempt")
        binding = f"vlsp-dispatch:{job['job_id']}:{attempt}"
        scenario_row = db.execute("SELECT snapshot_json FROM vlsp_attempt_leases WHERE job_id=? AND attempt_id=?",(job["job_id"], attempt)).fetchone()
        model_row = db.execute("SELECT snapshot_json FROM vlsp_hardware_attempt_leases WHERE job_id=? AND attempt_id=?",(job["job_id"], attempt)).fetchone()
        scenario_raw = scenario_row["snapshot_json"] if scenario_row is not None else None
        model_raw = model_row["snapshot_json"] if model_row is not None else None
        try:
            scenario_snapshot = json.loads(scenario_raw) if scenario_raw is not None else None
            model_snapshot = json.loads(model_raw) if model_raw is not None else None
        except Exception as exc:
            raise QueueError("unauthorized", "Invalid original attempt lease") from exc
        self._verify(scenario_snapshot, binding)
        self._verify_hardware(job, model_snapshot, binding)
        return (attempt, scenario_raw, model_raw)

    def _require_security(self, db):
        required = db.execute("SELECT required FROM vlsp_security WHERE id=1").fetchone()
        if required is not None and required[0]:
            if self._security is None:
                raise QueueError("unauthorized", "This durable queue requires an active verified scenario license")
            try:
                self._security[0].assert_current()
            except Exception as exc:
                raise QueueError("unauthorized", "The scenario license is no longer active") from exc

    def _capture(self, binding):
        if self._security is None:
            return None
        try:
            authorized, key = self._security
            return authorized.capture_persistent_lease(binding, key)
        except Exception as exc:
            raise QueueError("unauthorized", "Could not capture a signed operation lease") from exc

    def _verify(self, snapshot, binding):
        if self._security is None:
            return
        try:
            if snapshot is None:
                raise ValueError("Missing signed operation lease")
            self._security[0].assert_persistent_lease(snapshot, binding)
        except Exception as exc:
            raise QueueError("unauthorized", "Original signed operation lease is no longer valid") from exc

    def _verify_enqueue(self, db, job_id):
        if self._security is None:
            return
        row = db.execute("SELECT snapshot_json FROM vlsp_enqueue_leases WHERE job_id=?", (job_id,)).fetchone()
        try:
            if row is None:
                raise ValueError("Missing enqueue signature")
            self._security[0].assert_persistent_lease_integrity(json.loads(row["snapshot_json"]), f"vlsp-enqueue:{job_id}")
        except Exception as exc:
            raise QueueError("unauthorized", "Job has no valid bound enqueue signature") from exc

    def _model_guard(self, job, result=None, *, media_bytes=None):
        self._hardware_guard(job)
        if self._model_contracts is None:
            if self._security is not None:
                raise QueueError("unauthorized", "Protected model bindings are not configured")
            return
        identity = job["identity"]
        model = self._model_contracts.get(identity["model_alias"])
        if model is None or identity["model_revision"] != model.revision or identity["model_digest"] != model.model_digest:
            raise QueueError("correlation_mismatch", "Job model differs from trusted declared binding")
        # The request limit covers both canonical UTF-8 payload and the exact
        # raw snapshot bytes, not merely the small opaque media descriptor.
        if len(_json(job["payload"])) + (len(media_bytes) if media_bytes is not None else 0) > model.max_input_bytes:
            raise QueueError("invalid_job", "Job exceeds model input size")
        if result is not None:
            if (len(_json(result)) > model.max_output_bytes
                or (result["text"] is not None and "text" not in model.output_types)
                or (result["structured"] is not None and "structured" not in model.output_types)):
                raise QueueError("invalid_result", "Output is outside the model contract")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        except BaseException:
            if db.in_transaction: db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        now = self.clock(); _timestamp(now, "clock"); return now

    def _authorize(self, phase, job):
        try:
            if self.authorize(phase, json.loads(_json(job))) is False:
                raise ValueError("denied")
        except Exception as exc:
            raise QueueError("unauthorized", f"Authorization failed for {phase}") from exc

    @staticmethod
    def _begin(db): db.execute("BEGIN IMMEDIATE")

    def _validated_row(self, row):
        """Recompute canonical job identity from payload, media and row scope."""
        try:
            data = json.loads(row["job_json"])
            ident = data["identity"]
            request = {"frame_id":ident["frame_id"], "track_id":ident["track_id"], "tracker_alias":ident["tracker_alias"],
                       "source_event_time_ms":ident["source_event_time_ms"], "model_alias":ident["model_alias"],
                       "model_revision":ident["model_revision"], "model_digest":ident["model_digest"],
                       "payload":data["payload"], "media":data["media"], "deadline_ms":ident["deadline_ms"]}
            if "hardware_binding" in ident:
                request["hardware_binding"] = ident["hardware_binding"]
            actual = _job(json.loads(self._namespace), row["source_id"], row["run_id"], request)
            expiry = actual["media"]["expires_at_ms"] if actual["media"] else actual["identity"]["deadline_ms"]
            if (actual["job_id"] != row["job_id"] or _json(actual) != row["job_json"]
                    or _json(actual["identity"]) != row["identity_json"]
                    or actual["identity"]["deadline_ms"] != row["deadline"] or expiry != row["media_expires_at"]):
                raise ValueError("Job digest or persisted columns changed")
            return actual
        except Exception as exc:
            raise QueueError("correlation_mismatch", "Persisted job data or identity changed") from exc

    def state(self, source_id, run_id):
        with self._db() as db:
            self._require_security(db)
            row = db.execute("SELECT revision,state_json FROM states WHERE source_id=? AND run_id=?", (source_id,run_id)).fetchone()
            return (row["revision"], json.loads(row["state_json"])) if row else (0, {})

    def enqueue_and_commit(self, source_id, run_id, expected_revision, next_state, job):
        if self._security is not None:
            raise QueueError("configuration", "Protected jobs must be enqueued through the integrated executor")
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1 or type(next_state) is not dict:
            raise QueueError("invalid_state", "Expected revision and object state are required")
        state_raw = _json(next_state); state_hash = _sha(b"VLSP-ASYNC-STATE-V1\0", state_raw)
        data = _job(json.loads(self._namespace), source_id, run_id, job); now = self._now()
        self._model_guard(data)
        media_expiry = data["media"]["expires_at_ms"] if data["media"] else data["identity"]["deadline_ms"]
        if now >= data["identity"]["deadline_ms"] or now >= media_expiry:
            raise QueueError("expired", "Deadline or media has expired")
        self._authorize("enqueue", data)
        with self._db() as db:
            self._begin(db)
            self._require_security(db)
            if db.execute("SELECT required FROM vlsp_security WHERE id=1").fetchone():
                raise QueueError("configuration", "Protected jobs must be enqueued through the integrated executor")
            old = db.execute("SELECT * FROM jobs WHERE job_id=?", (data["job_id"],)).fetchone()
            if old:
                if old["expected_revision"] != expected_revision or old["committed_state_hash"] != state_hash:
                    raise QueueError("idempotency_conflict", "Job ID already bound to another state transaction")
                db.commit()
                return {"job_id":data["job_id"],"revision":expected_revision + 1,"duplicate":True}
            if self._now() >= data["identity"]["deadline_ms"] or self._now() >= media_expiry:
                raise QueueError("expired", "Deadline or media expired while waiting for transaction")
            state = db.execute("SELECT revision FROM states WHERE source_id=? AND run_id=?", (source_id,run_id)).fetchone()
            if (state["revision"] if state else 0) != expected_revision:
                raise QueueError("revision_conflict", "State revision differs")
            db.execute("INSERT INTO states VALUES(?,?,?,?) ON CONFLICT(source_id,run_id) DO UPDATE SET revision=excluded.revision,state_json=excluded.state_json", (source_id,run_id,expected_revision+1,state_raw))
            db.execute("INSERT INTO jobs(job_id,identity_json,job_json,status,source_id,run_id,deadline,media_expires_at,expected_revision,committed_state_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (data["job_id"],_json(data["identity"]),_json(data),"pending",source_id,run_id,data["identity"]["deadline_ms"],media_expiry,expected_revision,state_hash))
            self._authorize("enqueue_commit", data)
            if self._now() >= data["identity"]["deadline_ms"] or self._now() >= media_expiry:
                raise QueueError("expired", "Deadline or media expired before transaction commit")
            db.commit()
        return {"job_id":data["job_id"],"revision":expected_revision+1,"duplicate":False}

    def status(self, job_id):
        with self._db() as db:
            self._require_security(db)
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            return row["status"]

    def wire_job(self, job_id):
        """Return the draft wire job; caller is trusted and enforces access policy."""
        with self._db() as db:
            self._require_security(db)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            job = self._validated_row(row); self._authorize("inspect", job)
            identity = job["identity"]
            keys = ("namespace", "source_id", "run_id", "frame_id", "tracker_alias", "track_id",
                    "source_event_time_ms", "model_alias", "model_revision", "deadline_ms")
            return {**{key:identity[key] for key in keys}, "job_id":job_id,
                    "payload":job["payload"], "media":job["media"]}

    def wire_completion(self, job_id):
        """Map an already recorded final outcome to the draft completion shape."""
        with self._db() as db:
            self._require_security(db)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            job = self._validated_row(row); self._authorize("inspect", job)
            status = row["status"]
            if status not in ("completed", "delivered", "failed", "timed_out", "cancelled"):
                raise QueueError("invalid_status", "Job has no final completion")
            identity = job["identity"]
            keys = ("namespace", "source_id", "run_id", "frame_id", "tracker_alias", "track_id",
                    "source_event_time_ms", "model_alias", "model_revision")
            result = json.loads(row["result_json"]) if row["result_json"] is not None else None
            if status == "delivered":
                previous = db.execute("SELECT terminal_status FROM vlsp_delivered_status WHERE job_id=?",(job_id,)).fetchone()
                status = previous["terminal_status"] if previous is not None else "completed"
            if status in ("completed", "delivered"):
                completion = {"status":"completed", "text":result["text"], "structured":result["structured"], "error":None}
            else:
                completion = {"status":status, "text":None, "structured":None, "error":{"code":status}}
            return {**{key:identity[key] for key in keys}, "job_id":job_id, **completion}

    def recover(self):
        with self._db() as db:
            self._begin(db)
            self._require_security(db)
            now = self._now()
            timed = db.execute("UPDATE jobs SET status='timed_out',attempt_id=NULL,lease_until=NULL WHERE (status IN ('pending','running') AND deadline<=?) OR (status='pending' AND media_expires_at<=?)", (now,now)).rowcount
            retried = db.execute("UPDATE jobs SET status='pending',attempt_id=NULL,lease_until=NULL WHERE status='running' AND lease_until<=? AND deadline>?", (now,now)).rowcount
            db.commit()
        return {"timed_out":timed,"requeued":retried}

    def claim(self, *, lease_seconds=30):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600: raise QueueError("configuration", "Invalid worker lease")
        if self._security is not None:
            try:
                # A long queue wait can outlive the enqueue grant; refresh only
                # the current authority before checking the historical signature.
                self._security[0].before_process()
            except Exception as exc:
                raise QueueError("unauthorized", "Current license cannot authorize dispatch") from exc
        self.recover()
        with self._db() as db:
            self._require_security(db)
            now = self._now()
            row = db.execute("SELECT * FROM jobs WHERE status='pending' AND deadline>? AND media_expires_at>? ORDER BY deadline,job_id LIMIT 1", (now,now)).fetchone()
            if row is None: return None
            data = self._validated_row(row)
            self._verify_enqueue(db, row["job_id"])
            self._model_guard(data)
        self._authorize("dispatch", data)
        attempt = uuid.uuid4().hex
        binding = f"vlsp-dispatch:{data['job_id']}:{attempt}"
        # License renewal can use the network: do not hold SQLite's writer lock.
        snapshot = self._capture(binding)
        hardware_snapshot = self._capture_hardware(data, binding)
        with self._db() as db:
            self._begin(db); self._require_security(db)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?",(data["job_id"],)).fetchone()
            if row is None or row["status"] != "pending":
                db.commit(); return None
            if self._validated_row(row) != data:
                raise QueueError("correlation_mismatch", "Job changed while capturing authorization")
            self._verify_enqueue(db, row["job_id"])
            now = self._now()
            if now >= row["deadline"]:
                db.execute("UPDATE jobs SET status='timed_out' WHERE job_id=?", (row["job_id"],)); db.commit()
                raise QueueError("expired", "Job expired before dispatch")
            if data["media"] is not None and now >= data["media"]["expires_at_ms"]:
                db.execute("UPDATE jobs SET status='timed_out' WHERE job_id=?", (row["job_id"],)); db.commit()
                raise QueueError("expired", "Media expired before dispatch")
            lease_until = min(now + lease_seconds * 1000, row["deadline"])
            db.execute("UPDATE jobs SET status='running',attempt_id=?,lease_until=? WHERE job_id=?", (attempt,lease_until,row["job_id"]))
            if snapshot is not None:
                db.execute("INSERT INTO vlsp_attempt_leases VALUES(?,?,?)", (row["job_id"],attempt,_json(snapshot)))
                self._verify(snapshot, binding)
            if hardware_snapshot is not None:
                db.execute("INSERT INTO vlsp_hardware_attempt_leases VALUES(?,?,?)", (row["job_id"],attempt,_json(hardware_snapshot)))
            self._verify_hardware(data, hardware_snapshot, binding)
            db.commit()
        return {"job":data,"attempt_id":attempt,"lease_until":lease_until,"security_snapshot":snapshot,"hardware_snapshot":hardware_snapshot}

    def complete(self, job_id, attempt_id, identity, result):
        if (type(result) is not dict or set(result) != {"text", "structured"}
            or (result["text"] is not None and type(result["text"]) is not str)
            or (result["structured"] is not None and type(result["structured"]) is not dict)
            or (not result["text"] and result["structured"] is None)):
            raise QueueError("invalid_result", "Result requires text and/or structured output")
        result_raw = _json(result); result_hash = _sha(b"VLSP-ASYNC-RESULT-V1\0", result_raw)
        with self._db() as db:
            self._begin(db)
            self._require_security(db)
            now = self._now()
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            data = self._validated_row(row); self._authorize("complete", data)
            self._verify_enqueue(db, job_id)
            self._model_guard(data, result)
            binding = f"vlsp-dispatch:{job_id}:{attempt_id}"
            snaprow = db.execute("SELECT snapshot_json FROM vlsp_attempt_leases WHERE job_id=? AND attempt_id=?",(job_id,attempt_id)).fetchone()
            snapshot = json.loads(snaprow["snapshot_json"]) if snaprow is not None else None
            self._verify(snapshot,binding)
            hwrow = db.execute("SELECT snapshot_json FROM vlsp_hardware_attempt_leases WHERE job_id=? AND attempt_id=?",(job_id,attempt_id)).fetchone()
            hardware_snapshot = json.loads(hwrow["snapshot_json"]) if hwrow is not None else None
            self._verify_hardware(data, hardware_snapshot, binding)
            if _json(identity) != row["identity_json"] or attempt_id != row["attempt_id"]:
                raise QueueError("correlation_mismatch", "Completion does not match job and attempt")
            if row["status"] in ("completed", "delivered"):
                if row["result_hash"] != result_hash: raise QueueError("duplicate_conflict", "Different result already recorded")
                db.commit(); return {"duplicate":True,"status":row["status"]}
            if row["status"] != "running": raise QueueError("invalid_status", "Job is not running")
            if now >= row["deadline"] or now >= row["lease_until"]:
                raise QueueError("expired", "Completion arrived after deadline or worker lease")
            try:
                schema = data["payload"]["response_schema"]
                if schema is not None:
                    if result["structured"] is None or list(Draft202012Validator(schema).iter_errors(result["structured"])):
                        raise ValueError("Structured output does not match requested schema")
                if self.validate_result(json.loads(result_raw), json.loads(_json(data))) is False: raise ValueError("invalid result")
            except Exception as exc:
                raise QueueError("invalid_result", "Host rejected provider result") from exc
            self._authorize("complete_commit", data)
            self._verify(snapshot,binding)
            self._verify_hardware(data, hardware_snapshot, binding)
            if self._now() >= row["deadline"] or self._now() >= row["lease_until"]:
                raise QueueError("expired", "Completion expired before transaction commit")
            db.execute("UPDATE jobs SET status='completed',result_json=?,result_hash=? WHERE job_id=?", (result_raw,result_hash,job_id))
            db.commit()
        return {"duplicate":False,"status":"completed"}

    def apply_result(self, job_id, expected_revision, transition):
        if self._security is not None:
            raise QueueError("configuration", "Protected results must be applied through the integrated executor")
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1 or not callable(transition):
            raise QueueError("invalid_state", "A revision and pure transition are required")
        with self._db() as db:
            self._begin(db)
            self._require_security(db)
            if db.execute("SELECT required FROM vlsp_security WHERE id=1").fetchone():
                raise QueueError("configuration", "Protected results must be applied through the integrated executor")
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            data = self._validated_row(row); self._authorize("apply", data)
            state = db.execute("SELECT revision,state_json FROM states WHERE source_id=? AND run_id=?", (row["source_id"],row["run_id"])).fetchone()
            if row["status"] == "delivered": db.commit(); return {"duplicate":True,"revision":state["revision"]}
            if row["status"] != "completed": raise QueueError("invalid_status", "Job result is not completed")
            if state is None or state["revision"] != expected_revision: raise QueueError("revision_conflict", "State revision differs")
            candidate = transition(json.loads(state["state_json"]), json.loads(row["result_json"]), json.loads(row["identity_json"]))
            if type(candidate) is not dict: raise QueueError("invalid_state", "Transition must return JSON object")
            self._authorize("apply_commit", data)
            db.execute("UPDATE states SET revision=?,state_json=? WHERE source_id=? AND run_id=?", (expected_revision+1,_json(candidate),row["source_id"],row["run_id"]))
            db.execute("UPDATE jobs SET status='delivered' WHERE job_id=?", (job_id,)); db.commit()
        return {"duplicate":False,"revision":expected_revision+1}

    def cancel(self, job_id):
        with self._db() as db:
            self._begin(db)
            self._require_security(db)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None: raise QueueError("unknown_job", "Unknown job")
            self._authorize("cancel", self._validated_row(row))
            if row["status"] == "cancelled": db.commit(); return False
            if row["status"] not in ("pending","running","completed"): raise QueueError("invalid_status", "Job cannot be cancelled")
            db.execute("UPDATE jobs SET status='cancelled',attempt_id=NULL,lease_until=NULL WHERE job_id=?", (job_id,)); db.commit()
        return True

    def dispatch_one(self, provider, media_resolver, *, lease_seconds=30):
        if not callable(provider) or (media_resolver is not None and not callable(media_resolver)): raise QueueError("configuration", "Trusted provider and optional media resolver required")
        claim = self.claim(lease_seconds=lease_seconds)
        if claim is None: return None
        job, attempt = claim["job"], claim["attempt_id"]
        try:
            image = None
            if job["media"] is not None:
                if media_resolver is None: raise QueueError("configuration", "Image jobs require a trusted media resolver")
                image = media_resolver(json.loads(_json(job["media"])), json.loads(_json(job["identity"])))
                if type(image) is not bytes or len(image) > MAX_IMAGE or hashlib.sha256(image).hexdigest() != job["media"]["sha256"]:
                    raise QueueError("media_mismatch", "Media is inaccessible or differs from its authenticated digest")
                if self._now() >= job["media"]["expires_at_ms"]: raise QueueError("expired", "Media expired before provider call")
            self._model_guard(job, media_bytes=image)
            with self._db() as db:
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                if row is not None:
                    self._verify_enqueue(db, job["job_id"])
            if (row is None or row["status"] != "running" or row["attempt_id"] != attempt
                or self._now() >= row["deadline"] or self._now() >= row["lease_until"]):
                raise QueueError("expired", "Job was cancelled or worker lease expired before provider call")
            if self._validated_row(row) != job:
                raise QueueError("correlation_mismatch", "Persisted job changed before provider call")
            self._authorize("provider", job)
            self._verify(claim["security_snapshot"],f"vlsp-dispatch:{job['job_id']}:{attempt}")
            self._verify_hardware(job, claim["hardware_snapshot"], f"vlsp-dispatch:{job['job_id']}:{attempt}")
            result = provider(json.loads(_json(job)), image)
            return self.complete(job["job_id"], attempt, job["identity"], result)
        except Exception as exc:
            with self._db() as db:
                self._begin(db)
                status = "timed_out" if isinstance(exc, QueueError) and exc.code == "expired" else "failed"
                db.execute("UPDATE jobs SET status=? WHERE job_id=? AND status='running' AND attempt_id=?", (status,job["job_id"],attempt))
                db.commit()
            raise
