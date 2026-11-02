"""Independent synchronous VLSP 0.2 host, implemented from the public contract.

This is trusted-code execution, not a Python sandbox. No reference executor or
VideoPipe runtime is imported. Formal schemas and security leases are shared.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import struct
import sys
import tempfile
import threading
import time
import uuid

from jsonschema import Draft202012Validator, validators
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


_ERROR_ALIASES = {
    "package_contract": "manifest_invalid", "package_inventory": "inventory_mismatch",
    "unsupported_capability": "capability_missing", "dependency_unavailable": "dependency_missing",
    "parameter_contract": "parameter_invalid", "scenario_load": "scenario_exception",
    "authorization": "authorization_failed", "service_error": "service_failed",
    "output_contract": "event_contract", "scenario_close": "scenario_exception",
}
_STAGES = {
    "manifest_invalid": "manifest", "version_unsupported": "manifest",
    "capability_missing": "requirements", "dependency_missing": "requirements",
    "dependency_mismatch": "requirements", "model_link_mismatch": "requirements",
    "path_unsafe": "integrity", "file_missing": "integrity", "digest_mismatch": "integrity",
    "inventory_mismatch": "integrity", "parameter_invalid": "parameters",
    "input_contract": "input", "input_order": "input", "input_mutation": "input",
    "event_contract": "output", "state_contract": "state", "process_timeout": "execution",
    "scenario_exception": "execution", "service_failed": "execution", "service_contract": "execution",
    "lifecycle": "lifecycle", "golden_mismatch": "golden", "policy_rejected": "security",
    "authorization_failed": "security", "hardware_rejected": "requirements",
}


class Fault(Exception):
    def __init__(self, code, message, stage="process", fatal=True, retryable=False):
        super().__init__(message)
        code = _ERROR_ALIASES.get(code, code)
        stage = _STAGES.get(code, stage)
        self.code, self.message, self.stage = code, str(message), stage
        self.fatal, self.retryable = bool(fatal), bool(retryable)

    def to_dict(self):
        return {"stage": self.stage, "code": self.code, "message": self.message,
                "fatal": self.fatal, "retryable": self.retryable}


_CAPABILITIES = frozenset({
    "python-scenario-services-json-v1", "frame-ref-v1", "model-inference-v1",
    "pure-tracker-v1", "event-json-v1", "source-run-state-v1",
    "event-id-sha256-v1",
})


def _json_check(value):
    """Reject Python-only values, nonfinite numbers and non-scalar Unicode."""
    kind = type(value)
    if kind is str:
        value.encode("utf-8", "strict")
    elif kind is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
    elif kind in (int, bool, type(None)):
        pass
    elif kind is list:
        for item in value:
            _json_check(item)
    elif kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _json_check(key)
            _json_check(item)
    else:
        raise ValueError("not a JSON value: " + kind.__name__)


def _bytes(value):
    _json_check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8", "strict")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _load(raw):
    value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                       parse_constant=lambda token: (_ for _ in ()).throw(
                           ValueError("nonfinite JSON number: " + token)))
    _json_check(value)
    return value


_TYPES = Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda checker, value: type(value) is int,
    "number": lambda checker, value: type(value) in (int, float) and (
        type(value) is int or math.isfinite(value)),
    "object": lambda checker, value: type(value) is dict,
    "array": lambda checker, value: type(value) is list,
})
_Validator = validators.extend(Draft202012Validator, type_checker=_TYPES)


def _validate(value, schema, code, stage, fatal=True):
    try:
        _json_check(value)
        error = next(_Validator(schema).iter_errors(value), None)
        if error is not None:
            location = "/".join(str(x) for x in error.absolute_path)
            raise ValueError((location + ": " if location else "") + error.message)
    except Fault:
        raise
    except Exception as exc:
        raise Fault(code, str(exc), stage, fatal) from exc


def _schema(schema):
    def visit(value):
        if type(value) is dict:
            if any(key in value for key in ("$ref", "$dynamicRef", "$id")):
                raise ValueError("embedded schemas must be self-contained without ref/id")
            for item in value.values():
                visit(item)
        elif type(value) is list:
            for item in value:
                visit(item)
    visit(schema)
    Draft202012Validator.check_schema(schema)


def _digest(parts, prefix):
    encoded = bytearray(prefix + b"\x00")
    for part in parts:
        raw = part.encode("utf-8", "strict")
        if len(raw) >= 2 ** 32:
            raise ValueError("identity field exceeds uint32 byte length")
        encoded.extend(struct.pack(">I", len(raw)))
        encoded.extend(raw)
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _field(binding, name, default=None):
    return binding.get(name, default) if type(binding) is dict else getattr(binding, name, default)


def _bbox(box):
    return (type(box) is list and len(box) == 4 and
            all(type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1 for x in box)
            and box[0] < box[2] and box[1] < box[3])


def _contains(value, forbidden):
    if type(value) is str:
        return value == forbidden
    if type(value) is list:
        return any(_contains(item, forbidden) for item in value)
    if type(value) is dict:
        return any(key == "frame_ref" or key == forbidden or _contains(item, forbidden)
                   for key, item in value.items())
    return False


class _Services:
    """A new scope is created for every process; errors remain sticky."""

    def __init__(self, engine, frame, context):
        self.engine = engine
        self.frame, self.context = copy.deepcopy(frame), copy.deepcopy(context)
        self.active, self._error = True, None
        self.model_calls = self.tracker_calls = 0
        self.objects, self.raw_objects = {}, {}

    def _fail(self, message, code="service_contract"):
        fault = Fault(code, message, "service", True, False)
        self._remember(fault)
        raise fault

    def _remember(self, fault):
        if self._error is None:
            self._error = (fault.code, fault.message, fault.stage)

    @property
    def error(self):
        # A scenario may catch and edit an exception object. Never retain that
        # mutable object as the authoritative record of a service failure.
        return Fault(*self._error, fatal=True, retryable=False) if self._error is not None else None

    def _start(self, kind):
        if not self.active:
            self._fail("service scope is closed")
        if self.error is not None:
            raise self.error
        counter = kind + "_calls"
        setattr(self, counter, getattr(self, counter) + 1)
        if getattr(self, counter) > self.engine.manifest["limits"]["max_" + counter]:
            self._fail(kind + " call limit exceeded")
        self.engine._deadline()

    def _shape(self, value, name):
        try:
            _validate(value, self.engine.service_schemas[name], "service_contract", "service")
        except Exception as exc:
            self._fail(str(exc))

    def infer(self, alias, request):
        try:
            return self._infer(alias, request)
        except Fault as exc:
            self._remember(exc)
            raise
        except Exception as exc:
            self._fail(str(exc), "service_error")

    def _infer(self, alias, request):
        self._start("model")
        if type(alias) is not str or alias not in self.engine.model_specs:
            self._fail("undeclared model alias")
        binding = self.engine.models[alias]
        if binding is None:
            self._fail("model dependency unavailable: " + alias)
        self._shape(request, "infer_request")
        if (request["frame_id"] != self.frame["frame_id"] or
                request["frame_ref"] != self.frame["frame_ref"]):
            self._fail("model request does not identify the current frame")
        kind = self.engine.model_specs[alias]["kind"]
        if kind == "detection":
            if request["object_id"] is not None or request["bbox"] is not None:
                self._fail("detection requires null object_id and bbox")
        else:
            oid, crop = request["object_id"], request["bbox"]
            if oid is None or oid not in self.objects or not _bbox(crop):
                self._fail("classification requires a registered current-frame object")
            full = self.objects[oid]["bbox"]
            if not (full[0] <= crop[0] < crop[2] <= full[2] and
                    full[1] <= crop[1] < crop[3] <= full[3]):
                self._fail("classification crop lies outside its detection")
        try:
            response = _field(binding, "infer")(copy.deepcopy(request), copy.deepcopy(self.context))
        except Exception as exc:
            self._fail("model execution failed: " + str(exc), "service_error")
        self.engine._deadline()
        payload_key = "detections" if kind == "detection" else "classification"
        if type(response) is not dict or set(response) != {payload_key}:
            self._fail("model provider must return exactly " + payload_key)
        response = {"model_alias": alias, "frame_id": request["frame_id"],
                    "object_id": request["object_id"], "kind": kind, **response}
        self._shape(response, kind + "_response")
        if (response["model_alias"] != alias or response["frame_id"] != self.frame["frame_id"] or
                response["object_id"] != request["object_id"]):
            self._fail("model response correlation mismatch")
        response = copy.deepcopy(response)
        if kind == "detection":
            seen = set()
            for item in response["detections"]:
                raw_id = item["object_id"]
                if raw_id in seen:
                    self._fail("duplicate detector raw object_id")
                seen.add(raw_id)
                if not _bbox(item["bbox"]):
                    self._fail("invalid detector xyxy bbox")
                key = (alias, raw_id)
                previous = self.raw_objects.get(key)
                if previous is not None and _bytes(previous) != _bytes(item):
                    self._fail("conflicting repeated detector object_id")
                oid = _digest((alias, raw_id), b"VLSP-OBJECT-V1")
                mapped = dict(item, object_id=oid)
                if oid in self.objects and (previous is None or _bytes(self.objects[oid]) != _bytes(mapped)):
                    self._fail("namespaced detector object_id collision")
                self.raw_objects[key] = copy.deepcopy(item)
                self.objects[oid] = copy.deepcopy(mapped)
                item["object_id"] = oid
        return response

    def track(self, alias, request, tracker_state):
        try:
            return self._track(alias, request, tracker_state)
        except Fault as exc:
            self._remember(exc)
            raise
        except Exception as exc:
            self._fail(str(exc), "service_error")

    def _track(self, alias, request, tracker_state):
        self._start("tracker")
        if type(alias) is not str or alias not in self.engine.tracker_specs:
            self._fail("undeclared tracker alias")
        binding = self.engine.trackers[alias]
        if binding is None:
            self._fail("tracker dependency unavailable: " + alias)
        self._shape(request, "tracker_request")
        if request["frame_id"] != self.frame["frame_id"]:
            self._fail("tracker request does not identify the current frame")
        if type(tracker_state) is not dict:
            self._fail("tracker state must be a JSON object")
        if len(_bytes(tracker_state)) > self.engine.manifest["limits"]["max_state_bytes"]:
            self._fail("tracker state exceeds byte limit")
        selected = {}
        for item in request["detections"]:
            oid = item["object_id"]
            if oid in selected or oid not in self.objects or _bytes(self.objects[oid]) != _bytes(item):
                self._fail("tracker detection is duplicate, unknown or altered")
            selected[oid] = item
        try:
            response = _field(binding, "track")(
                copy.deepcopy(request), copy.deepcopy(tracker_state), copy.deepcopy(self.context))
        except Exception as exc:
            self._fail("tracker execution failed: " + str(exc), "service_error")
        self.engine._deadline()
        if type(response) is not dict or set(response) != {"tracks", "state"}:
            self._fail("tracker provider must return exactly tracks and state")
        response = {"tracker_alias": alias, "frame_id": request["frame_id"], **response}
        self._shape(response, "tracker_response")
        if response["tracker_alias"] != alias or response["frame_id"] != self.frame["frame_id"]:
            self._fail("tracker response correlation mismatch")
        if len(_bytes(response["state"])) > self.engine.manifest["limits"]["max_state_bytes"]:
            self._fail("candidate tracker state exceeds byte limit")
        objects, tracks = set(), set()
        for observation in response["tracks"]:
            oid, tid = observation["object_id"], observation["track_id"]
            if oid not in selected or oid in objects or tid in tracks:
                self._fail("tracker returned unknown object_id or duplicate assignment")
            if not _bbox(observation["bbox"]):
                self._fail("invalid tracker xyxy bbox")
            objects.add(oid)
            tracks.add(tid)
        return copy.deepcopy(response)


class Engine:
    """Load one immutable package and own all state for a Scenario instance."""

    def __init__(self, root: Path, providers, authorization=None, parameters=None):
        self.authorization = authorization
        self._lock = threading.RLock()
        self._state, self._order, self._seen = {}, {}, {}
        self._failed = self._closed = self._busy = False
        self._process_calls = self._committed_calls = self._reset_calls = self._close_calls = 0
        self._scenario_close_called = False
        self._scenario, self._temporary, self._module_prefix = None, None, None
        self._started = None
        self.root = Path(root)
        try:
            if authorization is not None:
                self._authorize(authorization.assert_current)
            files = self._read_package(self.root)
            manifest_raw = files.pop("scenario-package.json")
            self.manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
            self.manifest = _load(manifest_raw)
            spec_root = Path(__file__).resolve().parents[2] / "scenario" / "spec"
            manifest_schema = _load((spec_root / "scenario-package.schema.json").read_bytes())
            _validate(self.manifest, manifest_schema, "package_contract", "manifest")
            self.context_schema = _load((spec_root / "schemas/context.schema.json").read_bytes())
            self.event_schema = _load((spec_root / "schemas/event.schema.json").read_bytes())
            services = _load((spec_root / "schemas/services.schema.json").read_bytes())
            self.service_schemas = {
                name: {"$defs": services["$defs"], "$ref": "#/$defs/" + name}
                for name in services["$defs"]
            }
            self.input_schema = {"$defs": services["$defs"], "type": "object",
                                 "required": ["frame"], "additionalProperties": False,
                                 "properties": {"frame": {"$ref": "#/$defs/frame"}}}
            self._preflight(files, providers, parameters)
            if authorization is not None:
                claims = authorization.claims
                if (claims.get("manifest_sha256") != self.manifest_sha256 or
                        claims.get("package_format") != "vl.scenario-package" or
                        claims.get("manifest_path") != "scenario-package.json"):
                    raise Fault("authorization", "license does not identify this manifest", "authorization")
                self._authorize(authorization.assert_current)
            self._import(files, manifest_raw)
        except Fault as exc:
            self._dispose()
            raise Fault(exc.code, exc.message, exc.stage, False, False) from exc
        except Exception as exc:
            self._dispose()
            raise Fault("package_contract", str(exc), "load", False, False) from exc

    @staticmethod
    def _read_package(root):
        if root.is_symlink() or not root.is_dir():
            raise Fault("package_inventory", "package root must be a real directory", "inventory")
        files = {}
        for base, directories, names in os.walk(root, followlinks=False):
            for name in directories + names:
                path = Path(base) / name
                if path.is_symlink():
                    raise Fault("package_inventory", "symlink in package", "inventory")
            for name in names:
                path = Path(base) / name
                if not path.is_file():
                    raise Fault("package_inventory", "nonregular package file", "inventory")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        if "scenario-package.json" not in files:
            raise Fault("package_inventory", "manifest missing", "inventory")
        return files

    def _preflight(self, files, providers, parameters):
        manifest = self.manifest
        declared = manifest["files"]
        if set(files) != set(declared):
            raise Fault("package_inventory", "package files do not match complete inventory", "inventory")
        for name, digest in declared.items():
            path = PurePosixPath(name)
            if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
                raise Fault("path_unsafe", "unsafe package path", "inventory")
            if hashlib.sha256(files[name]).hexdigest() != digest:
                raise Fault("digest_mismatch", "file digest mismatch: " + name, "inventory")
        entry_root = manifest["entrypoint"]["root"]
        for leaf in ("__init__.py", "scenario.py"):
            if entry_root + "/" + leaf not in files:
                raise Fault("package_inventory", "entrypoint missing " + leaf, "inventory")
        for sequence in manifest["golden"]["sequences"]:
            if sequence["path"] not in files:
                raise Fault("package_inventory", "golden file missing from inventory", "inventory")
        if len({item["id"] for item in manifest["golden"]["sequences"]}) != len(manifest["golden"]["sequences"]):
            raise Fault("package_contract", "duplicate golden sequence ID", "manifest")
        if set(manifest["required_capabilities"]) - _CAPABILITIES:
            raise Fault("unsupported_capability", "unknown required capability", "capability")
        if any(extension["required"] for extension in manifest["extensions"].values()):
            raise Fault("unsupported_capability", "unknown required extension", "capability")
        try:
            if Version(platform.python_version()) not in SpecifierSet(manifest["runtime"]["python_version_specifier"]):
                raise ValueError("Python version does not satisfy package runtime")
            dependency_names = set()
            for dependency in manifest["dependencies"]:
                name = dependency["name"].lower().replace("_", "-").replace(".", "-")
                if name in dependency_names:
                    raise ValueError("duplicate Python dependency")
                dependency_names.add(name)
                if Version(importlib.metadata.version(dependency["name"])) not in SpecifierSet(dependency["version_specifier"]):
                    raise ValueError("Python dependency version mismatch: " + dependency["name"])
        except (ValueError, InvalidVersion, InvalidSpecifier, importlib.metadata.PackageNotFoundError) as exc:
            raise Fault("dependency_unavailable", str(exc), "dependencies") from exc
        self.event_types = {}
        try:
            for declaration in manifest["event_contract"]["types"]:
                name = declaration["event_type"]
                if name in self.event_types:
                    raise ValueError("duplicate event_type")
                _schema(declaration["payload_schema"])
                self.event_types[name] = declaration["payload_schema"]
            _schema(manifest["state"]["schema"])
        except Exception as exc:
            raise Fault("package_contract", str(exc), "schema") from exc
        _validate(manifest["state"]["initial"], manifest["state"]["schema"], "package_contract", "initial_state")
        if len(_bytes(manifest["state"]["initial"])) > manifest["limits"]["max_state_bytes"]:
            raise Fault("state_contract", "initial state exceeds byte limit", "initial_state")
        self.parameters = {}
        overrides = {} if parameters is None else parameters
        if type(overrides) is not dict or set(overrides) - set(manifest["parameters"]):
            raise Fault("parameter_contract", "unknown or malformed parameter override", "parameters")
        for name, definition in manifest["parameters"].items():
            schema = {key: value for key, value in definition.items() if key != "default"}
            _validate(definition["default"], schema, "parameter_contract", "parameters")
            value = overrides.get(name, definition["default"])
            _validate(value, schema, "parameter_contract", "parameters")
            self.parameters[name] = copy.deepcopy(value)
        self.model_specs, self.models = self._bindings(manifest["models"], _field(providers, "models", {}), "model")
        self.tracker_specs, self.trackers = self._bindings(manifest["trackers"], _field(providers, "trackers", {}), "tracker")

    @staticmethod
    def _bindings(declarations, supplied, kind):
        specs, bindings = {}, {}
        if not isinstance(supplied, dict):
            raise Fault("dependency_unavailable", "provider bindings must be dictionaries", "dependencies")
        for declaration in declarations:
            alias = declaration["alias"]
            if alias in specs:
                raise Fault("package_contract", "duplicate " + kind + " alias", "manifest")
            specs[alias] = declaration
            binding = supplied.get(alias)
            identity_fields = ("kind", "manifest_sha256", "profile") if kind == "model" else ("implementation", "version")
            usable = (binding is not None and _field(binding, "available", True) is True and
                      all(_field(binding, key) == declaration[key] for key in identity_fields) and
                      callable(_field(binding, "infer" if kind == "model" else "track")))
            if not usable and declaration["required"]:
                code = "dependency_missing" if binding is None or _field(binding, "available", True) is not True else (
                    "model_link_mismatch" if kind == "model" else "dependency_mismatch")
                raise Fault(code, "required " + kind + " unavailable or incompatible: " + alias, "dependencies")
            bindings[alias] = binding if usable else None
        return specs, bindings

    def _import(self, files, manifest_raw):
        self._temporary = tempfile.mkdtemp(prefix="vlsp-independent-")
        temporary = Path(self._temporary)
        for name, raw in dict(files, **{"scenario-package.json": manifest_raw}).items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
            path.chmod(0o400)
        for base, directories, names in os.walk(temporary, topdown=False):
            Path(base).chmod(0o500 if Path(base) != temporary else 0o700)
        code_root = temporary / self.manifest["entrypoint"]["root"]
        self._module_prefix = "_vlsp_independent_" + uuid.uuid4().hex
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            name = self._module_prefix
            spec = importlib.util.spec_from_file_location(name, code_root / "__init__.py", submodule_search_locations=[str(code_root)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[name] = package
            spec.loader.exec_module(package)
            scenario_spec = importlib.util.spec_from_file_location(name + ".scenario", code_root / "scenario.py")
            module = importlib.util.module_from_spec(scenario_spec)
            sys.modules[scenario_spec.name] = module
            scenario_spec.loader.exec_module(module)
            self._scenario = module.Scenario(**copy.deepcopy(self.parameters))
            if not callable(getattr(self._scenario, "process", None)):
                raise TypeError("Scenario.process is not callable")
        except Exception as exc:
            raise Fault("scenario_load", str(exc), "import") from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode

    def _authorize(self, callback):
        try:
            return callback()
        except Exception as exc:
            raise Fault("authorization", str(exc), "authorization", False, True) from exc

    def _deadline(self):
        if self._started is not None and (time.monotonic() - self._started) * 1000 > self.manifest["limits"]["max_process_ms"]:
            raise Fault("process_timeout", "synchronous process exceeded max_process_ms", "deadline")

    def _usable(self):
        if self._closed:
            raise Fault("lifecycle", "instance is closed", "lifecycle", False, False)
        if self._failed:
            raise Fault("lifecycle", "instance failed", "lifecycle", True, False)
        if self._busy:
            raise Fault("lifecycle", "instance is already processing", "lifecycle", False, False)

    def process(self, inputs, context):
        with self._lock:
            self._process_calls += 1
            self._usable()
            _validate(context, self.context_schema, "input_contract", "input", False)
            _validate(inputs, self.input_schema, "input_contract", "input", False)
            try:
                input_bytes, context_bytes = _bytes(inputs), _bytes(context)
            except Exception as exc:
                raise Fault("input_contract", "input/context cannot be encoded: " + str(exc), "input", False, False) from exc
            if max(len(input_bytes), len(context_bytes)) > self.manifest["limits"]["max_input_bytes"]:
                raise Fault("input_contract", "input exceeds max_input_bytes", "input", False, False)
            source, run = context["source_id"], context["run_id"]
            key = source, run
            previous = self._order.get(key)
            if previous is not None and (context["sequence"] <= previous[0] or context["event_time_ms"] < previous[1]):
                raise Fault("input_order", "sequence must increase and UTC event time cannot decrease", "input", False, False)
            if inputs["frame"]["frame_id"] in self._seen.get(key, set()):
                raise Fault("input_order", "frame_id already committed in this source/run", "input", False, False)
            service = None
            self._busy = True
            try:
                lease = self._authorize(self.authorization.before_process) if self.authorization is not None else None
                if self.authorization is not None and not callable(getattr(lease, "assert_current", None)):
                    raise Fault("authorization", "before_process did not return an ExecutionLease", "authorization", False, True)
                safe_inputs, safe_context = copy.deepcopy(inputs), copy.deepcopy(context)
                baseline_inputs, baseline_context = input_bytes, context_bytes
                state = copy.deepcopy(self._state.get(key, self.manifest["state"]["initial"]))
                service = _Services(self, inputs["frame"], context)
                self._started = time.monotonic()
                try:
                    raw = self._scenario.process(safe_inputs, safe_context, state, service)
                except Exception as exc:
                    if service.error is not None:
                        raise service.error from exc
                    raise Fault("scenario_exception", str(exc), "process") from exc
                self._deadline()
                if service.error is not None:
                    raise Fault("service_failed", "A host service failed during the call", "execution", True, False)
                try:
                    mutated = _bytes(safe_inputs) != baseline_inputs or _bytes(safe_context) != baseline_context
                except Exception:
                    mutated = True
                if mutated:
                    raise Fault("input_mutation", "Scenario modified inputs or context", "output")
                if type(raw) is not dict or set(raw) != {"events", "state"} or type(raw["events"]) is not list or type(raw["state"]) is not dict:
                    raise Fault("output_contract", "expected exactly events:list and state:object", "output")
                _validate(raw["state"], self.manifest["state"]["schema"], "state_contract", "state")
                limits = self.manifest["limits"]
                if len(_bytes(raw["state"])) > limits["max_state_bytes"]:
                    raise Fault("state_contract", "candidate state exceeds limit", "state")
                if len(raw["events"]) > limits["max_events_per_call"]:
                    raise Fault("event_contract", "event count exceeds limit", "output")
                events = []
                for ordinal, candidate in enumerate(raw["events"]):
                    if type(candidate) is not dict or set(candidate) != {"event_type", "payload"} or type(candidate["event_type"]) is not str or type(candidate["payload"]) is not dict:
                        raise Fault("output_contract", "malformed event candidate", "output")
                    event_type = candidate["event_type"]
                    if event_type not in self.event_types:
                        raise Fault("output_contract", "undeclared event_type", "output")
                    _validate(candidate["payload"], self.event_types[event_type], "output_contract", "output")
                    if _contains(candidate["payload"], inputs["frame"]["frame_ref"]):
                        raise Fault("output_contract", "frame_ref must not escape in events", "output")
                    event_id = _digest((self.manifest["scenario_id"], self.manifest["version"], source, run,
                                        str(context["sequence"]), event_type, str(ordinal)), b"VLSP-EVENT-V1")
                    envelope = {"event_id": event_id, "event_type": event_type, **context,
                                "payload": copy.deepcopy(candidate["payload"])}
                    _validate(envelope, self.event_schema, "output_contract", "output")
                    if len(_bytes(envelope)) > limits["max_event_bytes"]:
                        raise Fault("event_contract", "full event envelope exceeds byte limit", "output")
                    events.append(envelope)
                self._deadline()
                if lease is not None:
                    self._authorize(lease.assert_current)
                # No external calls occur after this check and before the local commit.
                committed = copy.deepcopy(raw["state"])
                self._state[key] = committed
                self._order[key] = (context["sequence"], context["event_time_ms"])
                self._seen.setdefault(key, set()).add(inputs["frame"]["frame_id"])
                self._committed_calls += 1
                return {"events": events, "state": copy.deepcopy(committed)}
            except Fault as exc:
                if exc.code == "authorization_failed":
                    raise Fault(exc.code, exc.message, exc.stage, False, True) from exc
                # Every failure after entering the scenario/lease path is a
                # fatal host failure; publisher-controlled exception flags do
                # not change the lifecycle policy.
                self._failed = True
                raise Fault(exc.code, exc.message, exc.stage, True, False) from exc
            except Exception as exc:
                self._failed = True
                raise Fault("output_contract", str(exc), "output") from exc
            finally:
                if service is not None:
                    service.active = False
                self._started, self._busy = None, False

    def reset(self, source_id=None, run_id=None):
        with self._lock:
            self._reset_calls += 1
            self._usable()
            if run_id is not None and source_id is None:
                raise Fault("input_contract", "reset run_id requires source_id", "reset", False, False)
            for value in (source_id, run_id):
                if value is not None:
                    _validate(value, {"type": "string", "minLength": 1, "maxLength": 256}, "input_contract", "reset", False)
            for key in list(set(self._state) | set(self._order) | set(self._seen)):
                if (source_id is None or key[0] == source_id) and (run_id is None or key[1] == run_id):
                    self._state.pop(key, None)
                    self._order.pop(key, None)
                    self._seen.pop(key, None)

    def snapshot(self):
        with self._lock:
            state, order = {}, {}
            for (source, run), value in self._state.items():
                state.setdefault(source, {})[run] = copy.deepcopy(value)
            for (source, run), value in self._order.items():
                order.setdefault(source, {})[run] = list(value)
            return {"state": state, "order": order}

    def lifecycle(self):
        with self._lock:
            return {"process_calls": self._process_calls, "committed_calls": self._committed_calls,
                    "reset_calls": self._reset_calls, "close_calls": self._close_calls,
                    "scenario_close_called": self._scenario_close_called,
                    "closed": self._closed, "failed": self._failed}

    def _dispose(self):
        if self._module_prefix:
            for name in list(sys.modules):
                if name == self._module_prefix or name.startswith(self._module_prefix + "."):
                    sys.modules.pop(name, None)
        if self._temporary:
            for base, directories, names in os.walk(self._temporary):
                Path(base).chmod(0o700)
            shutil.rmtree(self._temporary)
            self._temporary = None

    def close(self):
        with self._lock:
            self._close_calls += 1
            if self._closed:
                return
            self._closed = True
            try:
                close = getattr(self._scenario, "close", None)
                if callable(close):
                    self._scenario_close_called = True
                    close()
            except Exception as exc:
                self._failed = True
                raise Fault("scenario_close", str(exc), "close") from exc
            finally:
                self._scenario = None
                self._state.clear()
                self._order.clear()
                self._seen.clear()
                self._dispose()
