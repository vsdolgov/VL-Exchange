"""Build an independent, self-contained Python scenario for VLSP pilot tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "examples/target-scenario"

SCENARIO_CODE = '''"""Standalone scenario: emit once when a classification streak reaches a threshold.

This synthetic example illustrates the host interface; it does not import the
VL block runtime or require a visual editor. Production delivery is encrypted.
"""


class Scenario:
    def __init__(self, *, target_label, min_score, min_count):
        self.target_label = target_label
        self.min_score = min_score
        self.min_count = min_count

    def process(self, inputs, context, state):
        classification = inputs["classification"]
        matched = (classification["label"] == self.target_label
                   and classification["score"] >= self.min_score)
        previous_count = state["count"]
        count = min(previous_count + 1, 1_000_000) if matched else 0
        crossed = matched and previous_count < self.min_count <= count
        events = []
        if crossed:
            events.append({"event_type": "org.vlexchange.target-observed.v1",
                           "payload": {"label": classification["label"],
                                       "score": classification["score"],
                                       "count": count}})
        return {"events": events, "state": {"count": count}}
'''


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    code, golden = PACKAGE / "code", PACKAGE / "golden"
    code.mkdir(parents=True, exist_ok=True)
    golden.mkdir(parents=True, exist_ok=True)
    (code / "__init__.py").write_text("", encoding="utf-8")
    (code / "scenario.py").write_text(SCENARIO_CODE, encoding="utf-8")
    sequence = json.loads((ROOT / "spec/example-sequence.json").read_text(encoding="utf-8"))
    write_json(golden / "basic.json", sequence)
    manifest = json.loads((ROOT / "spec/example-manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = {p.relative_to(PACKAGE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(PACKAGE.rglob("*"))
                         if p.is_file() and p.name != "scenario-package.json"}
    write_json(PACKAGE / "scenario-package.json", manifest)
    archive = ROOT / "reports/vlsp-target-scenario.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(PACKAGE.rglob("*")):
            if path.is_file():
                item = zipfile.ZipInfo(path.relative_to(PACKAGE).as_posix(), date_time=(2026, 10, 31, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                item.external_attr = 0o100644 << 16
                output.writestr(item, path.read_bytes())
    print(json.dumps({"package": str(PACKAGE), "archive": str(archive),
                      "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "manifest_sha256": hashlib.sha256((PACKAGE / "scenario-package.json").read_bytes()).hexdigest(),
                      "inventory_files": len(manifest["files"]), "golden_steps": len(sequence["steps"])}, indent=2))


if __name__ == "__main__":
    main()
