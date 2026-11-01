"""Operator CLI for generic VL Exchange keys and protected deliveries."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time

from nacl.public import PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey

from .protection import AuthorizationError, seal_package


def read_key(path: Path, *, private: bool) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Key must be a regular, non-symlink file")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Private key file must not be accessible by group or others")
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("A key file must contain exactly 32 raw bytes")
    return value


def _write_new(path: Path, value: bytes, permissions: int):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, permissions)
    with os.fdopen(descriptor, "wb") as out:
        out.write(value)


def _keygen(kind: str, private: Path, public: Path):
    if private == public or private.exists() or public.exists():
        raise ValueError("Choose two distinct new output paths; existing keys are never overwritten")
    key = PrivateKey.generate() if kind == "encryption" else SigningKey.generate()
    public_key = key.public_key if kind == "encryption" else key.verify_key
    _write_new(private, bytes(key), 0o600)
    try:
        _write_new(public, bytes(public_key), 0o644)
    except Exception:
        private.unlink(missing_ok=True)
        raise
    return {"status": "ok", "kind": kind, "private_key_file": str(private),
            "public_key_file": str(public), "private_key_contents_printed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="VL Exchange protected package tools")
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="Create a new signing or encryption key pair")
    keygen.add_argument("--kind", required=True, choices=("signing", "encryption"))
    keygen.add_argument("--private", type=Path, required=True)
    keygen.add_argument("--public", type=Path, required=True)
    seal = commands.add_parser("seal", help="Issue one signed license and encrypted package")
    seal.add_argument("--package", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.add_argument("--issuer-private", type=Path, required=True)
    seal.add_argument("--recipient-public", type=Path, required=True)
    seal.add_argument("--installation-signing-public", type=Path, required=True)
    seal.add_argument("--issuer-id", required=True)
    seal.add_argument("--license-id", required=True)
    seal.add_argument("--installation-id", required=True)
    seal.add_argument("--mode", choices=("offline", "online"), required=True)
    seal.add_argument("--valid-seconds", type=int, required=True)
    seal.add_argument("--manifest-path", required=True, help="Relative manifest path inside the original ZIP")
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            result = _keygen(args.kind, args.private, args.public)
        else:
            if args.valid_seconds <= 0:
                raise ValueError("valid-seconds must be positive")
            current = int(time.time())
            result = seal_package(args.package, args.output,
                issuer_signing_key=SigningKey(read_key(args.issuer_private, private=True)),
                recipient_public_key=PublicKey(read_key(args.recipient_public, private=False)),
                installation_verify_key=VerifyKey(read_key(args.installation_signing_public, private=False)),
                issuer_id=args.issuer_id, license_id=args.license_id, installation_id=args.installation_id,
                mode=args.mode, issued_at=current, not_before=current, expires_at=current + args.valid_seconds,
                manifest_path=args.manifest_path)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        return 0
    except (AuthorizationError, OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"status": "error", "code": getattr(exc, "code", "operation_failed"),
                                     "message": str(exc)}, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
