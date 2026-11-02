"""Ephemeral test issuer and composite per-frame authorization leases."""
from contextlib import ExitStack
from functools import partial
from pathlib import Path
import tempfile
import threading
import time

from nacl.public import PrivateKey
from nacl.signing import SigningKey
from vl_exchange_security import ExecutionLease, OnlineAuthorizer, authorized_package, seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService, make_server


class ModelProtection:
    """Separate signed licenses and envelopes for three exact model ZIPs."""
    def __init__(self, fixtures, policy):
        self.resources = ExitStack()
        self.offset = 0
        self.now = int(time.time())
        self.issuer = SigningKey.generate()
        self.recipient = PrivateKey.generate()
        self.installation = SigningKey.generate()
        self.policy = policy
        self.envelopes = {}
        try:
            tmp = Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix='vlsp-model-protection-')))
            server_key = SigningKey.generate()
            self.authorizer = None
            if policy == 'online':
                service = ReferenceLicenseService(issuer_verify_key=self.issuer.verify_key,
                    server_signing_key=server_key, clock=self.clock)
                server = make_server(service, '127.0.0.1', 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def stop():
                    server.shutdown(); thread.join(timeout=5); server.server_close()
                self.resources.callback(stop)
                self.authorizer = OnlineAuthorizer(f'http://127.0.0.1:{server.server_port}/v1/grant',
                    server_key.verify_key, self.installation, allow_loopback_http=True)
            for alias in ('detector', 'classifier_a', 'classifier_b'):
                target = tmp / alias
                sealed = seal_package(fixtures / (alias + '.zip'), target,
                    issuer_signing_key=self.issuer, recipient_public_key=self.recipient.public_key,
                    installation_verify_key=self.installation.verify_key, issuer_id='local-model-issuer',
                    license_id='model-' + alias, installation_id='local-model-installation', mode=policy,
                    issued_at=self.now, not_before=self.now, expires_at=self.now+3600,
                    manifest_path='model-package.json')
                self.envelopes[alias] = (target, sealed['package_sha256'])
            self.verifier = partial(authorized_package, issuer_verify_key=self.issuer.verify_key,
                recipient_private_key=self.recipient, policy=policy, online_authorizer=self.authorizer,
                now=self.clock)
        except BaseException:
            self.resources.close()
            raise

    def clock(self): return self.now + self.offset
    def expire(self): self.offset += 3601
    def close(self): self.resources.close()


def close_all(actions):
    first = None
    for action in actions:
        try:
            action()
        except BaseException as exc:
            if first is None:
                first = (exc, exc.__traceback__)
    if first is not None:
        raise first[0].with_traceback(first[1])


class LeaseRegistry:
    def __init__(self): self.current = []
    def begin(self):
        self.current = []
        return self.current
    def append(self, lease): self.current.append(lease)


class LeaseRecorder:
    """Record the exact per-prediction lease used by the protected model host."""
    def __init__(self, wrapped, leases): self.wrapped, self.leases = wrapped, leases
    def __getattr__(self, name): return getattr(self.wrapped, name)
    def before_process(self):
        lease = self.wrapped.before_process()
        self.leases.append(lease)
        return lease


class CompositeLease(ExecutionLease):
    def __init__(self, scenario, models):
        super().__init__(scenario._owner, scenario._license_raw, scenario._license_signature,
                         scenario._issuer_key, scenario._clock, scenario._mode,
                         scenario._server_key, scenario._grant)
        object.__setattr__(self, '_scenario_lease', scenario)
        object.__setattr__(self, '_model_leases', models)
    def assert_current(self):
        self._scenario_lease.assert_current()
        for lease in self._model_leases: lease.assert_current()


class CompositeAuthorization:
    """Use the scenario's native commit hook to also guard all model leases."""
    def __init__(self, wrapped, leases): self.wrapped, self.leases = wrapped, leases
    def __getattr__(self, name): return getattr(self.wrapped, name)
    def before_process(self):
        bucket = self.leases.begin()
        return CompositeLease(self.wrapped.before_process(), bucket)
