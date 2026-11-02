"""Compare separately protected model ZIPs in the two real VLSP transports.

The independent path is sent through the pinned C++ VideoPipe graph. All keys,
license servers, encrypted packages and native stderr are temporary.
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

import numpy as np

if not __debug__:
    raise RuntimeError('Run without Python optimization')
HERE = Path(__file__).resolve().parent
INTEGRATION = HERE.parent
ROOT = HERE.parents[3]
PILOT = ROOT / 'drafts/v0.2/videopipe-pilot'
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PILOT / 'python'))
from run_pilot import Native
import bootstrap


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj

reference = module('protected_reference_worker', HERE / 'reference_worker.py')
vectors = module('protected_golden_vectors', INTEGRATION / 'tests/test_workers.py')

class NativeIndependent(Native):
    def __init__(self, binary, report_dir):
        self.stderr_path = report_dir / 'videopipe-stderr.txt'
        self.stderr = self.stderr_path.open('w')
        self.process = subprocess.Popen([str(binary), '--python', sys.executable,
            '--worker', str(HERE / 'independent_worker.py')], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
            start_new_session=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
        self.seen = self.frames = 0


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    paths = []
    for directory in ('drafts/v0.2/vlsp-unified-integration/model-protected',
                      'drafts/v0.2/vlsp-unified-integration/reference',
                      'drafts/v0.2/vlsp-unified-integration/independent',
                      'drafts/v0.2/vlsp-unified-integration/fixtures',
                      'drafts/v0.2/videopipe-pilot/python',
                      'drafts/v0.2/videopipe-pilot/independent',
                      'drafts/v0.2/vlmp-unified/reference',
                      'drafts/v0.2/vlmp-unified/independent',
                      'drafts/v0.2/security-runtime/src',
                      'drafts/v0.2/scenario/src', 'drafts/v0.2/model/src',
                      'drafts/v0.2/scenario/spec', 'drafts/v0.2/model/spec',
                      'drafts/v0.2/vlmp-unified/spec'):
        paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file() and
                     p.suffix in ('.py', '.zip', '.json') and
                     not any(x in p.parts for x in ('__pycache__', 'reports', 'tests')))
    paths += [INTEGRATION / 'tests/test_workers.py', ROOT / 'model/src/public_model.py',
              ROOT / 'drafts/v0.2/scenario/examples/vlsp-color-cascade.zip']
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(paths))}


def clean_error(response):
    return {k: v for k, v in response.get('error', {}).items() if k != 'message'}


def comparison(binary, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)  # a failed run must not preserve a stale PASS
    build = json.loads((PILOT / 'build/cpp-build-manifest.json').read_text())
    binary_hash = digest(binary)
    assert binary_hash == build['binaries']['build/adapter/vlsp_videopipe']
    for name, expected in build['adapter_sources'].items():
        assert digest(PILOT / 'cpp' / name) == expected
    vendor = ROOT.parents[2] / 'videopipe-pilot/vendor/VideoPipe'
    if not vendor.exists(): vendor = PILOT / 'vendor/VideoPipe'
    assert subprocess.check_output(['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True).strip() == build['videopipe']['commit']
    assert not subprocess.check_output(['git', '-C', str(vendor), 'status', '--porcelain', '--untracked-files=no'], text=True)
    before = snapshot()
    pins = json.loads((INTEGRATION / 'fixtures/pins.json').read_text())
    for alias in ('scenario', 'detector', 'classifier_a', 'classifier_b'):
        assert digest(INTEGRATION / 'fixtures' / (alias + '.zip')) == pins[alias]['zip_sha256']
    (first, second), golden = vectors.golden_commands()
    summaries, all_rows = [], []
    for policy in ('offline', 'online'):
        failed = deepcopy(second)
        failed['fault'] = {'alias': 'classifier_b', 'mode': 'expire_model_after_return', 'call': 2}
        commands = [{'op': 'open', 'policy': policy}, first, second, {'op': 'close'},
                    {'op': 'open', 'policy': policy}, first, failed, {'op': 'close'}, {'op': 'shutdown'}]
        with tempfile.TemporaryDirectory(prefix='vlsp-model-compare-') as temp:
            left, right = reference.Session(), NativeIndependent(binary, Path(temp))
            finished = False
            try:
                previous = None
                for index, original in enumerate(commands):
                    cmd = {**deepcopy(original), 'id': index + 1}
                    a, b = left.handle(cmd), right.request(cmd)
                    for r in (a, b):
                        assert r['id'] == cmd['id']
                        if index in (0, 4):
                            assert r['ok'] and r['opened']['protected'] and r['opened']['models_protected']
                            assert r['opened']['scenario_zip_sha256'] == pins['scenario']['zip_sha256']
                            assert len(r['opened']['model_licenses']) == 3
                            for alias in ('detector', 'classifier_a', 'classifier_b'):
                                signed = r['opened']['model_licenses'][alias]
                                assert signed['mode'] == policy and signed['license_id'] == 'model-' + alias
                                for field in ('package_sha256', 'manifest_sha256'):
                                    expected = pins[alias]['zip_sha256' if field == 'package_sha256' else field]
                                    assert signed[field] == expected
                        elif index in (1, 5):
                            assert r['ok'] and r['result'] == golden[0]
                        elif index == 2:
                            assert r['ok'] and r['result'] == golden[1]
                            assert len(r['result']['events']) == 1
                        elif index == 6:
                            assert not r['ok'] and clean_error(r) == {'stage':'security','code':'authorization_failed','fatal':False,'retryable':True}
                            assert r['snapshot'] == previous['snapshot']
                            assert r['published_events'] == previous['published_events']
                            assert [t['alias'] for t in r['trace']] == ['detector','tracker','classifier_a','classifier_b','classifier_a','classifier_b']
                            assert r['trace'][-1]['fault_injection'] == 'expire_model_after_return'
                            assert r['trace'][-1]['onnx_input']['sha256']
                        else: assert r['ok']
                    for key in ('ok','snapshot','published_events','calls','scenario_imports'):
                        assert a[key] == b[key], (policy, index, key, a[key], b[key])
                    assert a.get('result') == b.get('result')
                    assert clean_error(a) == clean_error(b)
                    assert a['trace'] == b['trace'], (policy,index,'trace mismatch')
                    assert b['independence']['guard_active'] and not b['independence']['forbidden_import_attempts'] and not b['independence']['loaded_reference_modules']
                    for t in a['trace']:
                        if t['service'] == 'model':
                            alias = t['alias']
                            assert t['inference_executed'] and t['zip_sha256'] == pins[alias]['zip_sha256']
                            assert t['manifest_sha256'] == pins[alias]['manifest_sha256']
                            image = cmd['image']
                            raw = bytes.fromhex(image['bgr_hex'])
                            assert t['frame_sha256'] == hashlib.sha256(raw).hexdigest()
                            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(image['height'], image['width'], 3)
                            crop = pixels
                            if alias != 'detector':
                                x1, y1, x2, y2 = t['request']['bbox']
                                h, w = pixels.shape[:2]
                                crop = pixels[math.floor(y1*h):math.ceil(y2*h), math.floor(x1*w):math.ceil(x2*w)]
                            assert t['crop_shape'] == list(crop.shape)
                            assert t['crop_sha256'] == hashlib.sha256(crop.tobytes()).hexdigest()
                            assert t['onnx_input']['sha256'] and t['onnx_outputs']
                    all_rows.append({'policy':policy,'command':cmd,'reference':a,'videopipe':b})
                    summaries.append({'policy':policy,'id':index+1,'op':cmd['op'], 'ok':a['ok'], 'error':clean_error(a)})
                    previous = a
                right.finish()
                finished = True
            finally:
                left.shutdown()
                if not finished: right.abort()
        print(json.dumps({'policy':policy,'status':'pass'}), flush=True)
    assert before == snapshot(), 'Source or input package changed during run'
    assert digest(binary) == binary_hash, 'Native binary changed during run'
    report = {'status':'pass','checked_at':datetime.now(timezone.utc).isoformat(),
        'comparison':'protected-scenario-and-protected-vlmp-models-reference-vs-real-videopipe',
        'policies':['offline','online'],'commands_compared':len(summaries),
        'scenario_zip_sha256':pins['scenario']['zip_sha256'],
        'models':{alias:pins[alias] for alias in ('detector','classifier_a','classifier_b')},
        'videopipe_commit':build['videopipe']['commit'],'native_binary_sha256':binary_hash,
        'sources_sha256':before,'comparisons':summaries,'rows':all_rows,
        'model_lease_before_scenario_commit':'Original model leases are checked by composite scenario lease before state/events commit.',
        'expiry_probe':'Only test model clock expires after last classification returns, while scenario clock remains valid.',
        'limitations':['Test-only local issuer with ephemeral keys', 'Synthetic CPU ONNX models and lossless 4x2 frames',
                       'Composite lease is an experiment-specific integration adapter for a synchronous worker; concurrent process calls are not supported',
                       'No live cameras, production licensing server, hardware isolation, GPU, load or async MLLM']}
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    print(json.dumps({'status':'pass','commands':len(summaries),'report':str(output)}),flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary',type=Path,default=PILOT / 'build/adapter/vlsp_videopipe')
    parser.add_argument('--output',type=Path,default=HERE / 'reports/comparison.json')
    args=parser.parse_args()
    comparison(args.binary.resolve(), args.output.resolve())
