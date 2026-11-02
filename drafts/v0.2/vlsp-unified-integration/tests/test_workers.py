"""Portable worker smoke checks; native VideoPipe is not exercised here."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import numpy as np
import pytest

HERE = Path(__file__).resolve().parents[1]


def golden_commands():
    with zipfile.ZipFile(HERE / 'fixtures/scenario.zip') as archive:
        steps = json.loads(archive.read('golden/basic.json'))['steps']
        media = json.loads(archive.read('golden/media.json'))['frames']
        commands = []
        for step in (steps[0], steps[2]):
            frame, context = step['inputs']['frame'], step['context']
            row = next(row for row in media if all(row[k] == v for k, v in {
                'source_id': context['source_id'], 'run_id': context['run_id'],
                'frame_id': frame['frame_id'], 'frame_ref': frame['frame_ref']}.items()))
            pixels = np.load(io.BytesIO(archive.read(row['path'])), allow_pickle=False)
            commands.append({'op': 'process', 'inputs': step['inputs'], 'context': context,
                'image': {'width': frame['width'], 'height': frame['height'],
                          'bgr_hex': pixels.tobytes().hex()}})
        return commands, [steps[0]['expected'], steps[2]['expected']]


def invoke(worker, commands):
    text = ''.join(json.dumps(dict(command, id=index)) + '\n'
                   for index, command in enumerate(commands))
    proc = subprocess.run([sys.executable, str(HERE / worker / 'worker.py')], input=text,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=True,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(responses) == len(commands)
    return responses


@pytest.mark.parametrize('policy', ['offline', 'online'])
@pytest.mark.parametrize('fault', ['expire_before', 'expire_lease'])
def test_protected_workers_commit_and_reject_atomically(policy, fault):
    (first, second), expected = golden_commands()
    rejected = deepcopy(second)
    if fault == 'expire_before':
        rejected['expire_before'] = True
    else:
        rejected['fault'] = {'alias': 'classifier_b', 'mode': 'expire_lease', 'call': 2}
    commands = [
        {'op': 'open', 'policy': policy}, first, rejected, {'op': 'close'},
        {'op': 'open', 'policy': policy}, first, second, {'op': 'shutdown'},
    ]
    left, right = [invoke(worker, commands) for worker in ('reference', 'independent')]
    for responses in (left, right):
        opened, initial, denied, closed, reopened, replay, event, shutdown = responses
        assert opened['ok'] and opened['opened']['protected']
        assert initial['result'] == expected[0] and replay['result'] == expected[0]
        assert event['result'] == expected[1] and len(event['published_events']) == 1
        assert not denied['ok'] and denied['error']['code'] == 'authorization_failed'
        assert denied['error']['stage'] == 'security' and denied['error']['retryable']
        assert denied['snapshot'] == initial['snapshot']
        assert denied['published_events'] == initial['published_events']
        if fault == 'expire_before':
            assert denied['trace'] == [] and denied['calls'] == initial['calls']
        else:
            assert {'detector', 'classifier_a', 'classifier_b'} <= {
                row['alias'] for row in denied['trace'] if row['service'] == 'model'}
        assert closed['lifecycle']['closed'] and reopened['ok'] and shutdown['lifecycle']['closed']
    for a, b in zip(left, right):
        for key in ('ok', 'snapshot', 'published_events', 'calls'):
            assert a[key] == b[key]
        assert a.get('result') == b.get('result')
        for key in ('closed', 'failed', 'scenario_close_called'):
            if a['lifecycle'] is not None:
                assert a['lifecycle'][key] == b['lifecycle'][key]


@pytest.mark.parametrize('worker', ['reference', 'independent'])
def test_wrong_model_binding_rejected_before_import(worker):
    rejected, _ = invoke(worker, [{'op': 'open', 'policy': 'offline',
        'bindings': {'wrong_digest': 'detector'}}, {'op': 'shutdown'}])
    assert not rejected['ok'] and rejected['error']['code'] == 'model_link_mismatch'
    assert rejected['scenario_imports'] == 0 and not rejected['trace']
