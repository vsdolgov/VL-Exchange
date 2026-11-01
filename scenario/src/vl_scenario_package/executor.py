"""Independent scenario orchestration with explicit transactional JSON state."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
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

    def __init__(self, root, *, policy="protected", parameters=None, model_packages=None):
        if policy != "development":
            raise ScenarioError("policy_rejected", "Protected delivery requires ScenarioExecutor.open_protected(..., verifier=...) before importing code")
        self._initialize(root, parameters=parameters, model_packages=model_packages, authorized=None)

    def _initialize(self, root, *, parameters, model_packages, authorized):
        self._authorized = authorized
        self._closed = False
        self._failed = False
        self._busy = Lock()
        self._states = {}
        self._order = {}
        self._module_cm = None
        self._scenario = None
        self._metrics = {"process_calls": 0, "committed_calls": 0, "reset_calls": 0,
                         "close_calls": 0, "scenario_close_called": False}
        self._authorize()
        self._package = validate_package(root, parameters=parameters, model_packages=model_packages, check_environment=True)
        if authorized is not None:
            claims = authorized.claims
            if (claims.get("package_format") != "vl.scenario-package" or claims.get("manifest_path") != "scenario-package.json"
                    or claims.get("manifest_sha256") != self._package.manifest_sha256):
                raise ScenarioError("policy_rejected", "The signed delivery does not license this exact scenario manifest")
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
    def open_protected(cls, envelope_path, *, verifier, parameters=None, model_packages=None):
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
        try:
            with manager as authorized:
                if not callable(getattr(authorized, "assert_current", None)) or not callable(getattr(authorized, "before_process", None)) or not all(hasattr(authorized, field) for field in ("root", "claims", "package_sha256")):
                    raise ScenarioError("policy_rejected", "Verifier did not return the required authorized-package receipt")
                if not re.fullmatch(r"[0-9a-f]{64}", authorized.package_sha256):
                    raise ScenarioError("policy_rejected", "Verifier receipt contains an invalid package digest")
                json_copy(authorized.claims, code="policy_rejected")
                executor = cls.__new__(cls)
                executor._initialize(authorized.root, parameters=parameters,
                                     model_packages=model_packages, authorized=authorized)
                try:
                    yield executor
                finally:
                    executor.close()
        except ScenarioError:
            raise
        except Exception as exc:
            if executor is None:
                raise ScenarioError("authorization_failed", f"Protected source authorization failed: {exc}") from exc
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
            trusted_context = json_copy(context, code="input_contract")
            trusted_inputs = json_copy(inputs, code="input_contract")
            state = json_copy(self._states.get(key, manifest["state"]["initial"]), code="state_contract")
            execution_lease = self._authorize(before_process=True)
            self._metrics["process_calls"] += 1
            start = perf_counter()
            try:
                raw = self._scenario.process(inputs, context, state)
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
            self._states[key] = json_copy(candidate, code="state_contract")
            self._order[key] = next_order
            self._metrics["committed_calls"] += 1
            return {"events": events, "state": json_copy(candidate, code="state_contract")}
        finally:
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
            else:
                keys = set(self._states) | set(self._order)
                for key in keys:
                    if key[0] == source_id and (run_id is None or key[1] == run_id):
                        self._states.pop(key, None)
                        self._order.pop(key, None)
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
        return {"executor": "vlsp-independent-json", "executor_version": __version__,
                "manifest_sha256": self._package.manifest_sha256,
                "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                            "dependencies": {item["name"]: metadata.version(item["name"]) for item in self._package.manifest["dependencies"]}},
                "delivery": delivery, "model_links": self._package.model_links,
                "lifecycle": {**self._metrics, "closed": self._closed, "failed": self._failed},
                "limits": {"deadline": "checked-after-return; no hard preemption", **self._package.manifest["limits"]}}

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
