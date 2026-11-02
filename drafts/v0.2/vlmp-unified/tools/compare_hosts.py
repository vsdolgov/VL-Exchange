"""Run both independent VLMP hosts on the same new and historical ZIPs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[4]
BASE = Path(__file__).resolve().parents[1]

PACKAGES = {
    "unified-classification": BASE / "examples/classification.zip",
    "unified-detection": BASE / "examples/detection.zip",
    "legacy-classification": ROOT / "model/reports/vlmp-public-channel-probe.zip",
    "legacy-detection": ROOT / "drafts/v0.2/model/examples/vl-color-detector.zip",
}
EXPECTED_ZIP_SHA256 = {
    "unified-classification": "a7b6e4299faff1d98ec2c7b53892c7c3dfc6e4ecf1029078224bab65d8a4e95f",
    "unified-detection": "39e2afcdbfd8ff9a29066cde92362b4cd9fde1fab851a0cec7d54f72e06341c9",
    "legacy-classification": "b004c02c6f12839007389c71ec60e6bd77b2824a72c27d9bd7ddc71be04752ff",
    "legacy-detection": "333422827bd990d99ca1c0a709b235cf6fdb51842bd0152c18183106ec350593",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def matches(actual, expected, atol, rtol):
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, int):
        return type(actual) is int and actual == expected
    if isinstance(expected, float):
        return (type(actual) in (int, float) and not isinstance(actual, bool)
                and math.isfinite(actual) and abs(actual - expected) <= atol + rtol * abs(expected))
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            matches(a, e, atol, rtol) for a, e in zip(actual, expected))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(
            matches(actual[k], v, atol, rtol) for k, v in expected.items())
    return False


def fixture_set(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("model-package.json"))
        golden = manifest["golden"]
        rows = golden if isinstance(golden, list) else golden["fixtures"]
        atol = 1e-6 if isinstance(golden, list) else golden["atol"]
        rtol = 1e-5 if isinstance(golden, list) else golden["rtol"]
        fixtures = []
        for row in rows:
            image = np.load(io.BytesIO(archive.read(row["input"])), allow_pickle=False)
            fixtures.append((row.get("id", Path(row["input"]).stem), image, row["expected"]))
        return manifest, fixtures, atol, rtol


def run_case(path):
    before = path.read_bytes()
    manifest, fixtures, atol, rtol = fixture_set(before)
    weights = manifest["profile"]["weights"] if isinstance(manifest["profile"], dict) else manifest["backend"]["weights"]
    with zipfile.ZipFile(io.BytesIO(before)) as archive:
        weights_sha256 = sha(archive.read(weights))
        manifest_sha256 = sha(archive.read("model-package.json"))
    assert manifest["files"][weights] == weights_sha256
    assert len(fixtures) >= 3, "each profile needs at least three golden inputs"
    assert len({(a.dtype.str, a.shape, a.tobytes()) for _, a, _ in fixtures}) == len(fixtures)
    order = list(range(len(fixtures))) + list(reversed(range(len(fixtures)))) + [0, 1, 0]
    generator = np.random.default_rng(20261103)
    extra_inputs = [generator.integers(0, 256, size=fixtures[0][1].shape, dtype=np.uint8) for _ in range(8)]
    hosts = {}
    for label, loader in (("reference", reference_load), ("independent", independent_load)):
        model = loader(path)
        try:
            assert model.zip_sha256 == sha(before)
            assert model.manifest_sha256 == manifest_sha256, "manifest hash does not match ZIP bytes"
            expected_profile = manifest["profile"]["id"] if isinstance(manifest["profile"], dict) else manifest["profile"]
            assert model.profile == expected_profile, (label, model.profile, expected_profile)
            calls = []
            for index in order:
                fixture_id, image, expected = fixtures[index]
                # Supply an independent image buffer to each host and each invocation.
                prediction = model.predict(image.copy())
                assert matches(prediction, expected, atol, rtol), (label, fixture_id, prediction, expected)
                calls.append({"fixture": fixture_id, "result": prediction})
            extra = [{"input_sha256": sha(image.tobytes()), "result": model.predict(image.copy())}
                     for image in extra_inputs]
            assert model.inference_calls == len(order) + len(extra_inputs), "unexpected model invocation count"
            hosts[label] = {"manifest_sha256": model.manifest_sha256,
                            "inference_calls": model.inference_calls, "calls": calls, "additional_inputs": extra}
        finally:
            model.close()
        assert path.read_bytes() == before, "host modified the input ZIP"
    left, right = hosts["reference"], hosts["independent"]
    assert left["manifest_sha256"] == right["manifest_sha256"]
    assert matches(left["calls"], right["calls"], atol, rtol), "hosts produced different results"
    assert matches(left["additional_inputs"], right["additional_inputs"], atol, rtol), "hosts differ on additional inputs"
    return {"package": str(path.relative_to(ROOT)), "spec_version": manifest["spec_version"],
            "profile": manifest["profile"], "zip_sha256_before": sha(before),
            "zip_sha256_after": sha(path.read_bytes()), "fixture_count": len(fixtures),
            "onnx_weights_sha256": weights_sha256, "golden_calls_per_host": len(order),
            "additional_random_calls_per_host": len(extra_inputs),
            "atol": atol, "rtol": rtol, "hosts": hosts}


def make_bad_zip(original, destination, kind):
    with zipfile.ZipFile(original) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    manifest = json.loads(entries["model-package.json"])
    if kind == "wrong-digest":
        path = manifest["backend"]["weights"]
        entries[path] += b"tampered"
    elif kind == "undeclared-file":
        entries["undeclared.txt"] = b"not declared"
    elif kind == "unsafe-path":
        entries["../escape.txt"] = b"escape"
    elif kind == "nonportable-colon-path":
        entries["artifacts:name/unused"] = b"invalid"
    elif kind == "unsupported-version":
        manifest["spec_version"] = "0.2.999"
    elif kind == "unknown-profile":
        manifest["profile"] = "not-a-profile"
    elif kind == "unknown-required-capability":
        manifest["required_capabilities"].append("unknown-required")
    elif kind == "shape-disagreement":
        manifest["source_input"]["shape"][0] += 1
    elif kind == "golden-category-disagreement":
        fixtures = manifest["golden"]["fixtures"]
        if manifest["profile"] == "classification-onnx-cpu-v1":
            fixtures[0]["expected"]["label"] = "not-the-declared-label"
        else:
            first = next(x for x in fixtures if x["expected"]["detections"])
            first["expected"]["detections"][0]["label"] = "not-the-declared-label"
    elif kind != "duplicate-manifest":
        raise ValueError(kind)
    entries["model-package.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            if kind == "duplicate-manifest":
                archive.writestr("model-package.json", entries["model-package.json"])


def negative_cases():
    cases = []
    kinds = ("wrong-digest", "undeclared-file", "unsafe-path", "nonportable-colon-path", "unsupported-version",
             "unknown-profile", "unknown-required-capability", "shape-disagreement",
             "golden-category-disagreement", "duplicate-manifest")
    with tempfile.TemporaryDirectory(prefix="vlmp-unified-negative-") as directory:
        for profile in ("unified-classification", "unified-detection"):
            for kind in kinds:
                path = Path(directory) / f"{profile}-{kind}.zip"
                make_bad_zip(PACKAGES[profile], path, kind)
                outcomes = {}
                for label, loader in (("reference", reference_load), ("independent", independent_load)):
                    try:
                        model = loader(path)
                    except (ValueError, TypeError, RuntimeError) as exc:
                        outcomes[label] = {"rejected": True, "error_type": type(exc).__name__}
                    else:
                        model.close()
                        raise AssertionError(f"{label} accepted invalid package {profile}/{kind}")
                cases.append({"profile": profile, "case": kind, "hosts": outcomes})
        for profile in ("unified-classification", "unified-detection"):
            original = PACKAGES[profile].read_bytes()
            _, fixtures, _, _ = fixture_set(original)
            image = fixtures[0][1]
            for label, loader in (("reference", reference_load), ("independent", independent_load)):
                path = Path(directory) / f"runtime-{profile}-{label}.zip"
                path.write_bytes(original)
                model = loader(path)
                tampered = False
                try:
                    if profile == "unified-detection":
                        image = next(frame for _, frame, expected in fixtures if expected["detections"])
                    expected_output = model.predict(image.copy())
                    public_manifest = model.manifest
                    public_manifest["categories"][0]["label"] = "externally-mutated-label"
                    public_manifest["backend"]["input_shape"][2] += 1
                    if profile == "unified-detection":
                        public_manifest["result"]["threshold"] = 1.0
                    assert model.predict(image.copy()) == expected_output, "mutable public metadata affected inference"
                    cases.append({"profile": profile, "case": "public-manifest-mutation",
                                  "host": label, "unchanged": True})
                    for kind, bad_frame in (("wrong-dtype", image.astype(np.float32)),
                                            ("wrong-shape", image[:-1].copy())):
                        try:
                            model.predict(bad_frame)
                        except (ValueError, TypeError, RuntimeError):
                            cases.append({"profile": profile, "case": kind, "host": label, "rejected": True})
                        else:
                            raise AssertionError(f"{label} accepted invalid frame {profile}/{kind}")
                    path.write_bytes(original + b"modified-after-load")
                    tampered = True
                    try:
                        model.predict(image.copy())
                    except (ValueError, TypeError, RuntimeError):
                        cases.append({"profile": profile, "case": "zip-changed-after-load",
                                      "host": label, "rejected": True})
                    else:
                        raise AssertionError(f"{label} accepted changed input ZIP for {profile}")
                finally:
                    try:
                        model.close()
                    except (ValueError, TypeError, RuntimeError):
                        if not tampered:
                            raise
    return cases


def independent_import_guard():
    """Run each new profile in a fresh interpreter with no reference loader imported."""
    code = """\
import io, json, sys, zipfile
import numpy as np
sys.path.insert(0, sys.argv[1])
from independent.host import load_package
with zipfile.ZipFile(sys.argv[2]) as archive:
    manifest = json.loads(archive.read('model-package.json'))
    image = np.load(io.BytesIO(archive.read(manifest['golden']['fixtures'][0]['input'])), allow_pickle=False)
model = load_package(sys.argv[2])
try:
    model.predict(image)
    forbidden = [name for name in sys.modules if name == 'reference' or name.startswith('reference.')
                 or name == 'public_model' or name.startswith('model_package_v02')]
    assert not forbidden, forbidden
    print(json.dumps({'status': 'pass', 'profile': manifest['profile'], 'forbidden_imports': forbidden}))
finally:
    model.close()
"""
    outcomes = []
    for key in ("unified-classification", "unified-detection"):
        process = subprocess.run([sys.executable, "-I", "-c", code, str(BASE), str(PACKAGES[key])],
                                 capture_output=True, text=True, check=True)
        outcome = json.loads(process.stdout)
        assert outcome["status"] == "pass" and not outcome["forbidden_imports"]
        outcomes.append(outcome)
    return outcomes


def source_paths():
    return [Path(__file__), BASE / "reference/runtime.py", BASE / "independent/host.py",
            BASE / "independent/_storage.py", BASE / "spec/model-package.schema.json",
            BASE / "spec/SPECIFICATION.ru.md", BASE / "spec/SPECIFICATION.en.md",
            BASE / "tools/build_examples.py", BASE / "tests/test_schema.py",
            BASE / "tests/test_two_hosts_invalid_outputs.py", BASE / "tests/test_comparison_runner.py",
            BASE / "reference/tests/test_reference.py", BASE / "independent/tests/test_host_safety.py",
            ROOT / "model/src/public_model.py", ROOT / "model/spec/model-package.schema.json",
            ROOT / "drafts/v0.2/model/src/model_package_v02/runtime.py",
            ROOT / "drafts/v0.2/model/spec/model-package.schema.json"]


def source_hashes():
    return {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in source_paths()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    args = parser.parse_args()
    if args.output:
        protected = {path.resolve() for path in PACKAGES.values()}
        protected.update(path.resolve() for path in source_paths())
        if args.output.resolve() in protected:
            raise SystemExit("Output path must not overwrite an input package or verification source")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text('{"status":"running"}\n', encoding="utf-8")
    try:
        if not __debug__:
            raise RuntimeError("This verification runner requires assertions; do not use Python -O or PYTHONOPTIMIZE")
        global np, onnx, ort, reference_load, independent_load
        import numpy as np
        import onnx
        import onnxruntime as ort
        sys.path.insert(0, str(BASE))
        from reference.runtime import load_package as reference_load
        from independent.host import load_package as independent_load
        sources_before = source_hashes()
        for name, path in PACKAGES.items():
            assert sha(path.read_bytes()) == EXPECTED_ZIP_SHA256[name], f"unexpected package bytes: {name}"
        results = {name: run_case(path) for name, path in PACKAGES.items()}
        negatives = negative_cases()
        guards = independent_import_guard()
        assert source_hashes() == sources_before, "sources changed during comparison"
        report = {"status": "pass", "checked_at": datetime.now(timezone.utc).isoformat(),
                  "packages": results, "negative_cases": negatives,
                  "independent_import_guard": guards, "sources_sha256": sources_before,
                  "environment": {"platform": platform.platform(), "python": platform.python_version(),
                                  "numpy": np.__version__, "onnx": onnx.__version__, "onnxruntime": ort.__version__},
                  "limitations": ["Both hosts run locally on CPU and use common ONNX Runtime and formal schemas.",
                                  "Reference legacy counters count successful legacy loader calls, not separate ORT instrumentation.",
                                  "One published ZIP per legacy format was tested; compatibility with all legacy packages is not established.",
                                  "Synthetic, untrained models do not establish analytics quality or third-party platform compatibility.",
                                  "Only the declared static CPU profiles are tested; GPU, other backends and arbitrary models are not covered."]}
    except Exception as exc:
        if args.output:
            args.output.write_text(json.dumps({"status": "fail", "error_type": type(exc).__name__,
                                               "message": str(exc)}, ensure_ascii=False) + "\n", encoding="utf-8")
        raise
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
