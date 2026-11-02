"""Snapshot an existing ZIP and its exact dependencies; do not rebuild it."""
import bootstrap
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

PILOT, WORKSPACE = bootstrap.PILOT, bootstrap.WORKSPACE
ORIGINAL_ZIP = WORKSPACE / "drafts/v0.2/scenario/examples/vlsp-color-cascade.zip"
EXPECTED_ZIP = "1461028eb6176c72bb0229eb8b28b7e697b6a4fcc1f7d420e095631bf4e0f332"
MODELS = {
    "detector": WORKSPACE / "drafts/v0.2/model/examples/vl-color-detector",
    "classifier_a": WORKSPACE / "model/examples/channel-classifier",
    "classifier_b": WORKSPACE / "drafts/v0.2/videopipe-pilot/fixtures/models/classifier_b",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValueError(f"Unsafe archive path {name!r}")
    return path


def prepare():
    fixtures = PILOT / "fixtures"
    fixtures.mkdir(exist_ok=True)
    if sha(ORIGINAL_ZIP) != EXPECTED_ZIP:
        raise ValueError("Original VLSP changed since the read-only audit; do not silently rebuild the pilot")
    archive = fixtures / "scenario.zip"
    if archive.exists() and sha(archive) != EXPECTED_ZIP:
        raise ValueError("Pilot ZIP was altered")
    if not archive.exists():
        shutil.copyfile(ORIGINAL_ZIP, archive)
    with zipfile.ZipFile(archive) as zf:
        if len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError("Duplicate archive entry")
        zip_files = {}
        for info in zf.infolist():
            safe_name(info.filename)
            if info.is_dir():
                raise ValueError("Pilot ZIP must contain only inventoried files")
            zip_files[info.filename] = hashlib.sha256(zf.read(info)).hexdigest()
    package = fixtures / "scenario"
    if not package.exists():
        package.mkdir()
        with zipfile.ZipFile(archive) as zf:
            if len(zf.namelist()) != len(set(zf.namelist())):
                raise ValueError("Duplicate archive entry")
            for info in zf.infolist():
                path = package / safe_name(info.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zf.read(info))
    if inventory(package) != zip_files:
        raise ValueError("Scenario oracle snapshot differs from the pinned executable ZIP")
    manifest = json.loads((package / "scenario-package.json").read_text())
    expected = {**manifest["files"], "scenario-package.json": sha(package / "scenario-package.json")}
    if inventory(package) != expected:
        raise ValueError("Scenario fixture inventory mismatch")
    models = {}
    for alias, original in MODELS.items():
        original_manifest = original / "model-package.json"
        m = json.loads(original_manifest.read_text())
        copied = fixtures / "models" / alias
        expected_files = {**m["files"], "model-package.json": sha(original_manifest)}
        for name, digest in expected_files.items():
            safe_name(name)
            source, target = original / name, copied / name
            if source.is_symlink() or sha(source) != digest:
                raise ValueError(f"Changed source dependency {alias}/{name}")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        if inventory(copied) != expected_files:
            raise ValueError(f"Changed pilot dependency {alias}")
        weights = m["profile"]["weights"] if alias == "detector" else m["backend"]["weights"]
        models[alias] = {"manifest_sha256": sha(original_manifest), "weights_sha256": sha(copied / weights),
                         "files": expected_files}
        declared = next(row for row in manifest["models"] if row["alias"] == alias)
        if declared["manifest_sha256"] != models[alias]["manifest_sha256"]:
            raise ValueError("Snapshot does not match the unchanged scenario's model link")
    if len({row["weights_sha256"] for row in models.values()}) != 3:
        raise ValueError("This pilot requires three different ONNX model files")
    pin = {"scenario_zip_sha256": EXPECTED_ZIP, "scenario_manifest_sha256": sha(package / "scenario-package.json"),
           "scenario_files": expected, "models": models}
    pin_file = fixtures / "pins.json"
    if pin_file.exists() and json.loads(pin_file.read_text()) != pin:
        raise ValueError("Existing pins do not match the current snapshot")
    pin_file.write_text(json.dumps(pin, indent=2) + "\n")
    return pin


if __name__ == "__main__":
    pin = prepare()
    print(json.dumps({"status": "pass", "scenario_zip_sha256": pin["scenario_zip_sha256"], "distinct_model_graphs": 3}))
