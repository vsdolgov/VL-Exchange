"""Read-only validation of the scenario package and references."""

from __future__ import annotations

import ast
import importlib.util
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
CAPABILITIES = frozenset({"python-scenario-json-v1", "classification-v1", "event-json-v1", "source-run-state-v1", "event-id-sha256-v1"})


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
                "environment_checked": self.environment_checked, "model_links": self.model_links}


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
    forbidden = {"vl_block_package", "vl_scenario_package", "nri", "node_red_inference"}
    for node in ast.walk(tree):
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
    if [arg.arg for arg in process.args.args] != ["self", "inputs", "context", "state"] or process.args.vararg or process.args.kwarg:
        raise ScenarioError("manifest_invalid", "process signature must be (self, inputs, context, state)")


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
        if name in {"vl-block-package", "vl-scenario-package", "nri", "node-red-inference"}:
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


def _model_links(manifest, model_packages):
    supplied = {} if model_packages is None else model_packages
    aliases = {item["alias"] for item in manifest["model_links"]}
    if len(aliases) != len(manifest["model_links"]) or set(supplied) - aliases:
        raise ScenarioError("model_link_mismatch", "Model aliases must be unique and supplied aliases must be declared")
    reports = []
    for link in manifest["model_links"]:
        if link["alias"] not in supplied:
            if link["required"]:
                raise ScenarioError("dependency_missing", f"Required VLMP reference is unresolved: {link['alias']}")
            reports.append({"alias": link["alias"], "status": "unresolved_optional", "inference_executed": False})
            continue
        root = Path(supplied[link["alias"]]).resolve()
        file = package_path(root, "model-package.json")
        if sha256_file(file) != link["manifest_sha256"]:
            raise ScenarioError("model_link_mismatch", f"VLMP manifest digest mismatch: {link['alias']}")
        try:
            linked = read_json(file)
            if (linked.get("format") != "vl.model-package" or linked.get("spec_version") != "0.1.0"
                    or linked.get("result", {}).get("type") != "Classification"):
                raise ValueError("A v0.1 Classification VLMP manifest is required")
            selected = [item for item in linked["profiles"] if item.get("id") == link["profile"]]
            if len(selected) != 1:
                raise ValueError("The referenced profile is not declared exactly once")
            _check_inventory(root, linked, manifest_name="model-package.json")
            weights = selected[0]["weights"]
            if weights not in linked["files"]:
                raise ValueError("Profile weights are absent from inventory")
            package_path(root, weights)
        except Exception as exc:
            raise ScenarioError("model_link_mismatch", f"VLMP reference is invalid: {exc}") from exc
        reports.append({"alias": link["alias"], "status": "digest_and_declared_contract_verified",
                        "manifest_sha256": link["manifest_sha256"], "profile": link["profile"],
                        "inference_executed": False, "onnx_graph_validated": False})
    return reports


def validate_package(root: str | Path, *, parameters: dict | None = None,
                     model_packages: dict | None = None, capabilities=None,
                     check_environment: bool = False) -> ValidatedPackage:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ScenarioError("file_missing", f"Package directory does not exist: {root}")
    path = package_path(root, "scenario-package.json")
    manifest = read_json(path)
    if isinstance(manifest, dict) and manifest.get("spec_version") != "0.1.0":
        raise ScenarioError("version_unsupported", "Only VLSP specification 0.1.0 is supported")
    schema = read_json(SCHEMA_PATH)
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: error.json_path)
    if errors:
        raise ScenarioError("manifest_invalid", f"{errors[0].json_path}: {errors[0].message}")
    available = CAPABILITIES if capabilities is None else CAPABILITIES & set(capabilities)
    missing = set(manifest["required_capabilities"]) - available
    if missing:
        raise ScenarioError("capability_missing", f"Required capabilities unavailable: {sorted(missing)}")
    unsupported = [key for key, value in manifest["extensions"].items() if value["required"]]
    if unsupported:
        raise ScenarioError("capability_missing", f"Required extensions are unsupported: {unsupported}")
    if manifest["input_contract"]["message_type"] != "vl.classification.v1":
        raise ScenarioError("capability_missing", "Only the declared Classification semantic input is supported")
    event_types = manifest["event_contract"]["types"]
    if len({item["event_type"] for item in event_types}) != len(event_types):
        raise ScenarioError("manifest_invalid", "Event types must be unique")
    for item in event_types:
        _self_contained_schema(item["payload_schema"])
        try:
            Draft202012Validator.check_schema(item["payload_schema"])
        except Exception as exc:
            raise ScenarioError("manifest_invalid", f"Invalid event payload schema: {exc}") from exc
    _check_inventory(root, manifest)
    if 'org.vl-exchange.attribution' in manifest['extensions']:
        path = Path(__file__).resolve().parents[3] / 'attribution' / 'validator.py'
        spec = importlib.util.spec_from_file_location('vl_attribution_validator', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.validate_attribution(root, manifest)
        except ValueError as exc:
            raise ScenarioError('manifest_invalid', f'Invalid attribution: {exc}') from exc
    _check_python(root, manifest)
    _dependencies(manifest, check_environment)
    parameter_values(manifest, parameters)
    try:
        Draft202012Validator.check_schema(manifest["state"]["schema"])
    except Exception as exc:
        raise ScenarioError("manifest_invalid", f"Invalid state schema: {exc}") from exc
    _self_contained_schema(manifest["state"]["schema"])
    if manifest["state"]["schema"].get("type") != "object":
        raise ScenarioError("manifest_invalid", "State schema must describe an object")
    validate_value(manifest["state"]["initial"], manifest["state"]["schema"], code="state_contract",
                   max_bytes=manifest["limits"]["max_state_bytes"])
    sequence_ids, sequence_paths = set(), set()
    for sequence in manifest["golden"]["sequences"]:
        if sequence["id"] in sequence_ids or sequence["path"] in sequence_paths:
            raise ScenarioError("manifest_invalid", "Golden sequence ids and paths must be unique")
        sequence_ids.add(sequence["id"])
        sequence_paths.add(sequence["path"])
        path = package_path(root, sequence["path"])
        if sequence["path"] not in manifest["files"] or not sequence["path"].startswith("golden/"):
            raise ScenarioError("inventory_mismatch", "Golden files must be under golden/ and listed in inventory")
        fixture = read_json(path)
        fixture_schema = read_json(SCHEMA_PATH.parent / "schemas" / "sequence.schema.json")
        errors = sorted(StrictValidator(fixture_schema).iter_errors(fixture), key=lambda error: error.json_path)
        if errors:
            raise ScenarioError("manifest_invalid", f"Golden fixture {sequence['id']} {errors[0].json_path}: {errors[0].message}")
    links = _model_links(manifest, model_packages)
    return ValidatedPackage(root, manifest, sha256_file(package_path(root, "scenario-package.json")), links, check_environment)
