"""Compatibility and offline security checks for experimental attribution."""
import hashlib
import importlib.util
import json
import shutil
import socket
import struct
from pathlib import Path
import zlib

import pytest
from jsonschema import Draft202012Validator, ValidationError

from public_model import validate as validate_model, PackageError
from vl_scenario_package.validation import validate_package as validate_scenario
from vl_scenario_package.errors import ScenarioError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('attribution_test_validator', ROOT/'attribution/validator.py')
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
KEY = validator.KEY


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(b'\0\x30\x80\xd0')) + chunk(b'IEND',b'')


def metadata(version):
    return {'profile_version':'0.1','publisher':{'id':'lab','name':'Lab','website':'https://example.com'},
            'authors':[{'id':'model-a','name':'Model A','role':'model_author'},
                       {'id':'model-b','name':'Model B','role':'model_author'},
                       {'id':'scenario-a','name':'Scenario A','role':'scenario_author'}],
            'package_version':version,'preferred_text':'Analytics by Model A',
            'display':{'live_view':'recommended','export':'recommended','event_metadata':'required'},
            'license_url':'https://example.com/license','white_label':'prohibited'}


@pytest.fixture(params=['model','scenario'])
def package(request, tmp_path):
    is_model=request.param=='model'
    source=ROOT/('model/examples/channel-classifier' if is_model else 'scenario/examples/target-scenario')
    root=tmp_path/request.param
    shutil.copytree(source,root)
    path=root/('model-package.json' if is_model else 'scenario-package.json')
    manifest=json.loads(path.read_text())
    return root,path,manifest,(validate_model if is_model else validate_scenario)


def save(path,m):
    path.write_text(json.dumps(m),encoding='utf-8')


def test_old_packages_remain_valid(package):
    root,path,m,validate=package
    validate(root)
    assert KEY not in m['extensions']


def test_multiple_authors_and_logo_work_offline(package,monkeypatch):
    root,path,m,validate=package
    p=root/'assets'/'logo.png';p.parent.mkdir();p.write_bytes(png())
    m['files']['assets/logo.png']=hashlib.sha256(p.read_bytes()).hexdigest()
    data=metadata(m['version']);data['publisher']['logo']='assets/logo.png'
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    def deny(*a,**k): raise AssertionError('network is not allowed')
    monkeypatch.setattr(socket.socket,'connect',deny)
    validate(root)
    assert validator.validate_attribution(root,m)==data


def test_optional_logo_and_other_extension_are_supported(package,monkeypatch):
    root,path,m,validate=package
    data=metadata(m['version']);data['authors']=data['authors'][:1]
    m['extensions'][KEY]={'required':False,'data':data}
    m['extensions']['org.example.other']={'required':False,'data':{'anything':True}}
    save(path,m)
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k: pytest.fail('network used'))
    validate(root)


def test_missing_logo_is_rejected(package):
    root,path,m,validate=package
    data=metadata(m['version']);data['publisher']['logo']='assets/missing.png'
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    with pytest.raises((PackageError, ScenarioError, ValidationError)): validate(root)


@pytest.mark.parametrize('change',[
    lambda d: d['authors'].append(d['authors'][0].copy()),
    lambda d: d['publisher'].update(website='https://user:password@example.com'),
    lambda d: d['publisher'].update(website='http://example.com'),
    lambda d: d.update(package_version='0.0.0-invalid'),
    lambda d: d.pop('license_url'),
    lambda d: d['publisher'].update(logo='../escape.png'),
    lambda d: d['display'].update(live_view='always'),
])
def test_invalid_metadata_rejected(package,change):
    root,path,m,validate=package
    data=metadata(m['version']);change(data)
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    with pytest.raises((PackageError, ScenarioError, ValidationError)): validate(root)


def test_unsupported_png_and_bad_crc_rejected(package):
    root,path,m,validate=package
    p=root/'bad.png';p.write_bytes(png()[:-1] + b'X')
    m['files']['bad.png']=hashlib.sha256(p.read_bytes()).hexdigest()
    data=metadata(m['version']);data['publisher']['logo']='bad.png'
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    with pytest.raises((PackageError, ScenarioError, ValidationError)): validate(root)


def test_schemas_embed_same_profile():
    profile=json.loads((ROOT/'attribution/attribution.schema.json').read_text())
    embedded={k:v for k,v in profile.items() if k not in ('$schema','$id','title')}
    for f in ['model/spec/model-package.schema.json','scenario/spec/scenario-package.schema.json']:
        schema=json.loads((ROOT/f).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema['properties']['extensions']['properties'][KEY]['properties']['data']==embedded


def test_oversized_logo_rejected_after_digest_check(package):
    root,path,m,validate=package
    p=root/'large.png';p.write_bytes(png()+bytes(validator.MAX_PNG_BYTES))
    m['files']['large.png']=hashlib.sha256(p.read_bytes()).hexdigest()
    data=metadata(m['version']);data['publisher']['logo']='large.png'
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    with pytest.raises((PackageError, ScenarioError, ValidationError),match='logo_too_large'): validate(root)


@pytest.mark.parametrize('white_label,live_view,license_present,valid',[
    ('allowed','recommended',False,True),
    ('unspecified','disabled',False,True),
    ('prohibited','recommended',False,False),
    ('allowed','required',False,False),
    ('prohibited','required',True,True),
])
def test_white_label_and_license_policy(package,white_label,live_view,license_present,valid):
    root,path,m,validate=package
    data=metadata(m['version']);data['white_label']=white_label
    data['display']['live_view']=live_view;data['display']['event_metadata']='recommended'
    if not license_present: data.pop('license_url')
    m['extensions'][KEY]={'required':False,'data':data};save(path,m)
    if valid: validate(root)
    else:
        with pytest.raises((PackageError,ScenarioError),match='license_url_required'): validate(root)


def test_unrecognized_mandatory_extension_stays_unsupported(package):
    root,path,m,validate=package
    m['extensions']['org.example.unknown']={'required':True,'data':{}};save(path,m)
    with pytest.raises((PackageError,ScenarioError)): validate(root)


def test_attribution_extension_cannot_be_required(package):
    root,path,m,validate=package
    m['extensions'][KEY]={'required':True,'data':metadata(m['version'])};save(path,m)
    with pytest.raises((PackageError,ScenarioError,ValidationError)): validate(root)


@pytest.mark.parametrize('envelope', [
    {},
    {'required': False},
    {'data': {}},
    {'required': False, 'data': {}, 'unexpected': True},
])
def test_malformed_extension_envelope_rejected_by_base_schemas(package, envelope):
    root,path,m,validate=package
    m['extensions'][KEY]=envelope
    schema_path = ROOT / ('model/spec/model-package.schema.json' if path.name == 'model-package.json'
                          else 'scenario/spec/scenario-package.schema.json')
    schema=json.loads(schema_path.read_text())
    assert list(Draft202012Validator(schema).iter_errors(m))
    with pytest.raises(validator.AttributionError):
        validator.validate_attribution(root,m)


def test_both_vlmp_hosts_hash_files_without_whole_file_read(tmp_path, monkeypatch):
    import public_model
    spec=importlib.util.spec_from_file_location('independent_vlmp_stream_test',ROOT/'model/tools/independent_host.py')
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    contents=b'chunked hash test' * 200_000
    path=tmp_path/'large.bin';path.write_bytes(contents)
    expected=hashlib.sha256(contents).hexdigest()
    def forbid_read_bytes(_path):
        raise AssertionError('whole-file read_bytes used')
    monkeypatch.setattr(Path,'read_bytes',forbid_read_bytes)
    assert public_model.sha(path)==expected
    assert host.sha(path)==expected
