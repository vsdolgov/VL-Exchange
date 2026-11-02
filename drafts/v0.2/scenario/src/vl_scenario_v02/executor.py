"""Independent scenario orchestration with explicit transactional JSON state."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import hashlib
from importlib import metadata
from pathlib import Path
import platform
import re
import sys
from threading import Lock
from time import perf_counter
import uuid

from .common import json_copy, package_path, same_json
from .contracts import parameter_values, validate_input, validate_value, materialize_events, SCHEMAS
from .common import read_json
from .errors import ScenarioError
from .validation import validate_package
from .services import CallServices, Host


def _contains_frame_ref(value, frame_ref):
    if type(value) is str:
        return value == frame_ref
    if type(value) is list:
        return any(_contains_frame_ref(item, frame_ref) for item in value)
    if type(value) is dict:
        return any(_contains_frame_ref(key, frame_ref) or _contains_frame_ref(item, frame_ref)
                   for key, item in value.items())
    return False


@contextmanager
def _isolated_module(code_root: Path, module_path: Path, authorize):
    namespace = "_vlsp_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(namespace, code_root / "__init__.py",
                                                submodule_search_locations=[str(code_root)])
    if spec is None or spec.loader is None:
        raise ScenarioError("scenario_exception", "Cannot construct a package import", fatal=True)
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    old_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        authorize()
        spec.loader.exec_module(package)
        name = namespace + ".scenario"
        module = sys.modules.get(name)
        if module is None:
            module_spec = importlib.util.spec_from_file_location(name, module_path)
            if module_spec is None or module_spec.loader is None:
                raise ScenarioError("scenario_exception", "Cannot construct scenario module import", fatal=True)
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[name] = module
            authorize()
            module_spec.loader.exec_module(module)
        yield module
    finally:
        sys.dont_write_bytecode = old_dont_write
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                del sys.modules[name]


class ScenarioExecutor:
    """A neutral scenario host with protected external delivery by default.

    There is no graphics/editor dependency. State is owned by this engine rather
    than hidden in a platform-specific node wrapper.

    The host must exclude untrusted writes to verified source throughout its
    lifetime, using a private snapshot or read-only mount. Protected delivery
    supplies a private 0700 directory. A trusted administrator and trusted
    publisher Python remain outside any in-process isolation guarantee.
    """

    def __init__(self, root, *, policy="protected", parameters=None, host=None, hardware=None):
        if policy != "development":
            raise ScenarioError("policy_rejected", "Protected delivery requires ScenarioExecutor.open_protected(..., verifier=...) before importing code")
        try:
            self._initialize(root, parameters=parameters, host=host, authorized=None, hardware=hardware)
        except ScenarioError as exc:
            # No live instance exists when construction fails.
            raise ScenarioError(exc.code, exc.message, stage=exc.stage, fatal=False, retryable=False) from exc
        except Exception as exc:
            raise ScenarioError("scenario_exception", f"Scenario initialization failed: {type(exc).__name__}: {exc}") from exc

    def _initialize(self, root, *, parameters, host, authorized, hardware=None):
        if not isinstance(host, Host):
            raise ScenarioError("dependency_missing", "A trusted Host containing model and tracker bindings is required")
        # Bind the checked identities and callbacks for this executor instance;
        # later mutation of the operator's registry cannot silently swap a model.
        self._host = Host(models=dict(host.models), trackers=dict(host.trackers))
        self._authorized = authorized
        self._closed = False
        self._failed = False
        self._busy = Lock()
        self._states = {}
        self._order = {}
        self._seen_frames = {}
        self._module_cm = None
        self._scenario = None
        self._metrics = {"process_calls": 0, "committed_calls": 0, "reset_calls": 0,
                         "close_calls": 0, "scenario_close_called": False}
        self._authorize()
        # Validate and authorize the exact scenario before opening any hardware model.
        self._package = validate_package(root, parameters=parameters, host=None if hardware else self._host, check_environment=True,
                                         allow_async=getattr(self, "_supports_async", False),
                                         allow_hardware_async=getattr(self, "_supports_hardware_async", False))
        if authorized is not None:
            claims = authorized.claims
            if (claims.get("package_format") != "vl.scenario-package" or claims.get("manifest_path") != "scenario-package.json"
                    or claims.get("manifest_sha256") != self._package.manifest_sha256):
                raise ScenarioError("policy_rejected", "The signed delivery does not license this exact scenario manifest")
        self._hardware = None
        if hardware is not None:
            from .hardware import HardwareAdapter
            if not isinstance(hardware, HardwareAdapter):
                raise ScenarioError("hardware_rejected", "A trusted HardwareAdapter is required")
            scenario_bytes = (self._package.root / "scenario-package.json").read_bytes()
            if hashlib.sha256(scenario_bytes).hexdigest() != self._package.manifest_sha256:
                raise ScenarioError("hardware_rejected", "Scenario manifest changed after validation")
            self._host, self._hardware = hardware.bind(scenario_bytes, self._package.manifest, self._host)
            self._package = validate_package(root, parameters=parameters, host=self._host, check_environment=True,
                                             allow_async=getattr(self, "_supports_async", False),
                                             allow_hardware_async=getattr(self, "_supports_hardware_async", False))
            if hashlib.sha256(scenario_bytes).hexdigest() != self._package.manifest_sha256:
                raise ScenarioError("hardware_rejected", "Scenario manifest changed during hardware binding")
        self._parameters = parameter_values(self._package.manifest, parameters)
        entry = self._package.manifest["entrypoint"]
        code_root = self._package.root / entry["root"]
        module_path = package_path(self._package.root, entry["root"] + "/" + entry["module"])
        self._module_cm = _isolated_module(code_root, module_path, self._authorize)
        try:
            self._authorize()
            module = self._module_cm.__enter__()
            cls = getattr(module, entry["class"])
            self._authorize()
            self._scenario = cls(**json_copy(self._parameters, code="parameter_invalid"))
            if not callable(getattr(self._scenario, "process", None)):
                raise ScenarioError("scenario_exception", "Scenario.process is not callable", fatal=True)
        except BaseException as exc:
            if self._module_cm is not None:
                self._module_cm.__exit__(*sys.exc_info())
                self._module_cm = None
            self._scenario = None
            self._closed = True
            if isinstance(exc, ScenarioError):
                raise
            raise ScenarioError("scenario_exception", f"Scenario initialization failed: {type(exc).__name__}: {exc}", fatal=True) from exc

    @classmethod
    @contextmanager
    def open_protected(cls, envelope_path, *, verifier, parameters=None, host=None, hardware=None):
        """Keep a trusted verifier's private plaintext lifetime around execution.

        `verifier(path)` must return a context manager yielding the root security
        library's authorized object: root, claims, package_sha256, assert_current.
        No manifest flag or boolean can grant protected delivery status. A type
        check is a protocol assertion, not isolation from a trusted administrator
        or arbitrary Python in this process; keys belong to trusted host config.
        """
        if not callable(verifier):
            raise ScenarioError("policy_rejected", "A configured trusted protected-source verifier is required")
        try:
            manager = verifier(Path(envelope_path))
        except Exception as exc:
            raise ScenarioError("authorization_failed", f"Protected source verification failed: {exc}") from exc
        executor = None
        ready = False
        entered = False
        try:
            with manager as authorized:
                entered = True
                if not callable(getattr(authorized, "assert_current", None)) or not callable(getattr(authorized, "before_process", None)) or not all(hasattr(authorized, field) for field in ("root", "claims", "package_sha256")):
                    raise ScenarioError("policy_rejected", "Verifier did not return the required authorized-package receipt")
                if not re.fullmatch(r"[0-9a-f]{64}", authorized.package_sha256):
                    raise ScenarioError("policy_rejected", "Verifier receipt contains an invalid package digest")
                json_copy(authorized.claims, code="policy_rejected")
                executor = cls.__new__(cls)
                executor._initialize(authorized.root, parameters=parameters,
                                     host=host, authorized=authorized, hardware=hardware)
                ready = True
                try:
                    yield executor
                finally:
                    executor.close()
        except ScenarioError as exc:
            if not ready:
                raise ScenarioError(exc.code, exc.message, stage=exc.stage, fatal=False, retryable=False) from exc
            raise
        except Exception as exc:
            if not ready:
                code = "scenario_exception" if entered else "authorization_failed"
                raise ScenarioError(code, f"Protected source initialization failed: {type(exc).__name__}: {exc}") from exc
            raise

    def _authorize(self, *, before_process=False):
        if self._authorized is not None:
            try:
                method = self._authorized.before_process if before_process else self._authorized.assert_current
                receipt = method()
                if receipt is False:
                    raise ValueError("Authorization guard returned false")
                if before_process:
                    from vl_exchange_security import ExecutionLease
                    if not isinstance(receipt, ExecutionLease):
                        raise ValueError("Protected calls require an immutable ExecutionLease from the trusted security opener")
                return receipt
            except Exception as exc:
                raise ScenarioError("authorization_failed", f"Protected source is not currently authorized: {exc}", retryable=True) from exc

    def _healthy(self):
        if self._closed:
            raise ScenarioError("lifecycle", "Executor is closed")
        if self._failed:
            raise ScenarioError("lifecycle", "Executor failed; create a new instance", fatal=True)

    @property
    def manifest(self):
        return json_copy(self._package.manifest, code="manifest_invalid")

    @property
    def parameters(self):
        return json_copy(self._parameters, code="parameter_invalid")

    def snapshot(self):
        with self._busy:
            states, ordering = {}, {}
            for (source, run), value in self._states.items():
                states.setdefault(source, {})[run] = value
            for (source, run), value in self._order.items():
                ordering.setdefault(source, {})[run] = list(value)
            return json_copy({"state": states, "order": ordering}, code="state_contract")

    def process(self, inputs, context):
        self._healthy()
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Concurrent calls on one executor are not supported")
        hardware_token = None
        try:
            self._healthy()
            manifest = self._package.manifest
            limits = manifest["limits"]
            context = validate_value(context, read_json(SCHEMAS / "context.schema.json"), code="input_contract", max_bytes=limits["max_input_bytes"])
            inputs = validate_input(inputs, manifest)
            key = (context["source_id"], context["run_id"])
            previous = self._order.get(key)
            if previous is not None and (context["sequence"] <= previous[0] or context["event_time_ms"] < previous[1]):
                raise ScenarioError("input_order", "Sequence must increase and event time must not decrease within one source/run")
            next_order = (context["sequence"], context["event_time_ms"])
            frame_id = inputs["frame"]["frame_id"]
            seen = self._seen_frames.get(key, set())
            if frame_id in seen:
                raise ScenarioError("input_order", "frame_id must be unique within the source/run until reset")
            if len(seen) >= 100000:
                raise ScenarioError("input_contract", "Reference executor limit: at most 100000 frames per source/run")
            trusted_context = json_copy(context, code="input_contract")
            trusted_inputs = json_copy(inputs, code="input_contract")
            state = json_copy(self._states.get(key, manifest["state"]["initial"]), code="state_contract")
            execution_lease = self._authorize(before_process=True)
            if self._hardware is not None:
                hardware_token = self._hardware.begin_call()
            self._metrics["process_calls"] += 1
            start = perf_counter()
            services = None
            try:
                services = CallServices(self._host, manifest, trusted_inputs["frame"], trusted_context)
                try:
                    raw = self._scenario.process(inputs, context, state, services)
                except ScenarioError as exc:
                    recorded = services._failure_fields
                    if recorded is not None:
                        code, message, stage = recorded
                        if code == "authorization_failed":
                            raise ScenarioError(code, message, stage=stage, fatal=False, retryable=True) from exc
                        raise ScenarioError(code, message, stage=stage, fatal=True, retryable=False) from exc
                    raise
                finally:
                    services.close()
                if services._failure is not None:
                    if services._failure_fields[0] == "authorization_failed":
                        code, message, stage = services._failure_fields
                        raise ScenarioError(code, message, stage=stage, fatal=False, retryable=True) from services._failure
                    raise ScenarioError("service_failed", "A host service failed during the call; results cannot be committed", fatal=True) from services._failure
            except ScenarioError as exc:
                if exc.code == "authorization_failed" and services is not None and services._failure_fields is not None and services._failure_fields[0] == "authorization_failed":
                    raise ScenarioError(exc.code, exc.message, stage="security", fatal=False, retryable=True) from exc
                self._failed = True
                # Publisher Python cannot make a failed process retryable by
                # changing the public fields of an exception before rethrowing.
                raise ScenarioError(exc.code, exc.message, stage=exc.stage, fatal=True, retryable=False) from exc
            except BaseException as exc:
                self._failed = True
                raise ScenarioError("scenario_exception", f"Scenario process failed: {type(exc).__name__}: {exc}", fatal=True) from exc
            elapsed_ms = (perf_counter() - start) * 1000
            if elapsed_ms > limits["max_process_ms"]:
                self._failed = True
                raise ScenarioError("process_timeout", f"Process returned after {elapsed_ms:.3f} ms; budget is {limits['max_process_ms']} ms; no state committed", fatal=True)
            try:
                returned_inputs = json_copy(inputs, code="input_mutation", max_bytes=limits["max_input_bytes"])
                returned_context = json_copy(context, code="input_mutation", max_bytes=limits["max_input_bytes"])
                if not same_json(returned_inputs, trusted_inputs) or not same_json(returned_context, trusted_context):
                    raise ScenarioError("input_mutation", "Scenario changed read-only inputs or context")
            except ScenarioError as exc:
                self._failed = True
                raise ScenarioError("input_mutation", exc.message, fatal=True) from exc
            try:
                if type(raw) is not dict or set(raw) != {"events", "state"}:
                    raise ScenarioError("event_contract", "process must return exactly event candidates and state")
                raw = json_copy(raw, code="event_contract",
                                max_bytes=limits["max_state_bytes"] + limits["max_event_bytes"] * limits["max_events_per_call"] + 128)
                if _contains_frame_ref(raw, trusted_inputs["frame"]["frame_ref"]):
                    raise ScenarioError("event_contract", "An ephemeral frame_ref may not be returned in state or events")
                events = materialize_events(raw["events"], manifest, trusted_context)
                candidate = validate_value(raw["state"], manifest["state"]["schema"], code="state_contract", max_bytes=limits["max_state_bytes"])
            except ScenarioError as exc:
                self._failed = True
                raise ScenarioError(exc.code, exc.message, stage=exc.stage, fatal=True) from exc
            # The license may expire while synchronous scenario code is running.
            # Re-check immediately before committing either state or ordering.
            if self._authorized is not None:
                try:
                    execution_lease.assert_current()
                except Exception as exc:
                    raise ScenarioError("authorization_failed", f"The original execution lease is no longer valid: {exc}", retryable=True) from exc
            if self._hardware is not None:
                self._hardware.assert_current()
            self._states[key] = json_copy(candidate, code="state_contract")
            self._order[key] = next_order
            self._seen_frames.setdefault(key, set()).add(frame_id)
            self._metrics["committed_calls"] += 1
            return {"events": events, "state": json_copy(candidate, code="state_contract")}
        finally:
            if hardware_token is not None:
                self._hardware.end_call(hardware_token)
            self._busy.release()

    def reset(self, source_id=None, run_id=None):
        self._healthy()
        if source_id is not None and (type(source_id) is not str or not 1 <= len(source_id) <= 256):
            raise ScenarioError("input_contract", "reset source_id must be a nonempty string or None")
        if run_id is not None and (source_id is None or type(run_id) is not str or not 1 <= len(run_id) <= 256):
            raise ScenarioError("input_contract", "reset run_id requires a source_id and must be a nonempty string")
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Cannot reset an executor during process")
        try:
            self._healthy()
            if source_id is None:
                self._states.clear()
                self._order.clear()
                self._seen_frames.clear()
            else:
                keys = set(self._states) | set(self._order)
                for key in keys:
                    if key[0] == source_id and (run_id is None or key[1] == run_id):
                        self._states.pop(key, None)
                        self._order.pop(key, None)
                        self._seen_frames.pop(key, None)
            self._metrics["reset_calls"] += 1
        finally:
            self._busy.release()

    def close(self):
        if self._closed:
            return
        if not self._busy.acquire(blocking=False):
            raise ScenarioError("lifecycle", "Cannot close an executor during process")
        try:
            self._closed = True
            self._metrics["close_calls"] += 1
            cleanup = getattr(self._scenario, "close", None)
            try:
                if callable(cleanup):
                    self._metrics["scenario_close_called"] = True
                    try:
                        cleanup()
                    except Exception as exc:
                        self._failed = True
                        raise ScenarioError("scenario_exception", f"Scenario cleanup failed: {exc}", fatal=True) from exc
            finally:
                self._scenario = None
                if self._module_cm is not None:
                    self._module_cm.__exit__(None, None, None)
                    self._module_cm = None
                self._states.clear()
                self._order.clear()
                self._seen_frames.clear()
        finally:
            self._busy.release()

    def report(self):
        from . import __version__

        delivery = {"policy": "development", "protected": False, "external_delivery": False}
        if self._authorized is not None:
            delivery = {"policy": "protected", "protected": True,
                        "claims": json_copy(self._authorized.claims, code="policy_rejected"),
                        "package_sha256": self._authorized.package_sha256,
                        "auto_renew_before_process": True, "immutable_execution_lease_required": True,
                        "external_delivery": True}
        return {"executor": "vlsp-services-v02", "executor_version": __version__,
                "manifest_sha256": self._package.manifest_sha256,
                "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                            "dependencies": {item["name"]: metadata.version(item["name"]) for item in self._package.manifest["dependencies"]}},
                "delivery": delivery, "model_bindings": self._package.model_links,
                "hardware_bindings": None if self._hardware is None else json_copy(self._hardware.selection, code="hardware_rejected"),
                "hardware_artifact_bytes_hashed_before_factory": self._hardware is not None,
                "hardware_device_placement_verified_by_executor": False,
                "model_package_bytes_verified_by_scenario_executor": False,
                "lifecycle": {**self._metrics, "closed": self._closed, "failed": self._failed},
                "limits": {"deadline": "checked-after-return; no hard preemption", "max_frames_per_source_run": 100000,
                           **self._package.manifest["limits"]}}

    def __enter__(self):
        self._healthy()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"Scenario cleanup also failed: {cleanup_error}")
        return False
