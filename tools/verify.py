"""One-command public checks. Run from any directory with public dependencies."""
from pathlib import Path
import hashlib,json,os,platform,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
def main():
 env=dict(os.environ);env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONPATH']=os.pathsep.join(str(ROOT/p) for p in ('model/src','scenario/src','security-runtime/src'))+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
 results=[]
 (ROOT/'reports/summary.json').write_text(json.dumps({'status':'pending'}))
 commands=[('schemas',['tools/check_schemas.py'],False),('pytest',['-m','pytest','-q'],False),('vlmp-reference',['model/src/public_model.py','golden','model/examples/channel-classifier'],True),('vlmp-independent',['model/tools/independent_host.py','model/examples/channel-classifier'],True),('vlmp-archive-roundtrip',['tools/model_archive_roundtrip.py'],True),('vlsp-reference',['-m','vl_scenario_package','golden','scenario/examples/target-scenario','--policy','development'],True),('vlsp-independent',['scenario/tools/independent_host.py','scenario/reports/vlsp-target-scenario.zip','--output','reports/vlsp-independent.json'],False),('pipeline',['tools/test_pipeline.py'],True),('links',['tools/check_links.py'],False)]
 reports=ROOT/'reports';reports.mkdir(exist_ok=True)
 for name,args,is_json in commands:
  if name=='vlsp-independent':(reports/'vlsp-independent.json').unlink(missing_ok=True)
  process=subprocess.run([sys.executable,*args],cwd=ROOT,env=env,text=True,capture_output=True)
  if is_json:
   try:value=json.loads(process.stdout);(reports/f'{name}.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
   except ValueError:(reports/f'{name}-error.txt').write_text(process.stdout+process.stderr)
  else:(reports/f'{name}.txt').write_text(process.stdout+process.stderr)
  results.append({'check':name,'exit_code':process.returncode,'pass':process.returncode==0})
  print(name, 'PASS' if process.returncode==0 else 'FAIL',flush=True)
  if process.returncode:
   print((process.stdout+process.stderr)[-3000:],flush=True)
   if name=='vlsp-independent' and (reports/'vlsp-independent.json').is_file():
    print('Independent host failure report:', (reports/'vlsp-independent.json').read_text(), flush=True)
 summary={'status':'pass' if all(x['pass'] for x in results) else 'fail','runtime':{'python':platform.python_version(),'platform':platform.platform()},'checks':results,'package_archives':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'model/reports/vlmp-public-channel-probe.zip',ROOT/'scenario/reports/vlsp-target-scenario.zip')},'limitations':['Local demonstration hosts only; no competitor platform tested.','GitHub Actions workflow prepared but not run on GitHub.','No production sandbox, TLS deployment or adversarial administrator guarantee.']}
 (reports/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 return 0 if summary['status']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
