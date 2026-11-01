"""Execute one unchanged VLMP ZIP through two separately implemented hosts.

Only trusted synthetic packages belong in this in-process development workflow.
Safe extraction is not a Python sandbox. The two host processes share ONNX Runtime.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_ENTRIES = 1024

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def canonical(name):
    if not name or "\\" in name or PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("Unsafe ZIP entry path")
    if str(PurePosixPath(name)) != name:
        raise ValueError("Noncanonical ZIP entry path")
    return name

def inventory(directory):
    result = {}
    for file in sorted(directory.rglob("*")):
        if file.is_symlink():
            raise ValueError("Symlink in extracted package")
        if file.is_file():
            result[file.relative_to(directory).as_posix()] = digest(file.read_bytes())
    return result

def inspect_archive(raw):
    if not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError("ZIP size exceeds the supported limit")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise ValueError("Too many ZIP entries")
        seen = set()
        files = {}
        total = 0
        for entry in entries:
            name = entry.filename[:-1] if entry.is_dir() else entry.filename
            canonical(name)
            folded = name.casefold()
            if folded in seen:
                raise ValueError("Duplicate or case-colliding ZIP entries")
            seen.add(folded)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                raise ValueError("Symlink or encrypted ZIP entry")
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("Unsupported ZIP compression")
            if not entry.is_dir():
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise ValueError("Expanded ZIP exceeds the supported limit")
                files[name] = entry
        folded_files = {name.casefold() for name in files}
        for name in files:
            parts = name.split("/")
            if any("/".join(parts[:i]).casefold() in folded_files for i in range(1, len(parts))):
                raise ValueError("ZIP file/directory collision")
        if "model-package.json" not in files:
            raise ValueError("Missing root model manifest")
        # zipfile enforces CRC on complete reads; also bound actual streamed size.
        contents = {}
        expanded = 0
        for name, entry in files.items():
            chunks = []
            with archive.open(entry) as source:
                actual = 0
                while chunk := source.read(1024 * 1024):
                    actual += len(chunk)
                    expanded += len(chunk)
                    if actual > entry.file_size or expanded > MAX_EXPANDED:
                        raise ValueError("Expanded ZIP exceeded declared size or resource limit")
                    chunks.append(chunk)
            if actual != entry.file_size:
                raise ValueError("ZIP entry size mismatch")
            contents[name] = b"".join(chunks)
    return contents

def extract(contents, destination):
    destination.mkdir(mode=0o700)
    for name, raw in contents.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Extraction escaped private directory")
        with target.open("xb") as output:
            output.write(raw)
        target.chmod(0o600)

def oracle(manifest, calls):
    fixtures = manifest["golden"]["fixtures"]
    order = fixtures + list(reversed(fixtures)) + [fixtures[0], fixtures[1], fixtures[0]]
    if len(calls) != len(order):
        raise ValueError("Host call count differs from expected golden lifecycle")
    for actual, fixture in zip(calls, order, strict=True):
        if actual["fixture"] != fixture["id"] or actual["pass"] is not True:
            raise ValueError("Host did not pass expected fixture")
        result, expected = actual["result"], fixture["expected"]
        if set(result) != {"label", "score", "category_id", "output_index"}:
            raise ValueError("Unexpected result shape")
        if any(type(result[key]) is not type(expected[key]) or result[key] != expected[key] for key in ("label", "category_id", "output_index")):
            raise ValueError("Result differs from exact golden identity")
        score = result["score"]
        if type(score) not in (float, int) or not math.isfinite(score):
            raise ValueError("Invalid score")
        tolerance = manifest["golden"]["atol"] + manifest["golden"]["rtol"] * abs(expected["score"])
        if abs(score - expected["score"]) > tolerance:
            raise ValueError("Result score differs from golden oracle")
    return len(order)

def execute(archive_path):
    if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Invalid or oversized archive file")
    raw = archive_path.read_bytes()
    archive_before = digest(raw)
    contents = inspect_archive(raw)
    members = {name: digest(data) for name, data in contents.items()}
    manifest = json.loads(contents["model-package.json"].decode("utf-8"))
    manifest_hash = members["model-package.json"]
    if set(members) - {"model-package.json"} != set(manifest["files"]):
        raise ValueError("ZIP member inventory differs from manifest")
    if any(members[name] != value for name, value in manifest["files"].items()):
        raise ValueError("ZIP member digest differs from manifest")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    hosts = {}
    temporary_removed = False
    with tempfile.TemporaryDirectory(prefix="vlmp-archive-roundtrip-") as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        for name, command in (
            ("reference", [sys.executable, str(ROOT / "model/src/public_model.py"), "golden"]),
            ("independent", [sys.executable, str(ROOT / "model/tools/independent_host.py")]),
        ):
            destination = private / name
            extract(contents, destination)
            before = inventory(destination)
            if before != members:
                raise ValueError("Extracted member bytes differ from ZIP")
            completed = subprocess.run([*command, str(destination)], cwd=private, env=env,
                                       text=True, capture_output=True, timeout=120, check=True)
            report = json.loads(completed.stdout)
            if report["status"] != "pass" or report["manifest_sha256"] != manifest_hash:
                raise ValueError("Host result or manifest identity mismatch")
            calls = oracle(manifest, report["calls"])
            after = inventory(destination)
            if after != before:
                raise ValueError("Host modified extracted package")
            hosts[name] = {"status": "pass", "calls_checked_against_oracle": calls,
                           "manifest_sha256_before": before["model-package.json"],
                           "manifest_sha256_after": after["model-package.json"],
                           "member_sha256_before": before, "member_sha256_after": after,
                           "host_source_sha256": digest(Path(command[1]).read_bytes()),
                           "runtime": report["runtime"], "calls": report["calls"]}
        if hosts["reference"]["calls"] != hosts["independent"]["calls"]:
            raise ValueError("Independent hosts disagree on complete calls/results")
        if len(hosts["reference"]["calls"]) != 13:
            raise ValueError("The published synthetic example must exercise exactly 13 calls")
    temporary_removed = not private.exists()
    archive_after = digest(archive_path.read_bytes())
    if archive_after != archive_before or not temporary_removed:
        raise ValueError("Archive changed or plaintext temporary directory remained")
    return {"status": "pass", "format": manifest["format"], "spec_version": manifest["spec_version"],
            "profile": manifest["profile"], "wrapper_source_sha256": digest(Path(__file__).read_bytes()),
            "archive_sha256_before": archive_before,
            "archive_sha256_after": archive_after, "archive_unchanged": True,
            "archive_member_sha256": members, "manifest_sha256": manifest_hash,
            "two_separate_private_extractions": True, "temporary_plaintext_removed": temporary_removed,
            "complete_host_results_equal": True, "hosts": hosts,
            "limitations": ["Trusted synthetic Python only; extraction safety is not a Python sandbox.",
                            "Two independent local loaders share the package model code and ONNX Runtime.",
                            "No third-party vendor platform, Box5, GPU or production deployment was tested."]}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=ROOT / "model/reports/vlmp-public-channel-probe.zip")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.archive.resolve():
        parser.error("Report must not replace the input archive")
    try:
        report = execute(args.archive)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        report = {"status": "fail", "error_type": type(error).__name__, "message": str(error)}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["status"] == "pass" else 1

if __name__ == "__main__":
    raise SystemExit(main())
