"""Trusted-host bridge from signed VLSP hardware selections to model callbacks.

The selector and callbacks are injected trusted host infrastructure. This is
not a sandbox and does not prove what arbitrary Python does with model bytes.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import copy
import hashlib
from pathlib import Path
from types import MappingProxyType

from .errors import ScenarioError
from .services import Host, ModelBinding


@dataclass(frozen=True)
class HardwareAdapter:
    """Configure a signed model binding; all callable fields are trusted host code.

    `select` is the real hardware-portability selector; `validate_package` the
    corresponding model overlay validator. `model_factory(role, selection,
    artifact_bytes)` returns an inference callable that MUST load only the
    supplied immutable signed bytes and enforce the selected device/fallback.
    `acquire_lease(role, selection)` returns the *original* model licence lease
    with assert_current(). It must check model-specific rights, not merely a
    signature or a manifest boolean. Host owns its protected-source context for
    the whole executor/worker lifetime. The adapter does not close that context.
    """
    envelope: dict
    trust_anchors: dict
    now: object
    requests: list
    devices: dict
    select: object
    validate_package: object
    authorize_license: object
    attest_compatibility: object
    acquire_lease: object
    model_factory: object
    verify_quality: object = None

    def prepare(self, scenario_manifest_bytes):
        try:
            result = self.select(copy.deepcopy(self.envelope), trust_anchors=dict(self.trust_anchors),
                now=self.now(), scenario_manifest_bytes=scenario_manifest_bytes,
                requests=copy.deepcopy(self.requests), devices=copy.deepcopy(self.devices),
                validate_package=self.validate_package, authorize_license=self.authorize_license,
                attest_compatibility=self.attest_compatibility, verify_quality=self.verify_quality)
            return copy.deepcopy(result)
        except Exception as exc:
            raise ScenarioError('hardware_rejected', f'Signed hardware selection rejected: {exc}') from exc

    def assert_current(self, scenario_manifest_bytes, role, expected_selection):
        actual = self.prepare(scenario_manifest_bytes)
        if role not in actual or actual[role] != expected_selection:
            raise ScenarioError('hardware_rejected', f'Hardware selection changed for {role}')
        return copy.deepcopy(actual[role])

    def bind(self, scenario_manifest_bytes, manifest, host):
        selected = self.prepare(scenario_manifest_bytes)
        by_alias = {x['alias']: x for x in manifest['models']}
        requests = {x['role']: x for x in self.requests}
        if set(selected) != set(requests) or not set(selected) <= set(by_alias):
            raise ScenarioError('hardware_rejected', 'Hardware bindings must target declared synchronous model roles')
        models = dict(host.models)
        # A distinct ContextVar per executor captures original leases until commit.
        active_leases = ContextVar('vlsp_hardware_leases_' + hex(id(models)), default=None)
        for role, selection in selected.items():
            root = Path(requests[role]['package_root'])
            contents = {}
            for artifact in selection['artifacts']:
                data = (root / artifact['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != artifact['sha256']:
                    raise ScenarioError('hardware_rejected', 'Artifact changed after verification')
                contents[artifact['path']] = data
            # Recheck after snapshot and before any model code is constructed.
            self.assert_current(scenario_manifest_bytes, role, selection)
            immutable_bytes = MappingProxyType(contents)
            infer = self.model_factory(role, copy.deepcopy(selection), immutable_bytes)
            if not callable(infer):
                raise ScenarioError('hardware_rejected', 'Model factory must return an inference callable')
            def guarded(request, context, *, _role=role, _selection=copy.deepcopy(selection), _infer=infer):
                try:
                    self.assert_current(scenario_manifest_bytes, _role, _selection)
                    lease = self.acquire_lease(_role, copy.deepcopy(_selection))
                except Exception as exc:
                    raise ScenarioError('authorization_failed', f'Model licence unavailable: {exc}', retryable=True) from exc
                if not callable(getattr(lease, 'assert_current', None)):
                    raise ScenarioError('service_failed', 'Model licence requires an immutable lease', fatal=True)
                try:
                    if lease.assert_current() is False:
                        raise ValueError('lease rejected')
                except Exception as exc:
                    raise ScenarioError('authorization_failed', f'Model licence is not current: {exc}', retryable=True) from exc
                leases = active_leases.get()
                if leases is None:
                    raise ScenarioError('service_failed', 'Hardware model call outside executor process', fatal=True)
                leases.append(lease)
                result = _infer(request, context)
                try:
                    if lease.assert_current() is False:
                        raise ValueError('lease rejected')
                    self.assert_current(scenario_manifest_bytes, _role, _selection)
                except Exception as exc:
                    raise ScenarioError('authorization_failed', f'Model licence expired during inference: {exc}', retryable=True) from exc
                return result
            item = by_alias[role]
            models[role] = ModelBinding(item['kind'], item['manifest_sha256'], item['profile'], guarded)
        return Host(models=models, trackers=dict(host.trackers)), BoundHardware(
            self, scenario_manifest_bytes, copy.deepcopy(selected), active_leases)


@dataclass(frozen=True)
class BoundHardware:
    adapter: HardwareAdapter
    scenario_manifest_bytes: bytes
    selection: dict
    active_leases: ContextVar

    def begin_call(self):
        try:
            for role, expected in self.selection.items():
                self.adapter.assert_current(self.scenario_manifest_bytes, role, expected)
        except Exception as exc:
            raise ScenarioError('authorization_failed', f'Hardware selection is no longer authorized: {exc}', retryable=True) from exc
        return self.active_leases.set([])

    def assert_current(self):
        try:
            for role, expected in self.selection.items():
                self.adapter.assert_current(self.scenario_manifest_bytes, role, expected)
            for lease in self.active_leases.get() or []:
                if lease.assert_current() is False:
                    raise ValueError('lease rejected')
        except Exception as exc:
            raise ScenarioError('authorization_failed', 'Original model licence or hardware selection expired or was revoked before commit', retryable=True) from exc

    def end_call(self, token):
        self.active_leases.reset(token)
