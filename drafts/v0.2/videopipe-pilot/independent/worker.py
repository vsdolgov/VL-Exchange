"""JSONL VideoPipe boundary for the separately written synchronous host."""
import bootstrap
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import time
import zipfile

from nacl.public import PrivateKey
from nacl.signing import SigningKey
from vl_exchange_security import authorized_package, seal_package

from independent.engine import Engine, Fault
from independent.providers import ProviderSet, ProviderInputError

FIXTURES = bootstrap.PILOT / "fixtures"
ZIP_DIGEST = "1461028eb6176c72bb0229eb8b28b7e697b6a4fcc1f7d420e095631bf4e0f332"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    pins = json.loads((FIXTURES / "pins.json").read_text("utf-8"))
    if pins["scenario_zip_sha256"] != ZIP_DIGEST or digest(FIXTURES / "scenario.zip") != ZIP_DIGEST:
        raise ValueError("Independent worker received a different scenario ZIP")
    for alias, info in pins["models"].items():
        root = FIXTURES / "models" / alias
        actual = {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}
        if actual != info["files"]:
            raise ValueError("Independent worker dependency changed: " + alias)
    return pins


def extract(archive, root):
    with zipfile.ZipFile(archive) as zf:
        if len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError("Duplicate ZIP entry")
        for entry in zf.infolist():
            name = entry.filename
            if (PurePosixPath(name).is_absolute() or "\\" in name or entry.is_dir()
                    or any(part in ("", ".", "..") for part in name.split("/"))):
                raise ValueError("Unsafe fixture ZIP entry")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(entry))


class Session:
    def __init__(self):
        self.pins = verify()
        self.providers = None
        self.engine = None
        self.resources = None
        self.offset = 0
        self.published = []
        self.closed = False

    def close(self):
        try:
            if self.engine is not None:
                self.engine.close()
        finally:
            if self.resources is not None:
                resources, self.resources = self.resources, None
                resources.close()

    def open(self, command):
        self.close()
        self.engine, self.offset, self.published = None, 0, []
        verify()
        if self.providers is None:
            self.providers = ProviderSet(self.pins, FIXTURES)
        self.providers.counts.clear()
        bindings = self.providers.bindings(command.get("bindings", {}))
        policy = command.get("policy", "offline")
        if policy not in ("offline", "development"):
            raise Fault("policy_rejected", "Unsupported pilot delivery policy", "security", False)
        resources = ExitStack()
        try:
            temp = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="independent-vlsp-")))
            if policy == "development":
                package = temp / "package"
                package.mkdir()
                extract(FIXTURES / "scenario.zip", package)
                with bootstrap.observe_imports():
                    self.engine = Engine(package, bindings, parameters=command.get("parameters"))
            else:
                signing = SigningKey.generate()
                recipient = PrivateKey.generate()
                installation = SigningKey.generate()
                now = int(time.time())
                sealed = seal_package(FIXTURES / "scenario.zip", temp / "sealed",
                    issuer_signing_key=signing, recipient_public_key=recipient.public_key,
                    installation_verify_key=installation.verify_key, issuer_id="independent-pilot-issuer",
                    license_id="independent-pilot-offline", installation_id="independent-local-installation",
                    mode="offline", issued_at=now, not_before=now, expires_at=now+3600,
                    manifest_path="scenario-package.json")
                if sealed["package_sha256"] != ZIP_DIGEST:
                    raise ValueError("Envelope licenses a different ZIP")
                authorization = resources.enter_context(authorized_package(temp / "sealed",
                    issuer_verify_key=signing.verify_key, recipient_private_key=recipient,
                    policy="offline", now=lambda: int(time.time()) + self.offset))
                with bootstrap.observe_imports():
                    self.engine = Engine(authorization.root, bindings, authorization=authorization,
                                         parameters=command.get("parameters"))
            self.resources = resources
        except BaseException:
            resources.close()
            self.engine = None
            raise
        return {"policy": policy, "protected": policy == "offline",
                "scenario_zip_sha256": ZIP_DIGEST,
                "scenario_manifest_sha256": self.pins["scenario_manifest_sha256"],
                "models": {alias: {key: info[key] for key in ("manifest_sha256", "weights_sha256")}
                           for alias, info in self.pins["models"].items()}}

    def handle(self, command):
        if self.providers is not None:
            self.providers.trace.clear()
        imports = len(bootstrap.SCENARIO_EXECS)
        answer = {"id": command.get("id"), "ok": True}
        try:
            operation = command["op"]
            if operation == "open":
                answer["opened"] = self.open(command)
            elif operation == "close":
                self.close()
            elif operation == "shutdown":
                self.shutdown()
            elif self.engine is None:
                raise Fault("lifecycle", "No open independent scenario", "lifecycle", False)
            elif operation == "process":
                try:
                    self.providers.begin(command, lambda: setattr(self, "offset", self.offset+3601))
                    answer["result"] = self.engine.process(deepcopy(command["inputs"]), deepcopy(command["context"]))
                except ProviderInputError as exc:
                    raise Fault("input_contract", str(exc), "input", False) from exc
                finally:
                    self.providers.end()
                self.published.extend(deepcopy(answer["result"]["events"]))
            elif operation == "reset":
                arguments = command.get("reset", {key: command[key] for key in ("source_id", "run_id") if key in command})
                self.engine.reset(**arguments)
            elif operation != "snapshot":
                raise Fault("input_contract", "Unknown command", "input", False)
        except Fault as exc:
            answer.update(ok=False, error=exc.to_dict())
        answer["snapshot"] = self.engine.snapshot() if self.engine is not None else {"state": {}, "order": {}}
        answer["published_events"] = deepcopy(self.published)
        answer["trace"] = deepcopy(self.providers.trace) if self.providers is not None else []
        answer["calls"] = dict(sorted(self.providers.counts.items())) if self.providers is not None else {}
        answer["lifecycle"] = self.engine.lifecycle() if self.engine is not None else None
        answer["scenario_imports"] = len(bootstrap.SCENARIO_EXECS) - imports
        answer["independence"] = bootstrap.evidence()
        if answer["independence"]["forbidden_import_attempts"] or answer["independence"]["loaded_reference_modules"]:
            raise RuntimeError("Reference implementation reuse detected")
        return answer

    def shutdown(self):
        if self.closed:
            return
        self.close()
        if self.providers is not None:
            self.providers.close()
        self.closed = True


def main():
    session = Session()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            command = json.loads(line, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
            answer = session.handle(command)
            print(json.dumps(answer, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
            if command["op"] == "shutdown":
                break
    finally:
        session.shutdown()


if __name__ == "__main__":
    main()
