"""Compare protected VLSP 0.2 execution with unified VLMP packages.

Requires the previously built native VideoPipe adapter and loopback networking.
Fail closed: a failed run cannot leave an old comparison.json claiming PASS.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

if not __debug__:
    raise RuntimeError('Do not run this experiment with Python optimization (-O)')
ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PILOT = ROOT / 'drafts/v0.2/videopipe-pilot'
sys.path.insert(0, str(PILOT / 'python'))
import bootstrap
from cases import build_cases
from run_pilot import Native, environment

spec = importlib.util.spec_from_file_location('unified_scenario_reference', HERE / 'reference/worker.py')
refmod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refmod
spec.loader.exec_module(refmod)

class NativeIndependent(Native):
    def __init__(self, binary, report_dir):
        self.stderr_path = report_dir / 'videopipe-stderr.txt'
        self.stderr = self.stderr_path.open('w')
        self.process = subprocess.Popen([str(binary), '--python', sys.executable,
            '--worker', str(HERE / 'independent/worker.py')], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
            start_new_session=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
        self.seen = self.frames = 0

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def snapshot():
    paths = []
    for d in ('drafts/v0.2/vlsp-unified-integration', 'drafts/v0.2/videopipe-pilot/python',
              'drafts/v0.2/videopipe-pilot/independent', 'drafts/v0.2/scenario/src',
              'drafts/v0.2/model/src', 'drafts/v0.2/security-runtime/src',
              'drafts/v0.2/vlmp-unified/reference', 'drafts/v0.2/vlmp-unified/independent',
              'drafts/v0.2/scenario/spec', 'drafts/v0.2/model/spec', 'model/spec'):
        paths += [p for p in (ROOT / d).rglob('*') if p.is_file() and p.suffix in ('.py', '.json', '.zip')
                  and '__pycache__' not in p.parts and 'reports' not in p.parts]
    paths += [ROOT / 'model/src/public_model.py', ROOT / 'drafts/v0.2/scenario/examples/vlsp-color-cascade.zip']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))}

def error(r):
    return {k: v for k, v in r.get('error', {}).items() if k != 'message'}

def trace(r):
    return [{k: v for k, v in x.items() if k not in ('returned_output', 'returned')} for x in r['trace']]

def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

def suites(package):
    cases = build_cases(package)
    baseline = cases[0]['commands']
    a1, a2 = deepcopy(baseline[1]), deepcopy(baseline[3])
    def op(name, **kw): return {'op': name, **kw, '_expect': {'ok': True}}
    def reject(code, stage, retryable=False):
        return {'ok': False, 'error': {'code': code, 'stage': stage, 'fatal': False, 'retryable': retryable}, 'no_commit': True}
    expired = deepcopy(a2)
    expired['fault'] = {'alias': 'classifier_b', 'mode': 'expire_lease', 'call': 2}
    expired['_expect'] = reject('authorization_failed', 'security', True)
    cases.append({'name': 'lease-expires-after-real-inference-before-commit', 'commands': [
        op('open'), a1, expired, op('close')]})
    before = deepcopy(a2)
    before['expire_before'] = True
    before['_expect'] = {**reject('authorization_failed', 'security', True), 'no_calls': True}
    cases.append({'name': 'lease-expires-before-process', 'commands': [op('open'), deepcopy(a1), before, op('close')]})
    for policy in ('offline', 'online'):
        for fault in ('expired',) + (('revoked',) if policy == 'online' else ()):
            cmd = op('open', policy=policy, license_fault=fault)
            cmd['_expect'] = reject('authorization_failed', 'security')
            cmd['_expect']['no_calls'] = True
            cases.append({'name': f'{policy}-{fault}-before-import', 'commands': [cmd, op('close')]})
    expanded = []
    for policy in ('offline', 'online'):
        for case in cases:
            if case['name'].endswith('-before-import'):
                if not case['name'].startswith(policy + '-'):
                    continue
            c = deepcopy(case)
            c['name'] = policy + '/' + c['name']
            for cmd in c['commands']:
                if cmd['op'] == 'open': cmd['policy'] = policy
            expanded.append(c)
    return expanded

def validate(cmd, expected, a, prev, pins):
    assert a['id'] == cmd['id']
    assert a['ok'] is expected['ok'], (cmd, a.get('error'))
    if 'result' in expected: assert a['result'] == expected['result'], (cmd, a['result'], expected['result'])
    if 'error' in expected:
        assert 'result' not in a
        for k, v in expected['error'].items(): assert a['error'][k] == v, (cmd, a['error'])
    if expected.get('no_commit'):
        if cmd['op'] == 'open': assert a['snapshot'] == {'state': {}, 'order': {}} and a['published_events'] == []
        else: assert a['snapshot'] == prev['snapshot'] and a['published_events'] == prev['published_events']
    if expected.get('no_calls'):
        assert a['trace'] == []
        if cmd['op'] != 'open': assert a['calls'] == prev['calls']
    if cmd['op'] == 'open':
        if a['ok']:
            assert a['scenario_imports'] == 2
            assert a['opened']['protected'] is True and a['opened']['policy'] == cmd['policy']
            assert a['opened']['scenario_zip_sha256'] == pins['scenario']['zip_sha256']
            for alias in ('detector', 'classifier_a', 'classifier_b'):
                for k in ('manifest_sha256', 'weights_sha256', 'zip_sha256'):
                    assert a['opened']['models'][alias][k] == pins[alias][k]
        else: assert a['scenario_imports'] == 0
    if cmd['op'] == 'process':
        raw = bytes.fromhex(cmd['image']['bgr_hex'])
        h, w = cmd['image']['height'], cmd['image']['width']
        import numpy as np
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
        for t in a['trace']:
            assert t['context'] == cmd['context']
            if t['service'] != 'model': continue
            alias = t['alias']
            assert t['inference_executed'] and t['backend'] == 'CPUExecutionProvider'
            assert t['frame_sha256'] == hashlib.sha256(raw).hexdigest()
            for k in ('manifest_sha256', 'weights_sha256', 'zip_sha256'):
                assert t[k] == pins[alias][k]
            crop = pixels
            if alias != 'detector':
                x1, y1, x2, y2 = t['request']['bbox']
                crop = pixels[math.floor(y1*h):math.ceil(y2*h), math.floor(x1*w):math.ceil(x2*w)]
            assert t['crop_shape'] == list(crop.shape)
            assert t['crop_sha256'] == hashlib.sha256(crop.tobytes()).hexdigest()
            assert t['onnx_input']['sha256'] and t['onnx_outputs']

def run(binary, reports):
    reports.mkdir(parents=True, exist_ok=True)
    for name in ('comparison.json', 'comparison-failed.json', 'reference.json', 'videopipe.json'):
        (reports / name).unlink(missing_ok=True)
    build = json.loads((PILOT / 'build/cpp-build-manifest.json').read_text())
    assert sha(binary) == build['binaries']['build/adapter/vlsp_videopipe']
    for path, digest in build['adapter_sources'].items(): assert sha(PILOT / 'cpp' / path) == digest
    vendor = ROOT.parents[2] / 'videopipe-pilot/vendor/VideoPipe'
    if not vendor.exists(): vendor = PILOT / 'vendor/VideoPipe'
    assert subprocess.check_output(['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True).strip() == build['videopipe']['commit']
    assert not subprocess.check_output(['git', '-C', str(vendor), 'status', '--porcelain', '--untracked-files=no'], text=True)
    initial = snapshot()
    binary_sha = sha(binary)
    pins = json.loads((HERE / 'fixtures/pins.json').read_text())
    assert sha(ROOT / 'drafts/v0.2/scenario/examples/vlsp-color-cascade.zip') == pins['previous_scenario_zip_sha256']
    for alias in ('scenario', 'detector', 'classifier_a', 'classifier_b'):
        assert sha(HERE / 'fixtures' / (alias + '.zip')) == pins[alias]['zip_sha256']
    assert len({pins[a]['weights_sha256'] for a in ('detector', 'classifier_a', 'classifier_b')}) == 3
    ref = native = None
    rows_a, rows_b, compared = [], [], []
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix='vlsp-case-') as temp:
            with zipfile.ZipFile(HERE / 'fixtures/scenario.zip') as archive:
                for member in archive.infolist():
                    target = (Path(temp) / member.filename).resolve()
                    if not target.is_relative_to(Path(temp).resolve()):
                        raise ValueError('Unsafe archive path')
                archive.extractall(temp)
            cases = suites(Path(temp))
            ref, native = refmod.Session(), NativeIndependent(binary, reports)
            prev_a = prev_b = {'snapshot': {'state': {}, 'order': {}}, 'published_events': [], 'calls': {}}
            idx = 0
            for suite in cases:
                for original in suite['commands']:
                    cmd = deepcopy(original); expected = cmd.pop('_expect'); idx += 1; cmd['id'] = idx
                    a, b = ref.handle(cmd), native.request(cmd)
                    validate(cmd, expected, a, prev_a, pins); validate(cmd, expected, b, prev_b, pins)
                    assert b['independence']['guard_active'] and not b['independence']['forbidden_import_attempts']
                    assert not b['independence']['loaded_reference_modules']
                    for key in ('ok', 'snapshot', 'published_events', 'calls', 'scenario_imports'):
                        assert a[key] == b[key], (suite['name'], cmd, key, a[key], b[key])
                    assert (a['lifecycle'] is None) == (b['lifecycle'] is None)
                    if a['lifecycle'] is not None:
                        for flag in ('closed', 'failed', 'scenario_close_called', 'committed_calls'):
                            assert a['lifecycle'][flag] == b['lifecycle'][flag], (suite['name'], cmd, flag)
                    assert a.get('result') == b.get('result'), (suite['name'], cmd, 'result')
                    assert error(a) == error(b), (suite['name'], cmd, error(a), error(b))
                    assert trace(a) == trace(b), (suite['name'], cmd, trace(a), trace(b))
                    row = {'suite': suite['name'], 'command': cmd, 'expected': expected}
                    rows_a.append({**row, 'actual': a}); rows_b.append({**row, 'actual': b})
                    compared.append({'id': idx, 'suite': suite['name'], 'op': cmd['op'], 'ok': a['ok'], 'error': error(a)})
                    prev_a, prev_b = a, b
                print(json.dumps({'suite': suite['name'], 'status': 'pass'}), flush=True)
            end_a, end_b = ref.handle({'op': 'shutdown', 'id': idx+1}), native.request({'op': 'shutdown', 'id': idx+1})
            assert end_a['snapshot'] == end_b['snapshot']
            native.finish()
            assert snapshot() == initial, 'Source or fixture changed during run'
            assert sha(binary) == binary_sha, 'Native binary changed during run'
            success = True
    finally:
        if ref is not None: ref.shutdown()
        if native is not None and not success: native.abort()
        write(reports / 'reference.json', {'runs': rows_a})
        write(reports / 'videopipe.json', {'runs': rows_b})
        if not success: write(reports / 'comparison-failed.json', {'status': 'failed', 'completed': compared})
    fingerprints = {}
    for row in rows_a:
        for t in row['actual']['trace']:
            if t['alias'] in ('classifier_a', 'classifier_b') and t['request']['bbox'] == [0.0, 0.0, 0.5, 1.0]:
                fingerprints.setdefault(t['alias'], t['actual_output']['classification']['score'])
    assert abs(fingerprints['classifier_a'] - 0.5761168847658291) < 1e-6
    assert abs(fingerprints['classifier_b'] - 0.7869859933853149) < 1e-6
    result = {'status': 'pass', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'test': 'protected-vlsp-with-vlmp-0.2-reference-vs-independent-real-videopipe',
        'pins': pins, 'videopipe_commit': build['videopipe']['commit'], 'binary_sha256': binary_sha,
        'environment': environment(), 'sources_sha256': initial,
        'suites': len(cases), 'commands_compared': len(compared),
        'process_commands': sum(x['op'] == 'process' for x in compared),
        'expected_rejections': sum(not x['ok'] for x in compared),
        'classifier_red_crop_scores': fingerprints, 'comparisons': compared,
        'lifecycle_flags_and_committed_calls_match': True,
        'lifecycle_counter_differences': sum(a['actual']['lifecycle'] != b['actual']['lifecycle'] for a, b in zip(rows_a, rows_b)),
        'lifecycle_counter_note': 'Host-specific counters can differ, including whether a rejected input counts as a process call; lifecycle flags are compared.',
        'scope': 'scenario archive protected; model ZIPs validated by digest and profile, not separately encrypted',
        'limitations': ['Local loopback online issuer and ephemeral test keys', 'Synthetic CPU ONNX models and 4x2 lossless frames',
            'Independent implementations share security library and ONNX Runtime', 'No GPU, RTSP, load, asynchronous MLLM or external licensing service',
            'VideoPipe is a demonstration integration, not a production deployment']}
    write(reports / 'comparison.json', result)
    print(json.dumps({k: result[k] for k in ('status', 'suites', 'commands_compared', 'process_commands', 'expected_rejections')}), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, default=PILOT / 'build/adapter/vlsp_videopipe')
    parser.add_argument('--reports', type=Path, default=HERE / 'reports')
    args = parser.parse_args()
    run(args.binary.resolve(), args.reports.resolve())
