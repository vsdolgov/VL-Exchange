"""Strict read-only package helpers; no scenario code is imported here."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath

from .errors import ScenarioError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ScenarioError("path_unsafe", f"Invalid package path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ScenarioError("path_unsafe", f"Package path must be canonical and relative: {relative!r}")
    root = root.resolve(strict=True)
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ScenarioError("path_unsafe", f"Symlink is not permitted: {relative}")
    if not current.resolve().is_relative_to(root):
        raise ScenarioError("path_unsafe", f"Path escapes package: {relative}")
    if not current.is_file():
        raise ScenarioError("file_missing", f"Package file is missing: {relative}")
    return current


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        return json_copy(value, code="manifest_invalid")
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ScenarioError("manifest_invalid", f"Invalid UTF-8 JSON in {path.name}: {exc}") from exc


def json_copy(value, *, code: str, max_bytes: int | None = None):
    """Validate native JSON types, bound serialized bytes, then detach ownership."""
    def inspect(item):
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                inspect(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                inspect(child)
            return
        raise ScenarioError(code, f"Expected finite native JSON data, got {type(item).__name__}")
    try:
        inspect(value)
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded = serialized.encode("utf-8", errors="strict")
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ScenarioError(code, f"Invalid or recursive JSON data: {exc}") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ScenarioError(code, f"JSON payload exceeds {max_bytes} bytes")
    return json.loads(serialized)


def same_json(left, right):
    """Exact JSON value/type equality, independent of object member ordering."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(same_json(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right
