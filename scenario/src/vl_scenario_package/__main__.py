"""Neutral host CLI: protected external delivery, explicit development mode."""

from __future__ import annotations

import argparse
from functools import partial
import json
from pathlib import Path
import stat
import sys

from .common import read_json, _unique_object, _invalid_constant
from .errors import ScenarioError, EXIT_CODES
from .executor import ScenarioExecutor
from .golden import check_golden
from .validation import validate_package


def _key_bytes(path, *, private):
    if path is None or path.is_symlink() or not path.is_file():
        raise ScenarioError("policy_rejected", "Required key must be an existing regular, non-symlink file")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ScenarioError("policy_rejected", "Private key file must not be accessible by group or others")
    value = path.read_bytes()
    if len(value) != 32:
        raise ScenarioError("policy_rejected", "Keys must contain exactly 32 raw bytes")
    return value


def _verifier(args):
    security_fields = (args.mode, args.issuer_verify_key, args.recipient_private_key,
                       args.license_server_url, args.server_verify_key, args.installation_signing_key)
    if args.policy == "development":
        if any(security_fields) or args.allow_loopback_http:
            raise ScenarioError("policy_rejected", "Development execution must not silently ignore licensing configuration")
        return None
    if not args.mode or args.issuer_verify_key is None or args.recipient_private_key is None:
        raise ScenarioError("policy_rejected", "Protected execution requires an explicit offline/online mode and issuer/recipient keys")
    from nacl.public import PrivateKey
    from nacl.signing import SigningKey, VerifyKey
    from vl_exchange_security import OnlineAuthorizer, authorized_package

    online_fields = (args.license_server_url, args.server_verify_key, args.installation_signing_key)
    authorizer = None
    if args.mode == "online":
        if not all(online_fields):
            raise ScenarioError("policy_rejected", "Online mode requires the license endpoint, server verify key and installation signing key")
        authorizer = OnlineAuthorizer(endpoint=args.license_server_url,
                     server_verify_key=VerifyKey(_key_bytes(args.server_verify_key, private=False)),
                     installation_signing_key=SigningKey(_key_bytes(args.installation_signing_key, private=True)),
                     allow_loopback_http=args.allow_loopback_http)
    elif any(online_fields) or args.allow_loopback_http:
        raise ScenarioError("policy_rejected", "Offline mode must not silently ignore online authorization configuration")
    return partial(authorized_package,
                   issuer_verify_key=VerifyKey(_key_bytes(args.issuer_verify_key, private=False)),
                   recipient_private_key=PrivateKey(_key_bytes(args.recipient_private_key, private=True)),
                   policy=args.mode, online_authorizer=authorizer)


def main(argv=None):
    parser = argparse.ArgumentParser(description="VL Scenario Package (VLSP) neutral Python host; external delivery is protected")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run", "golden"):
        sub = commands.add_parser(name)
        sub.add_argument("source", type=Path, help="Protected envelope directory; plaintext package only with --policy development")
        sub.add_argument("--policy", choices=("protected", "development"), default="protected")
        sub.add_argument("--mode", choices=("offline", "online"))
        sub.add_argument("--issuer-verify-key", type=Path)
        sub.add_argument("--recipient-private-key", type=Path)
        sub.add_argument("--license-server-url")
        sub.add_argument("--server-verify-key", type=Path)
        sub.add_argument("--installation-signing-key", type=Path)
        sub.add_argument("--allow-loopback-http", action="store_true", help="Explicit local test transport; external endpoints require verified TLS")
        sub.add_argument("--parameters", help="JSON parameter overrides")
        sub.add_argument("--model", action="append", default=[], metavar="ALIAS=VLMP_PATH")
        sub.add_argument("--output", type=Path)
        if name == "validate":
            sub.add_argument("--check-environment", action="store_true")
            sub.add_argument("--capability", action="append")
        if name == "run":
            sub.add_argument("--inputs", type=Path, required=True)
            sub.add_argument("--context", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.resolve().is_relative_to(args.source.resolve()):
            raise ScenarioError("path_unsafe", "Write reports outside the immutable source package or envelope")
        parameters = None
        if args.parameters:
            try:
                parameters = json.loads(args.parameters, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            except ValueError as exc:
                raise ScenarioError("parameter_invalid", f"Invalid parameter JSON: {exc}") from exc
        models = {}
        for binding in args.model:
            if "=" not in binding:
                raise ScenarioError("model_link_mismatch", "--model requires ALIAS=VLMP_PATH")
            alias, path = binding.split("=", 1)
            if not alias or not path or alias in models:
                raise ScenarioError("model_link_mismatch", "Model aliases and paths must be nonempty and aliases unique")
            models[alias] = Path(path)
        verifier = _verifier(args)
        if args.command == "validate":
            def checked(root):
                return validate_package(root, parameters=parameters, model_packages=models,
                                        check_environment=args.check_environment, capabilities=args.capability).to_dict()
            if verifier is None:
                result = checked(args.source)
                result["delivery"] = {"policy": "development", "protected": False, "external_delivery": False}
            else:
                with verifier(args.source) as authorized:
                    result = checked(authorized.root)
                    if (authorized.claims["package_format"] != "vl.scenario-package"
                            or authorized.claims["manifest_path"] != "scenario-package.json"
                            or authorized.claims["manifest_sha256"] != result["manifest_sha256"]):
                        raise ScenarioError("policy_rejected", "License does not bind this exact scenario manifest")
                    authorized.assert_current()
                    result["delivery"] = {"policy": "protected", "protected": True, "external_delivery": True,
                                          "package_sha256": authorized.package_sha256, "mode": authorized.claims["mode"]}
        elif args.command == "golden":
            result = check_golden(args.source, parameters=parameters, model_packages=models, policy=args.policy, verifier=verifier)
        else:
            manager = (ScenarioExecutor(args.source, policy="development", parameters=parameters, model_packages=models)
                       if verifier is None else ScenarioExecutor.open_protected(args.source, verifier=verifier,
                                                                                parameters=parameters, model_packages=models))
            with manager as executor:
                result = executor.process(read_json(args.inputs), read_json(args.context))
            result = {"result": result, "execution": executor.report()}
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    except ScenarioError as exc:
        sys.stderr.write(json.dumps({"status": "error", **exc.to_dict()}, ensure_ascii=False) + "\n")
        return EXIT_CODES.get(exc.stage, 6)
    except ImportError as exc:
        sys.stderr.write(json.dumps({"status": "error", **ScenarioError("dependency_missing", str(exc)).to_dict()}) + "\n")
        return 3
    except (OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"status": "error", **ScenarioError("policy_rejected", str(exc)).to_dict()}) + "\n")
        return 8
    except Exception as exc:
        security = sys.modules.get("vl_exchange_security")
        if security is not None and isinstance(exc, security.AuthorizationError):
            error = ScenarioError("authorization_failed", str(exc))
        else:
            error = ScenarioError("scenario_exception", f"Host operation failed: {type(exc).__name__}: {exc}", fatal=True)
        sys.stderr.write(json.dumps({"status": "error", **error.to_dict()}) + "\n")
        return EXIT_CODES.get(error.stage, 6)


if __name__ == "__main__":
    raise SystemExit(main())
