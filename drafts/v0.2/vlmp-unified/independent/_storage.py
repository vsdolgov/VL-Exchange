"""Independent ZIP handling; untrusted archives are never passed to extractall."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import hashlib
import io
import json
import math
import os
import stat
import tempfile
import zipfile

from jsonschema import Draft202012Validator, validators


class PackageError(ValueError):
    """Package does not meet the declared profile or integrity rules."""


_MAX_ARCHIVE = 128 * 1024 * 1024
_MAX_EXPANDED = 512 * 1024 * 1024
_MAX_FILES = 1024
_MAX_MANIFEST = 1024 * 1024


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise PackageError("duplicate JSON key")
        result[name] = value
    return result


def _constant(value):
    raise PackageError("non-finite JSON number")


def strict_json(raw: bytes):
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs, parse_constant=_constant)
        # Reject lone surrogate code points and non-finite values at every depth.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PackageError("invalid strict UTF-8 JSON") from exc


_TYPES = Draft202012Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda _, value: type(value) is int,
    "number": lambda _, value: type(value) in (int, float) and math.isfinite(value),
})
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TYPES)


def canonical_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise PackageError("invalid package path")
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise PackageError("noncanonical or absolute package path")
    if ":" in name or PurePosixPath(name).is_absolute():
        raise PackageError("absolute or nonportable package path")
    return name


def archive_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_ARCHIVE:
        raise PackageError("invalid or oversized ZIP")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > _MAX_ARCHIVE:
                raise PackageError("ZIP exceeds size limit")
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def unpack(archive):
    """Extract exact file bytes into a private temp directory, rejecting unsafe ZIP entries."""
    archive = Path(archive)
    digest = archive_digest(archive)
    with archive.open("rb") as stream:
        raw = stream.read(_MAX_ARCHIVE + 1)
    if len(raw) > _MAX_ARCHIVE or hashlib.sha256(raw).hexdigest() != digest:
        raise PackageError("archive changed before extraction")
    with tempfile.TemporaryDirectory(prefix="vlmp-independent-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                members = package.infolist()
                if len(members) > _MAX_FILES:
                    raise PackageError("too many ZIP entries")
                seen, expanded = set(), 0
                for member in members:
                    if member.orig_filename != member.filename:
                        raise PackageError("NUL or truncated ZIP filename")
                    is_dir = member.is_dir()
                    name = canonical_name(member.filename[:-1] if is_dir else member.filename)
                    if name in seen:
                        raise PackageError("duplicate ZIP entry")
                    seen.add(name)
                    mode = member.external_attr >> 16
                    typ = stat.S_IFMT(mode)
                    if typ not in (0, stat.S_IFREG, stat.S_IFDIR) or (is_dir and typ == stat.S_IFREG) or (not is_dir and typ == stat.S_IFDIR):
                        raise PackageError("symlinks and special ZIP entries are forbidden")
                    if member.flag_bits & 1:
                        raise PackageError("encrypted ZIP entries unsupported")
                    if member.file_size > _MAX_ARCHIVE:
                        raise PackageError("individual ZIP entry exceeds limit")
                    expanded += member.file_size
                    if expanded > _MAX_EXPANDED:
                        raise PackageError("expanded ZIP limit exceeded")
                    dest = root.joinpath(*name.split("/"))
                    if is_dir:
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as src, dest.open("xb") as out:
                        total = 0
                        while chunk := src.read(1024 * 1024):
                            total += len(chunk)
                            if total > member.file_size:
                                raise PackageError("ZIP entry larger than declared")
                            out.write(chunk)
                        if total != member.file_size:
                            raise PackageError("ZIP entry size mismatch")
                    os.chmod(dest, 0o600)
        except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
            if isinstance(exc, PackageError):
                raise
            raise PackageError("cannot safely extract ZIP") from exc
        try:
            yield root, digest
        finally:
            if archive_digest(archive) != digest:
                raise PackageError("archive changed during execution")


def read_manifest(root: Path, schema_path: Path):
    path = root / "model-package.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_MANIFEST:
        raise PackageError("missing or oversized model manifest")
    raw = path.read_bytes()
    manifest = strict_json(raw)
    schema = strict_json(schema_path.read_bytes())
    errors = list(StrictValidator(schema).iter_errors(manifest))
    if errors:
        raise PackageError(f"invalid manifest: {errors[0].message}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if any(p.is_symlink() for p in root.rglob("*")):
        raise PackageError("symlink inside extracted package")
    inventory = manifest["files"]
    if actual != set(inventory) | {"model-package.json"} or "model-package.json" in inventory:
        raise PackageError("file inventory mismatch")
    for name, expected in inventory.items():
        canonical_name(name)
        target = root.joinpath(*name.split("/"))
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise PackageError("inventory digest mismatch")
    return manifest, hashlib.sha256(raw).hexdigest()
