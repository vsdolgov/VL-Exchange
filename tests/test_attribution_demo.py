"""The published demonstration must transfer the same metadata to both hosts."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_two_hosts_export_and_annotated_frame(tmp_path):
    output = tmp_path / 'demo'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tools/attribution_demo.py'), '--output-dir', str(output)],
        cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, check=True, timeout=120,
    )
    summary = json.loads(result.stdout)
    reference = json.loads((output / 'reference-host.json').read_text())
    independent = json.loads((output / 'independent-host.json').read_text())
    event = json.loads((output / 'event.json').read_text())
    assert summary['status'] == 'pass'
    assert summary['two_separate_extractions'] and summary['original_frame_unchanged']
    assert reference['status'] == independent['status'] == 'pass'
    assert reference['attribution'] == independent['attribution'] == event['attribution']
    assert len(event['attribution']['authors']) == 2
    assert reference['calls'] == independent['calls']
    original = (output / 'frame-original.png').read_bytes()
    annotated = (output / 'frame-annotated.png').read_bytes()
    assert original.startswith(b'\x89PNG\r\n\x1a\n') and annotated.startswith(b'\x89PNG\r\n\x1a\n')
    assert original != annotated
