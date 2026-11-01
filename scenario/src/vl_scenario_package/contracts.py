"""Neutral JSON contracts for a Python scenario host; no block/editor runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from .common import json_copy, read_json
from .errors import ScenarioError

_types = Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda checker, instance: type(instance) is int,
    "number": lambda checker, instance: type(instance) in (int, float),
})
StrictValidator = validators.extend(Draft202012Validator, type_checker=_types)
SCHEMAS = Path(__file__).resolve().parents[2] / "spec" / "schemas"


def validate_value(value, schema: dict, *, code: str, max_bytes: int | None = None):
    detached = json_copy(value, code=code, max_bytes=max_bytes)
    errors = sorted(StrictValidator(schema).iter_errors(detached), key=lambda error: error.json_path)
    if errors:
        raise ScenarioError(code, f"{errors[0].json_path}: {errors[0].message}")
    return detached


def parameter_values(manifest, overrides=None):
    declared = manifest["parameters"]
    overrides = {} if overrides is None else json_copy(overrides, code="parameter_invalid")
    if type(overrides) is not dict or set(overrides) - set(declared):
        raise ScenarioError("parameter_invalid", "Unknown or invalid parameter overrides")
    result = {}
    for name, description in declared.items():
        schema = {key: value for key, value in description.items() if key != "default"}
        default = validate_value(description["default"], schema, code="parameter_invalid")
        result[name] = validate_value(overrides.get(name, default), schema, code="parameter_invalid")
    return result


def validate_input(inputs, manifest):
    copied = json_copy(inputs, code="input_contract", max_bytes=manifest["limits"]["max_input_bytes"])
    name = manifest["input_contract"]["name"]
    if type(copied) is not dict or set(copied) != {name}:
        raise ScenarioError("input_contract", f"Exactly the declared input {name!r} must be supplied")
    if manifest["input_contract"]["message_type"] != "vl.classification.v1":
        raise ScenarioError("capability_missing", "Only the versioned Classification input is implemented")
    validate_value(copied[name], read_json(SCHEMAS / "classification.schema.json"), code="input_contract")
    return copied


def event_id(scenario_id: str, version: str, context: dict, event_type: str, ordinal: int) -> str:
    """Domain-separated, length-prefixed UTF-8 identity; no normalization."""
    values = [scenario_id, version, context["source_id"], context["run_id"],
              str(context["sequence"]), event_type, str(ordinal)]
    material = bytearray(b"VLSP-EVENT-V1\0")
    try:
        for value in values:
            encoded = value.encode("utf-8", errors="strict")
            material.extend(len(encoded).to_bytes(4, "big"))
            material.extend(encoded)
    except (UnicodeError, OverflowError, AttributeError) as exc:
        raise ScenarioError("event_contract", f"Event identity fields cannot be encoded: {exc}") from exc
    return "sha256:" + hashlib.sha256(material).hexdigest()


def validate_events(events, manifest, context):
    if type(events) is not list or len(events) > manifest["limits"]["max_events_per_call"]:
        raise ScenarioError("event_contract", "events must be an explicit list within max_events_per_call")
    types = {item["event_type"]: item["payload_schema"] for item in manifest["event_contract"]["types"]}
    envelope_schema = read_json(SCHEMAS / "event.schema.json")
    seen = set()
    result = []
    for ordinal, event in enumerate(events):
        event = validate_value(event, envelope_schema, code="event_contract", max_bytes=manifest["limits"]["max_event_bytes"])
        if event["event_type"] not in types:
            raise ScenarioError("event_contract", f"Undeclared event_type: {event['event_type']}")
        for key in ("source_id", "run_id", "sequence", "event_time_ms"):
            if type(event[key]) is not type(context[key]) or event[key] != context[key]:
                raise ScenarioError("event_contract", f"Event {key} must echo the current host context exactly")
        expected = event_id(manifest["scenario_id"], manifest["version"], context, event["event_type"], ordinal)
        if event["event_id"] != expected or event["event_id"] in seen:
            raise ScenarioError("event_contract", "Event ID does not match the declared deterministic identity")
        seen.add(event["event_id"])
        validate_value(event["payload"], types[event["event_type"]], code="event_contract")
        result.append(event)
    return result


def materialize_events(candidates, manifest, context):
    """Host-owned envelope and identity; scenario code supplies only candidates."""
    if type(candidates) is not list or len(candidates) > manifest["limits"]["max_events_per_call"]:
        raise ScenarioError("event_contract", "events must be an explicit list within max_events_per_call")
    events = []
    for ordinal, candidate in enumerate(candidates):
        if type(candidate) is not dict or set(candidate) != {"event_type", "payload"}:
            raise ScenarioError("event_contract", "A scenario event candidate must contain exactly event_type and payload")
        kind = candidate["event_type"]
        event = {"event_id": event_id(manifest["scenario_id"], manifest["version"], context, kind, ordinal),
                 "event_type": kind, "source_id": context["source_id"], "run_id": context["run_id"],
                 "sequence": context["sequence"], "event_time_ms": context["event_time_ms"],
                 "payload": candidate["payload"]}
        events.append(event)
    return validate_events(events, manifest, context)
