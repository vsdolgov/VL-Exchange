"""Actual public VLMP inference into protected VLSP, with fresh temporary keys."""
from pathlib import Path
from contextlib import contextmanager
from functools import partial
import hashlib,json,tempfile,threading,time
import numpy as np
from nacl.public import PrivateKey
from nacl.signing import SigningKey
from public_model import ModelExecutor
from vl_scenario_package import ScenarioExecutor
from vl_exchange_security import OnlineAuthorizer,authorized_package,seal_package
from vl_exchange_security.reference_server import ReferenceLicenseService,make_server
ROOT=Path(__file__).resolve().parents[1]
def main():
 issuer=SigningKey.generate();recipient=PrivateKey.generate();installation=SigningKey.generate();server_key=SigningKey.generate()
 archive=ROOT/'scenario/reports/vlsp-target-scenario.zip'; model=ROOT/'model/examples/channel-classifier'
 archive_hash=hashlib.sha256(archive.read_bytes()).hexdigest();now=int(time.time())
 service=ReferenceLicenseService(issuer_verify_key=issuer.verify_key,server_signing_key=server_key,grant_ttl=30)
 server=make_server(service);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();modes={}
 try:
  executor=ModelExecutor(model)
  try:classification=executor.predict(np.load(model/'golden/red.npy',allow_pickle=False))
  finally:executor.close()
  with tempfile.TemporaryDirectory(prefix='vlexchange-pipeline-') as temporary:
   for mode in ('offline','online'):
    sealed=Path(temporary)/mode
    seal_package(archive,sealed,issuer_signing_key=issuer,recipient_public_key=recipient.public_key,installation_verify_key=installation.verify_key,issuer_id='synthetic-demo-publisher',license_id='synthetic-'+mode,installation_id='synthetic-local-installation',mode=mode,issued_at=now,not_before=now,expires_at=now+300,manifest_path='scenario-package.json')
    authorizer=OnlineAuthorizer(f'http://127.0.0.1:{server.server_port}/v1/grant',server_key.verify_key,installation,allow_loopback_http=True) if mode=='online' else None
    verifier=partial(authorized_package,issuer_verify_key=issuer.verify_key,recipient_private_key=recipient,policy=mode,online_authorizer=authorizer)
    with ScenarioExecutor.open_protected(sealed,verifier=verifier) as host:
     first=host.process({'classification':classification},{'source_id':'synthetic-camera','run_id':'demo-'+mode,'sequence':1,'event_time_ms':1700000000000})
     second=host.process({'classification':classification},{'source_id':'synthetic-camera','run_id':'demo-'+mode,'sequence':2,'event_time_ms':1700000000100})
    modes[mode]={'pass':not first['events'] and len(second['events'])==1,'first':first,'second':second}
 finally:server.shutdown();server.server_close();thread.join(timeout=2)
 result={'status':'pass' if all(x['pass'] for x in modes.values()) else 'fail','model_manifest_sha256':hashlib.sha256((model/'model-package.json').read_bytes()).hexdigest(),'scenario_zip_sha256':archive_hash,'scenario_archive_unchanged':archive_hash==hashlib.sha256(archive.read_bytes()).hexdigest(),'actual_inference':True,'classification':classification,'modes':modes,'keys':'generated-in-memory-only','limitations':['Synthetic classifier, not detector-quality evidence.','Explicit Classification mapping; no standardized model callback.','Local loopback license server; no third-party platform or production deployment.']}
 print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
