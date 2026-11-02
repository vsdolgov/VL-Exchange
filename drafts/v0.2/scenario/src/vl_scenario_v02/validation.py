"""Read-only validation of the scenario package and references."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import platform

from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.utils import canonicalize_name

from .common import package_path, read_json, sha256_file
from .contracts import parameter_values, validate_value, StrictValidator, validate_input, validate_events, SCHEMAS
from .errors import ScenarioError

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "spec" / "scenario-package.schema.json"
CAPABILITIES = frozenset({"python-scenario-services-json-v1", "frame-ref-v1", "model-inference-v1", "pure-tracker-v1", "event-json-v1", "source-run-state-v1", "event-id-sha256-v1"})


@dataclass(frozen=True)
class ValidatedPackage:
    root: Path
    manifest: dict
    manifest_sha256: str
    model_links: list[dict]
    environment_checked: bool

    def to_dict(self):
        return {"status": "pass", "tool": "VL Scenario Package (VLSP)", "format": self.manifest["format"],
                "scenario_id": self.manifest["scenario_id"], "version": self.manifest["version"],
                "manifest_sha256": self.manifest_sha256, "mode": "static",
                "scenario_code_executed": False, "files_verified": len(self.manifest["files"]),
                "environment_checked": self.environment_checked, "model_bindings": self.model_links,
                "model_package_bytes_verified": False,
                "note": "Host binding identity is a trusted host assertion; model bytes and rights are verified by the host separately."}


def _self_contained_schema(schema):
    if isinstance(schema, dict):
        if {"$ref", "$dynamicRef", "$id"} & set(schema):
            raise ScenarioError("manifest_invalid", "Schemas must be self-contained, without references or identifiers")
        for value in schema.values():
            _self_contained_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _self_contained_schema(value)


def _check_inventory(root, manifest, *, manifest_name="scenario-package.json"):
    for relative, expected in manifest["files"].items():
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ScenarioError("manifest_invalid", f"Invalid SHA-256 digest: {relative}")
        if sha256_file(package_path(root, relative)) != expected:
            raise ScenarioError("digest_mismatch", f"SHA-256 mismatch: {relative}")
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ScenarioError("path_unsafe", f"Symlink is not permitted: {relative}")
        if path.is_file() and relative != manifest_name:
            actual.add(relative)
    if actual != set(manifest["files"]):
        raise ScenarioError("inventory_mismatch", f"File inventory differs: unlisted={sorted(actual-set(manifest['files']))}; missing={sorted(set(manifest['files'])-actual)}")
    entry = manifest["entrypoint"]
    for relative in (entry["root"] + "/__init__.py", entry["root"] + "/" + entry["module"]):
        package_path(root, relative)
        if relative not in manifest["files"]:
            raise ScenarioError("inventory_mismatch", f"Entrypoint is not in inventory: {relative}")


def _check_python(root, manifest):
    entry = manifest["entrypoint"]
    path = package_path(root, entry["root"] + "/" + entry["module"])
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise ScenarioError("manifest_invalid", f"Invalid scenario Python source: {exc}") from exc
    forbidden = {"vl_block_package", "vl_scenario_package", "vl_scenario_v02", "nri", "node_red_inference"}
    for relative in manifest["files"]:
        if not relative.endswith(".py"):
            continue
        try:
            checked_tree = ast.parse(package_path(root, relative).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError) as exc:
            raise ScenarioError("manifest_invalid", f"Invalid Python source {relative}: {exc}") from exc
        for node in ast.walk(checked_tree):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            if any(name.split(".")[0] in forbidden for name in names):
                raise ScenarioError("manifest_invalid", "A neutral scenario must not import block/editor or host runtime tooling")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == entry["class"]]
    if len(classes) != 1:
        raise ScenarioError("manifest_invalid", "Exactly one top-level Scenario class is required")
    methods = {node.name: node for node in classes[0].body if isinstance(node, ast.FunctionDef)}
    if not {"__init__", "process"} <= set(methods):
        raise ScenarioError("manifest_invalid", "Scenario must define synchronous __init__ and process")
    process = methods["process"]
    if [arg.arg for arg in process.args.args] != ["self", "inputs", "context", "state", "services"] or process.args.vararg or process.args.kwarg:
        raise ScenarioError("manifest_invalid", "process signature must be (self, inputs, context, state, services)")


def _dependencies(manifest, check_environment):
    try:
        python_constraint = SpecifierSet(manifest["runtime"]["python_version_specifier"])
    except InvalidSpecifier as exc:
        raise ScenarioError("manifest_invalid", "Invalid Python version requirement") from exc
    if check_environment and platform.python_version() not in python_constraint:
        raise ScenarioError("dependency_mismatch", f"Python {platform.python_version()} does not satisfy {python_constraint}")
    seen = set()
    for item in manifest["dependencies"]:
        name = canonicalize_name(item["name"])
        if name in {"vl-block-package", "vl-scenario-package", "vl-scenario-v02", "nri", "node-red-inference"}:
            raise ScenarioError("manifest_invalid", "External scenarios must not require block/editor or host runtime distributions")
        if name in seen:
            raise ScenarioError("manifest_invalid", f"Duplicate dependency: {name}")
        seen.add(name)
        try:
            specifier = SpecifierSet(item["version_specifier"])
        except InvalidSpecifier as exc:
            raise ScenarioError("manifest_invalid", f"Invalid dependency constraint: {name}") from exc
        if check_environment:
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError as exc:
                raise ScenarioError("dependency_missing", f"Dependency not installed: {name}") from exc
            if installed not in specifier:
                raise ScenarioError("dependency_mismatch", f"{name}={installed} does not satisfy {specifier}")


def validate_package(root: str | Path, *, parameters=None, host=None, check_environment=True, allow_async=False, allow_hardware_async=False):
    """Validate the v0.2 package without importing its executable code.

    A host binding's digest is its trusted assertion: the host must verify its
    loaded model package and rights independently before constructing a binding.
    """
    root = Path(root).resolve()
    path = package_path(root, "scenario-package.json")
    manifest = read_json(path)
    if manifest.get("spec_version") != "0.2.0":
        raise ScenarioError("version_unsupported", "This executor implements VLSP v0.2 only; use a v0.1 executor for v0.1 packages")
    schema = read_json(SCHEMA_PATH)
    errors = sorted(StrictValidator(schema).iter_errors(manifest), key=lambda x: x.json_path)
    if errors:
        raise ScenarioError("manifest_invalid", f"{errors[0].json_path}: {errors[0].message}")
    async_name = "org.vlexchange.async-mllm.v1"
    hardware_async_name = "org.vlexchange.hardware-async-models.v1"
    async_enabled = "async-mllm-v1" in manifest["required_capabilities"]
    missing = set(manifest["required_capabilities"]) - (CAPABILITIES | ({"async-mllm-v1"} if allow_async else set()))
    supported_extensions = ({async_name} if allow_async else set()) | ({hardware_async_name} if allow_async and allow_hardware_async else set())
    unknown = [k for k, v in manifest["extensions"].items() if v["required"] and k not in supported_extensions]
    if missing or unknown:
        raise ScenarioError("capability_missing", "Unsupported required capability or extension")
    if allow_async:
        extension = manifest["extensions"].get(async_name)
        if not async_enabled or extension is None or extension["required"] is not True:
            raise ScenarioError("capability_missing", "An async-capable package must require the MLLM capability and extension")
        schema_async = read_json(SCHEMAS / "async-mllm.schema.json")
        extension_schema = {"$defs": schema_async["$defs"], "$ref": "#/$defs/extension"}
        ext_errors = list(StrictValidator(extension_schema).iter_errors(extension["data"]))
        if ext_errors:
            raise ScenarioError("manifest_invalid", f"Invalid async extension: {ext_errors[0].message}")
        aliases = [model["alias"] for model in extension["data"]["models"]]
        if len(aliases) != len(set(aliases)):
            raise ScenarioError("manifest_invalid", "Duplicate asynchronous model aliases")
        hardware_async = manifest["extensions"].get(hardware_async_name)
        if hardware_async is not None:
            if not allow_hardware_async:
                raise ScenarioError("capability_missing", "Hardware-bound async models need an explicit capable host")
            hw_errors = list(StrictValidator(read_json(SCHEMAS / "hardware-async-models.schema.json")).iter_errors(hardware_async))
            if hw_errors:
                raise ScenarioError("manifest_invalid", f"Invalid hardware async extension: {hw_errors[0].message}")
            async_by_alias = {m["alias"]: m for m in extension["data"]["models"]}
            hw_aliases = set()
            for model in hardware_async["data"]["models"]:
                alias = model["alias"]
                if alias in hw_aliases or alias not in async_by_alias or alias in {m["alias"] for m in manifest["models"]}:
                    raise ScenarioError("manifest_invalid", "Duplicate, unknown or conflicting hardware async alias")
                if async_by_alias[alias]["model_digest"] != model["base_manifest_sha256"]:
                    raise ScenarioError("manifest_invalid", "Async VLMP model requires the explicit matching manifest digest")
                hw_aliases.add(alias)
    for key in ("models", "trackers"):
        if len({x["alias"] for x in manifest[key]}) != len(manifest[key]):
            raise ScenarioError("manifest_invalid", f"Duplicate alias in {key}")
    event_types = manifest["event_contract"]["types"]
    if len({item["event_type"] for item in event_types}) != len(event_types):
        raise ScenarioError("manifest_invalid", "Event types must be unique")
    for item in event_types:
        _self_contained_schema(item["payload_schema"])
        Draft202012Validator.check_schema(item["payload_schema"])
    _self_contained_schema(manifest["state"]["schema"])
    Draft202012Validator.check_schema(manifest["state"]["schema"])
    validate_value(manifest["state"]["initial"], manifest["state"]["schema"], code="state_contract", max_bytes=manifest["limits"]["max_state_bytes"])
    _check_inventory(root, manifest)
    _check_python(root, manifest)
    _dependencies(manifest, check_environment)
    parameter_values(manifest, parameters)
    sequence_ids, sequence_paths = set(), set()
    sequence_schema = read_json(SCHEMAS / ("async-sequence.schema.json" if allow_async else "sequence.schema.json"))
    for sequence in manifest["golden"]["sequences"]:
        if sequence["id"] in sequence_ids or sequence["path"] in sequence_paths:
            raise ScenarioError("manifest_invalid", "Golden sequence ids and paths must be unique")
        sequence_ids.add(sequence["id"])
        sequence_paths.add(sequence["path"])
        if sequence["path"] not in manifest["files"] or not sequence["path"].startswith("golden/"):
            raise ScenarioError("inventory_mismatch", "Golden sequences must be listed under golden/")
        fixture = read_json(package_path(root, sequence["path"]))
        fixture_errors = sorted(StrictValidator(sequence_schema).iter_errors(fixture), key=lambda x: x.json_path)
        if fixture_errors:
            raise ScenarioError("manifest_invalid", f"Invalid golden sequence: {fixture_errors[0].json_path}: {fixture_errors[0].message}")
    reports = [{"alias": item["alias"], "manifest_sha256": item["manifest_sha256"],
                "profile": item["profile"], "status": "declared_unverified"} for item in manifest["models"]]
    if host is not None:
        for item in manifest["models"]:
            binding = host.models.get(item["alias"])
            if binding is None:
                if item["required"]:
                    raise ScenarioError("dependency_missing", f"Required model unavailable: {item['alias']}")
                continue
            if (binding.kind != item["kind"] or binding.manifest_sha256 != item["manifest_sha256"]
                    or binding.profile != item["profile"] or not callable(binding.infer)):
                raise ScenarioError("model_link_mismatch", f"Host model binding mismatch: {item['alias']}")
            next(report for report in reports if report["alias"] == item["alias"])["status"] = "host_assertion_verified"
        for item in manifest["trackers"]:
            binding = host.trackers.get(item["alias"])
            if binding is None:
                if item["required"]:
                    raise ScenarioError("dependency_missing", f"Required tracker unavailable: {item['alias']}")
                continue
            if binding.implementation != item["implementation"] or binding.version != item["version"] or not callable(binding.track):
                raise ScenarioError("dependency_mismatch", f"Host tracker binding mismatch: {item['alias']}")
    return ValidatedPackage(root, manifest, sha256_file(path), reports, check_environment)
