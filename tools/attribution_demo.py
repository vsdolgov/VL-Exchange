"""Offline demonstration of attribution in a trusted synthetic VLMP package.

This uses two local hosts, not a third-party platform. The source frame is a
synthetic PNG; only a separate copy receives the visible attribution overlay.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

from model_archive_roundtrip import extract, inspect_archive

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'model/examples/attribution-channel-classifier'
KEY = 'org.vl-exchange.attribution'

# A compact built-in 5x7 uppercase font, used only to label the synthetic image.
FONT = {
    'A':['01110','10001','10001','11111','10001','10001','10001'],
    'B':['11110','10001','10001','11110','10001','10001','11110'],
    'C':['01111','10000','10000','10000','10000','10000','01111'],
    'D':['11110','10001','10001','10001','10001','10001','11110'],
    'E':['11111','10000','10000','11110','10000','10000','11111'],
    'F':['11111','10000','10000','11110','10000','10000','10000'],
    'G':['01111','10000','10000','10111','10001','10001','01111'],
    'H':['10001','10001','10001','11111','10001','10001','10001'],
    'I':['11111','00100','00100','00100','00100','00100','11111'],
    'J':['00111','00010','00010','00010','10010','10010','01100'],
    'K':['10001','10010','10100','11000','10100','10010','10001'],
    'L':['10000','10000','10000','10000','10000','10000','11111'],
    'M':['10001','11011','10101','10101','10001','10001','10001'],
    'N':['10001','11001','10101','10011','10001','10001','10001'],
    'O':['01110','10001','10001','10001','10001','10001','01110'],
    'P':['11110','10001','10001','11110','10000','10000','10000'],
    'Q':['01110','10001','10001','10001','10101','10010','01101'],
    'R':['11110','10001','10001','11110','10100','10010','10001'],
    'S':['01111','10000','10000','01110','00001','00001','11110'],
    'T':['11111','00100','00100','00100','00100','00100','00100'],
    'U':['10001','10001','10001','10001','10001','10001','01110'],
    'V':['10001','10001','10001','10001','10001','01010','00100'],
    'W':['10001','10001','10001','10101','10101','10101','01010'],
    'X':['10001','10001','01010','00100','01010','10001','10001'],
    'Y':['10001','10001','01010','00100','00100','00100','00100'],
    'Z':['11111','00001','00010','00100','01000','10000','11111'],
    ' ':['00000']*7,
    '|':['00100']*7,
    '?':['01110','10001','00001','00010','00100','00000','00100'],
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def chunk(kind, content):
    return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff)


def png(width, height, rgb):
    rows = b''.join(b'\0' + bytes(rgb[y * width * 3:(y + 1) * width * 3]) for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def overlay_label(attribution):
    """Keep every author visible or fail when this tiny renderer cannot do so.

    This demo's one-line font is limited to 50 ASCII letters, spaces, `|` and
    `?`. A real platform needs appropriate text shaping and layout for its UI.
    """
    authors = [item['name'] for item in attribution['authors']]
    label = attribution.get('preferred_text') or 'Analytics by ' + ' | '.join(authors)
    for author in authors:
        if not re.search(r'(?<!\w)' + re.escape(author) + r'(?!\w)', label, re.IGNORECASE):
            label += ' | ' + author
    if not label.isascii() or any(char not in FONT for char in label.upper()):
        raise ValueError('demo overlay font supports only ASCII letters, spaces, | and ?')
    label = label.upper()
    if len(label) > 50:
        raise ValueError('demo overlay text is too long for the synthetic frame (maximum 50 characters)')
    return label


def frame(attribution):
    """Create an original synthetic frame and a separately annotated copy."""
    width, height = 640, 360
    original = bytearray()
    for y in range(height):
        for x in range(width):
            original.extend((22 + x // 16, 46 + y // 12, 58 + (x + y) // 24))
    # A synthetic object, never camera data or a real image.
    for y in range(80, 250):
        for x in range(245, 395):
            i = (y * width + x) * 3
            original[i:i+3] = bytes((75, 122, 125))
    annotated = original.copy()
    for y in range(317, 352):
        for x in range(8, 632):
            i = (y * width + x) * 3
            annotated[i:i+3] = bytes((10, 17, 24))
    label = overlay_label(attribution)
    for i, letter in enumerate(label):
        for gy, row in enumerate(FONT.get(letter, FONT['?'])):
            for gx, bit in enumerate(row):
                if bit == '1':
                    for dy in range(2):
                        for dx in range(2):
                            x, y = 18 + i * 12 + gx * 2 + dx, 326 + gy * 2 + dy
                            offset = (y * width + x) * 3
                            annotated[offset:offset+3] = bytes((241, 249, 249))
    return png(width, height, original), png(width, height, annotated)


def make_archive():
    """Build one in-memory archive without altering the source package."""
    manifest = json.loads((PACKAGE / 'model-package.json').read_text(encoding='utf-8'))
    source = {'model-package.json': (PACKAGE / 'model-package.json').read_bytes()}
    for name, digest in manifest['files'].items():
        path = PACKAGE / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(PACKAGE.resolve()):
            raise ValueError('invalid package file path')
        source[name] = path.read_bytes()
        if sha(source[name]) != digest:
            raise ValueError('package digest mismatch')
    actual = {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob('*') if p.is_file()}
    if actual != set(source):
        raise ValueError('package contains files absent from its inventory')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(source.items()):
            archive.writestr(name, contents)
    return buffer.getvalue(), manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path, help='new or empty directory for generated demo materials')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output == PACKAGE.resolve() or output.is_relative_to(PACKAGE.resolve()) or PACKAGE.resolve().is_relative_to(output):
        parser.error('output directory must be separate from the source package')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error('output directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    raw, manifest = make_archive()
    members = inspect_archive(raw)
    reports = {}
    commands = {
        'reference': [sys.executable, str(ROOT / 'model/src/public_model.py'), 'golden'],
        'independent': [sys.executable, str(ROOT / 'model/tools/independent_host.py')],
    }
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    with tempfile.TemporaryDirectory(prefix='vl-attribution-') as temp:
        for name, command in commands.items():
            destination = Path(temp) / name
            extract(members, destination)
            completed = subprocess.run([*command, str(destination)], capture_output=True, text=True,
                                       cwd=temp, env=env, timeout=120, check=True)
            report = json.loads(completed.stdout)
            if report['status'] != 'pass' or not report['unchanged'] or report['manifest_sha256'] != sha(members['model-package.json']):
                raise ValueError(f'{name} host failed validation or changed the package')
            reports[name] = report
    expected_attribution = manifest['extensions'][KEY]['data']
    if any(report.get('attribution') != expected_attribution for report in reports.values()):
        raise ValueError('hosts disagree on attribution metadata')
    # Use what the receiving host actually returned, after verifying equality.
    attribution = reports['independent']['attribution']
    if reports['reference']['calls'] != reports['independent']['calls']:
        raise ValueError('hosts disagree on inference results')
    original, annotated = frame(attribution)
    event = {'event_type': 'synthetic.classification', 'model_id': manifest['model_id'],
             'package_version': manifest['version'], 'attribution': attribution,
             'result': reports['reference']['calls'][0]['result']}
    def save_json(name, obj):
        (output / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    for name, report in reports.items():
        save_json(f'{name}-host.json', report)
    save_json('event.json', event)
    (output / 'frame-original.png').write_bytes(original)
    original_digest = sha((output / 'frame-original.png').read_bytes())
    (output / 'frame-annotated.png').write_bytes(annotated)
    if sha((output / 'frame-original.png').read_bytes()) != original_digest or original == annotated:
        raise ValueError('original changed or no overlay produced')
    summary = {'status': 'pass', 'archive_sha256': sha(raw), 'manifest_sha256': sha(members['model-package.json']),
               'two_separate_extractions': True, 'host_attribution_equal': True,
               'host_inference_equal': True, 'original_frame_unchanged': True,
               'original_frame_sha256': original_digest, 'annotated_frame_sha256': sha(annotated),
               'event_attribution_equal': event['attribution'] == attribution,
               'limitations': ['Synthetic trusted package and synthetic frame only.',
                               'No third-party platform or video encoder is tested.',
                               'Demo overlay supports at most 50 ASCII letters, spaces, | and ?; real platforms need suitable layout and text shaping.',
                               'Hosts are separate local implementations using the same model and ONNX Runtime.',
                               'Manifest hashes and attribution metadata do not authenticate an unsigned publisher.']}
    save_json('demo-report.json', summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
