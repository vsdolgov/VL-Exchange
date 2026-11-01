#!/usr/bin/env python3
"""Independent demonstration host for VL Scenario Package (VLSP) v0.1.

This is a small demonstration host, not a competitor's videoanalytics system.
It imports neither vl_scenario_package nor vl_block_package. Only normative
JSON schemas/test vectors and the separately distributed VL Exchange security
module are shared with the reference implementation.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from jsonschema import Draft202012Validator, validators
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "spec"
FORBIDDEN = ("vl_scenario_package", "vl_block_package", "nri", "vlmodels", "vlmodels_types")
# Preserve the legacy vendor root deny rule without embedding its display name.
LEGACY_VENDOR_ROOT_SHA256 = "752647253d9993907e89d8dedb912d3e202fb0c43dfbfcc8de4bda58589f0234"
CAPABILITIES = {"python-scenario-json-v1", "classification-v1", "event-json-v1",
                "source-run-state-v1", "event-id-sha256-v1"}
StrictValidator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda _checker, value: type(value) is int,
    "number": lambda _checker, value: type(value) in (int, float),
}))


def forbidden_dependency(name):
    if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN):
        return True
    root = name.split(".", 1)[0]
    return hashlib.sha256(root.encode("utf-8", errors="surrogatepass")).hexdigest() == LEGACY_VENDOR_ROOT_SHA256


class HostError(Exception):
    def __init__(self, code, message, *, fatal=False, retryable=False):
        super().__init__(message)
        self.code, self.fatal, self.retryable = code, fatal, retryable
        self.message = message
        self.stage = ("input" if code in {"input_contract", "input_order"} else
                      "output" if code in {"event_contract", "output_contract"} else
                      "state" if code == "state_contract" else "execution")

    def to_dict(self):
        return {"stage": self.stage, "code": self.code, "message": self.message,
                "fatal": self.fatal, "retryable": self.retryable}

    def envelope(self):
        return {"error": {"code": self.code, "fatal": self.fatal, "retryable": self.retryable}}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise HostError("manifest_invalid", "Invalid strict UTF-8 JSON document") from exc


def detached(value, *, code="input_contract", limit=None):
    def native(item):
        if item is None or type(item) in (bool, int):
            return
        if type(item) is str:
            item.encode("utf-8", errors="strict")
        elif type(item) is float and math.isfinite(item):
            return
        elif type(item) is list:
            for child in item:
                native(child)
        elif type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                native(key)
                native(child)
        else:
            raise HostError(code, "Expected finite native JSON values")
    try:
        native(value)
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise HostError(code, "Invalid JSON value") from exc
    if limit is not None and len(raw) > limit:
        raise HostError(code, "JSON byte limit exceeded")
    return json.loads(raw)


def checked(value, schema, code, limit=None):
    value = detached(value, code=code, limit=limit)
    errors = list(StrictValidator(schema).iter_errors(value))
    if errors:
        raise HostError(code, errors[0].message)
    return value


def path_in(root, name):
    if type(name) is not str or not name or "\\" in name or name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")):
        raise HostError("path_unsafe", "Noncanonical relative package path")
    current = Path(root)
    for part in PurePosixPath(name).parts:
        current /= part
        if current.is_symlink():
            raise HostError("path_unsafe", "Package symlinks are not permitted")
    if not current.resolve().is_relative_to(Path(root).resolve()) or not current.is_file():
        raise HostError("file_missing", "Package file is missing or outside the root")
    return current


def inventory(root):
    result = {}
    for file in sorted(Path(root).rglob("*")):
        if file.is_symlink():
            raise HostError("path_unsafe", "Package symlinks are not permitted")
        if file.is_file():
            result[file.relative_to(root).as_posix()] = sha(file)
    return result


def local_schema(schema):
    if type(schema) is dict:
        if {"$ref", "$dynamicRef", "$id"} & set(schema):
            raise HostError("manifest_invalid", "State and payload schemas must be self contained")
        for value in schema.values():
            local_schema(value)
    elif type(schema) is list:
        for value in schema:
            local_schema(value)


def validate(root):
    root = Path(root).resolve()
    manifest = checked(read_json(path_in(root, "scenario-package.json")), read_json(SCHEMA_DIR / "scenario-package.schema.json"), "manifest_invalid")
    if manifest["format"] != "vl.scenario-package" or manifest["spec_version"] != "0.1.0":
        raise HostError("version_unsupported", "Unsupported VLSP format/version")
    actual = inventory(root)
    if set(actual) != set(manifest["files"]) | {"scenario-package.json"}:
        raise HostError("inventory_mismatch", "Manifest file inventory differs")
    for name, expected in manifest["files"].items():
        if sha(path_in(root, name)) != expected:
            raise HostError("digest_mismatch", "Package SHA-256 mismatch")
    if set(manifest["required_capabilities"]) - CAPABILITIES or any(item["required"] for item in manifest["extensions"].values()):
        raise HostError("capability_missing", "Required capability/extension is unsupported")
    for declaration in manifest["parameters"].values():
        checked(declaration["default"], {k: v for k, v in declaration.items() if k != "default"}, "parameter_invalid")
    if not SpecifierSet(manifest["runtime"]["python_version_specifier"]).contains(".".join(map(str, sys.version_info[:3]))):
        raise HostError("dependency_mismatch", "Python version differs from the declared runtime")
    dependencies = set()
    for declaration in manifest["dependencies"]:
        name = declaration["name"].lower().replace("_", "-")
        if name in dependencies or name in {"vl-scenario-package", "vl-block-package", "node-red-inference"}:
            raise HostError("dependency_mismatch", "Duplicate or vendor-runtime dependency")
        dependencies.add(name)
        try:
            version = importlib.metadata.version(declaration["name"])
        except importlib.metadata.PackageNotFoundError as exc:
            raise HostError("dependency_missing", "A declared dependency is unavailable") from exc
        if version not in SpecifierSet(declaration["version_specifier"]):
            raise HostError("dependency_mismatch", "Dependency version differs from the manifest")
    if any(link["required"] for link in manifest["model_links"]):
        raise HostError("dependency_missing", "This demonstration host has no VLMP inference provider")
    for declaration in [manifest["state"]["schema"], *(event["payload_schema"] for event in manifest["event_contract"]["types"])]:
        local_schema(declaration)
        Draft202012Validator.check_schema(declaration)
    checked(manifest["state"]["initial"], manifest["state"]["schema"], "state_contract", manifest["limits"]["max_state_bytes"])
    event_types = [event["event_type"] for event in manifest["event_contract"]["types"]]
    if len(event_types) != len(set(event_types)):
        raise HostError("manifest_invalid", "Duplicate event type declaration")
    entry = manifest["entrypoint"]
    path_in(root, entry["root"] + "/__init__.py")
    for relative in manifest["files"]:
        if not relative.endswith(".py"):
            continue
        tree = ast.parse(path_in(root, relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            if any(forbidden_dependency(name) for name in names):
                raise HostError("dependency_mismatch", "Scenario imports a forbidden vendor runtime")
    source = ast.parse(path_in(root, entry["root"] + "/" + entry["module"]).read_text(encoding="utf-8"))
    classes = [node for node in source.body if isinstance(node, ast.ClassDef) and node.name == entry["class"]]
    if len(classes) != 1:
        raise HostError("manifest_invalid", "Exactly one Scenario class is required")
    methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == "process"]
    if len(methods) != 1 or [arg.arg for arg in methods[0].args.args] != ["self", "inputs", "context", "state"]:
        raise HostError("manifest_invalid", "process must use the synchronous scenario ABI")
    for sequence in manifest["golden"]["sequences"]:
        checked(read_json(path_in(root, sequence["path"])), read_json(SCHEMA_DIR / "schemas/sequence.schema.json"), "manifest_invalid")
    return manifest


def event_identity(fields):
    if len(fields) != 7 or any(type(field) is not str for field in fields):
        raise HostError("event_contract", "Event identity requires exactly seven strings")
    data = bytearray(b"VLSP-EVENT-V1\0")
    for field in fields:
        raw = field.encode("utf-8", errors="strict")
        data.extend(len(raw).to_bytes(4, byteorder="big", signed=False))
        data.extend(raw)
    return "sha256:" + hashlib.sha256(data).hexdigest(), bytes(data).hex()


class DemoHost:
    def __init__(self, root, *, authorization=None, parameters=None):
        self.root = Path(root).resolve()
        self.manifest = validate(self.root)
        self.authorization = authorization
        self.states, self.ordering = {}, {}
        self.closed = False
        self.failed = False
        self.lock = threading.RLock()
        self.namespace = "_independent_vlsp_" + uuid.uuid4().hex
        params = {} if parameters is None else detached(parameters, code="parameter_invalid")
        if type(params) is not dict or set(params) - set(self.manifest["parameters"]):
            raise HostError("parameter_invalid", "Unknown parameter override")
        selected = {}
        for name, declaration in self.manifest["parameters"].items():
            schema = {k: v for k, v in declaration.items() if k != "default"}
            # An override cannot hide an invalid declared default.
            selected[name] = checked(declaration["default"], schema, "parameter_invalid")
            if name in params:
                selected[name] = checked(params[name], schema, "parameter_invalid")
        if authorization is not None:
            authorization.assert_current()
        sys.dont_write_bytecode = True
        code = self.root / self.manifest["entrypoint"]["root"]
        try:
            package_spec = importlib.util.spec_from_file_location(self.namespace, code / "__init__.py", submodule_search_locations=[str(code)])
            package_module = importlib.util.module_from_spec(package_spec)
            sys.modules[self.namespace] = package_module
            package_spec.loader.exec_module(package_module)
            module_spec = importlib.util.spec_from_file_location(self.namespace + ".scenario", code / self.manifest["entrypoint"]["module"])
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_spec.name] = module
            module_spec.loader.exec_module(module)
            if authorization is not None:
                authorization.assert_current()
            self.scenario = getattr(module, self.manifest["entrypoint"]["class"])(**selected)
            if authorization is not None:
                authorization.assert_current()
        except BaseException:
            self.close()
            raise

    def process(self, inputs, context):
        with self.lock:
            self._user_started = False
            try:
                return self._process(inputs, context)
            except Exception as exc:
                if self._user_started:
                    self.failed = True
                    if isinstance(exc, HostError):
                        exc.fatal = True
                raise

    def _process(self, inputs, context):
        with self.lock:
            if self.closed or self.failed:
                raise HostError("lifecycle", "Scenario instance is closed or failed", fatal=True)
            manifest = self.manifest
            context = checked(context, read_json(SCHEMA_DIR / "schemas/context.schema.json"), "input_contract")
            inputs = detached(inputs, limit=manifest["limits"]["max_input_bytes"])
            name = manifest["input_contract"]["name"]
            if type(inputs) is not dict or set(inputs) != {name}:
                raise HostError("input_contract", "Exactly the declared input is required")
            checked(inputs[name], read_json(SCHEMA_DIR / "schemas/classification.schema.json"), "input_contract")
            key = (context["source_id"], context["run_id"])
            previous = self.ordering.get(key)
            if previous is not None and (context["sequence"] <= previous[0] or context["event_time_ms"] < previous[1]):
                raise HostError("input_order", "Sequence/time is not monotonic within this source/run")
            state = detached(self.states.get(key, manifest["state"]["initial"]), code="state_contract", limit=manifest["limits"]["max_state_bytes"])
            lease = self.authorization.before_process() if self.authorization is not None else None
            call_inputs, call_context = detached(inputs), detached(context)
            started = time.perf_counter_ns()
            self._user_started = True
            try:
                raw = self.scenario.process(call_inputs, call_context, state)
            except Exception as exc:
                raise HostError("scenario_exception", f"Scenario failed: {type(exc).__name__}") from exc
            try:
                mutated = detached(call_inputs) != inputs or detached(call_context) != context
            except HostError as exc:
                raise HostError("scenario_exception", "Scenario corrupted inputs/context", fatal=True) from exc
            if mutated:
                raise HostError("scenario_exception", "Scenario mutated inputs/context", fatal=True)
            if (time.perf_counter_ns() - started) / 1_000_000 > manifest["limits"]["max_process_ms"]:
                raise HostError("process_timeout", "Scenario exceeded its declared process budget")
            raw = checked(raw, read_json(SCHEMA_DIR / "schemas/raw-return.schema.json"), "event_contract")
            state = checked(raw["state"], manifest["state"]["schema"], "state_contract", manifest["limits"]["max_state_bytes"])
            if len(raw["events"]) > manifest["limits"]["max_events_per_call"]:
                raise HostError("event_contract", "Too many events returned")
            declared = {event["event_type"]: event["payload_schema"] for event in manifest["event_contract"]["types"]}
            events = []
            for ordinal, candidate in enumerate(raw["events"]):
                event_type = candidate["event_type"]
                if event_type not in declared:
                    raise HostError("event_contract", "Unknown event type")
                payload = checked(candidate["payload"], declared[event_type], "event_contract")
                identity, _ = event_identity([manifest["scenario_id"], manifest["version"], context["source_id"], context["run_id"],
                                               str(context["sequence"]), event_type, str(ordinal)])
                event = {"event_id": identity, "event_type": event_type, **context, "payload": payload}
                events.append(checked(event, read_json(SCHEMA_DIR / "schemas/event.schema.json"), "event_contract", manifest["limits"]["max_event_bytes"]))
            result = checked({"events": events, "state": state}, read_json(SCHEMA_DIR / "schemas/call-result.schema.json"), "output_contract")
            if lease is not None:
                lease.assert_current()  # The exact signed pre-call grant, never a refreshed replacement.
            self.states[key] = state
            self.ordering[key] = (context["sequence"], context["event_time_ms"])
            return detached(result, code="output_contract")

    def reset(self, source_id=None, run_id=None):
        with self.lock:
            if self.closed or self.failed:
                raise HostError("lifecycle", "Scenario instance is closed or failed", fatal=True)
            if run_id is not None and source_id is None:
                raise HostError("input_contract", "run_id reset requires source_id")
            for key in list(self.states):
                if (source_id is None or key[0] == source_id) and (run_id is None or key[1] == run_id):
                    self.states.pop(key, None)
                    self.ordering.pop(key, None)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.states.clear()
        self.ordering.clear()
        try:
            cleanup = getattr(getattr(self, "scenario", None), "close", None)
            if callable(cleanup):
                cleanup()
        finally:
            self.scenario = None
            for name in list(sys.modules):
                if name == self.namespace or name.startswith(self.namespace + "."):
                    del sys.modules[name]

    def snapshot(self):
        result = {"state": {}, "order": {}}
        for (source, run), value in self.states.items():
            result["state"].setdefault(source, {})[run] = detached(value)
        for (source, run), value in self.ordering.items():
            result["order"].setdefault(source, {})[run] = list(value)
        return result


def matches(actual, expected, atol, rtol):
    if type(expected) is dict:
        return type(actual) is dict and set(actual) == set(expected) and all(matches(actual[k], v, atol, rtol) for k, v in expected.items())
    if type(expected) is list:
        return type(actual) is list and len(actual) == len(expected) and all(matches(a, b, atol, rtol) for a, b in zip(actual, expected))
    if type(expected) is int:
        return type(actual) is int and actual == expected
    if type(expected) is float:
        return type(actual) in (int, float) and math.isfinite(actual) and abs(actual - expected) <= atol + rtol * abs(expected)
    return type(actual) is type(expected) and actual == expected


def run_golden(root, *, authorization=None):
    root = Path(root).resolve()
    before = inventory(root)
    manifest = validate(root)
    sequences = []
    for sequence in manifest["golden"]["sequences"]:
        host = DemoHost(root, authorization=authorization)
        checks = []
        try:
            for index, step in enumerate(read_json(path_in(root, sequence["path"]))["steps"]):
                if "reset" in step:
                    host.reset(**step["reset"])
                    checks.append({"index": index, "reset": step["reset"], "snapshot": host.snapshot(), "pass": True})
                    continue
                snapshot_before = host.snapshot()
                no_commit = None
                try:
                    actual = host.process(step["inputs"], step["context"])
                except HostError as exc:
                    actual = exc.envelope()
                    no_commit = host.snapshot() == snapshot_before
                expected = step["expected"]
                checks.append({"index": index, "actual": actual, "expected": expected,
                               **({"no_commit": no_commit} if no_commit is not None else {}),
                               "pass": matches(actual, expected, manifest["golden"]["atol"], manifest["golden"]["rtol"]) and no_commit is not False})
        finally:
            host.close()
        sequences.append({"id": sequence["id"], "fresh_instance": True, "checks": checks,
                          "pass": all(check["pass"] for check in checks)})
    after = inventory(root)
    return {"status": "pass" if before == after and all(s["pass"] for s in sequences) else "fail",
            "executor": "independent-demonstration-host", "format": manifest["format"],
            "manifest_sha256": sha(root / "scenario-package.json"), "sequences": sequences,
            "all_file_hashes_before": before, "all_file_hashes_after": after, "package_unchanged": before == after,
            "license_mode": authorization.claims["mode"] if authorization is not None else "development-plaintext",
            "event_identity_implementation": "independent length-prefixed UTF-8 SHA-256 implementation"}


@contextmanager
def development_unzip(archive):
    with tempfile.TemporaryDirectory(prefix="independent-vlsp-development-") as temp:
        root = Path(temp)
        total, names = 0, set()
        with zipfile.ZipFile(archive) as zipped:
            if len(zipped.infolist()) > 1024:
                raise HostError("archive_limit", "Too many ZIP entries")
            for entry in zipped.infolist():
                name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
                if not name or name.startswith("/") or "\\" in name or any(part in ("", ".", "..") for part in name.split("/")) or name.casefold() in names:
                    raise HostError("path_unsafe", "Unsafe/duplicate ZIP entry")
                names.add(name.casefold())
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise HostError("path_unsafe", "ZIP symlinks/encrypted entries are not accepted")
                total += entry.file_size
                if total > 512 * 1024 * 1024:
                    raise HostError("archive_limit", "Expanded ZIP exceeds the host limit")
                if entry.is_dir():
                    continue
                destination = root.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(zipped.read(entry))
                destination.chmod(0o600)
        yield root


def demonstration(archive):
    from nacl.public import PrivateKey
    from nacl.signing import SigningKey
    from vl_exchange_security import protection
    from vl_exchange_security.protection import AuthorizationError, OnlineAuthorizer, authorized_package, seal_package
    from vl_exchange_security.reference_server import ReferenceLicenseService, make_server
    archive = Path(archive).resolve()
    zip_before = sha(archive)
    vector = read_json(SCHEMA_DIR / "event-id-test-vector.json")
    identity, raw_hex = event_identity(vector["fields"])
    vector_passed = identity == vector["event_id"] and raw_hex == vector["hash_input_hex"]
    if not vector_passed:
        raise HostError("event_contract", "Independent implementation failed the normative event-id test vector")
    issuer, recipient, installation, server_key = SigningKey.generate(), PrivateKey.generate(), SigningKey.generate(), SigningKey.generate()
    now = int(time.time())
    service = ReferenceLicenseService(issuer_verify_key=issuer.verify_key, server_signing_key=server_key, grant_ttl=60)
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    modes = {}
    denials = []
    try:
        with development_unzip(archive) as root:
            modes["development-plaintext"] = run_golden(root)
        with tempfile.TemporaryDirectory(prefix="independent-vlsp-envelopes-") as temp:
            for mode in ("offline", "online"):
                envelope = Path(temp) / mode
                seal_package(archive, envelope, issuer_signing_key=issuer, recipient_public_key=recipient.public_key,
                             installation_verify_key=installation.verify_key, issuer_id="independent-demo-issuer",
                             license_id="independent-demo-" + mode, installation_id="independent-demo-installation",
                             mode=mode, issued_at=now, not_before=now, expires_at=now + 300,
                             manifest_path="scenario-package.json")
                encrypted_before = inventory(envelope)
                authorizer = OnlineAuthorizer(endpoint=f"http://127.0.0.1:{server.server_port}/v1/grant",
                                              server_verify_key=server_key.verify_key, installation_signing_key=installation,
                                              allow_loopback_http=True) if mode == "online" else None
                with authorized_package(envelope, issuer_verify_key=issuer.verify_key, recipient_private_key=recipient,
                                        policy=mode, online_authorizer=authorizer) as authorized:
                    modes[mode] = run_golden(authorized.root, authorization=authorized)
                    plaintext_root = authorized.root
                    modes[mode]["authorized_zip_sha256"] = authorized.package_sha256
                modes[mode]["plaintext_directory_removed"] = not plaintext_root.exists()
                modes[mode]["encrypted_envelope_unchanged"] = encrypted_before == inventory(envelope)
                for name, private, policy, clock in (
                    ("wrong-recipient", PrivateKey.generate(), mode, lambda: now),
                    ("expired", recipient, mode, lambda: now + 301),
                ):
                    opened = False
                    try:
                        with authorized_package(envelope, issuer_verify_key=issuer.verify_key, recipient_private_key=private,
                                                policy=policy, online_authorizer=authorizer, now=clock):
                            opened = True
                    except AuthorizationError as exc:
                        denials.append({"mode": mode, "case": name, "pass": not opened,
                                        "error_code": exc.code, "scenario_constructed": False})
                    else:
                        denials.append({"mode": mode, "case": name, "pass": False})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    forbidden = [name for name in sys.modules if forbidden_dependency(name)]
    zip_after = sha(archive)
    passed = vector_passed and not forbidden and zip_before == zip_after and all(item["status"] == "pass" for item in modes.values()) and all(item["pass"] for item in denials)
    return {
        "status": "pass" if passed else "fail", "host": "independent-demonstration-host",
        "not_a_competitor_system": True, "format": "vl.scenario-package",
        "package_zip_sha256_before": zip_before, "package_zip_sha256_after": zip_after,
        "package_unchanged": zip_before == zip_after, "modes": modes,
        "event_id_test_vector": {"pass": vector_passed, "event_id": identity, "hash_input_hex": raw_hex},
        "authorization_denials_before_scenario_import": denials, "forbidden_runtime_imports": forbidden,
        "runtime": {"python": sys.version.split()[0], "PyNaCl": importlib.metadata.version("PyNaCl"),
                    "jsonschema": importlib.metadata.version("jsonschema")},
        "shared_security_module": {"component": "security-runtime/src/vl_exchange_security/protection.py", "sha256": sha(protection.__file__)},
        "host_source_sha256": sha(__file__),
        "limitations": ["This is an independent demonstration host, not a competitor platform integration.",
                        "Online authorization uses a real signed HTTP exchange with a loopback reference issuer; production TLS/service deployment is not tested.",
                        "Scenario Python is trusted executable code, not a sandbox. The trusted administrator/process can access plaintext.",
                        "Required external model providers are unsupported by this tiny demonstration host; the example consumes canonical Classification input."]
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/independent-host-result.json")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    os.umask(0o077)
    try:
        report = demonstration(args.archive)
    except Exception as exc:
        report = {"status": "fail", "host": "independent-demonstration-host",
                  "error_type": type(exc).__name__, "message": str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": report["status"], "host": report["host"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
