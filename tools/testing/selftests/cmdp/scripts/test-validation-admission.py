#!/usr/bin/env python3
"""Check that optional admission gates never relax accounting or exit checks.

Inputs are a recorded clean zero-admission run and a recorded nonzero run.
Evidence is read-only; corrupted copies exist only in a temporary directory.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('zero_run',type=Path)
p.add_argument('nonzero_run',type=Path)
a=p.parse_args()
spec=importlib.util.spec_from_file_location('validation',Path(__file__).with_name('validate-run.py'))
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
assert v.validate(a.zero_run)['errors']==['Qualification run admitted no pages; admission test is vacuous']
assert v.validate(a.zero_run,require_admissions=False)['pass']
assert v.validate(a.nonzero_run)['pass']
with tempfile.TemporaryDirectory(prefix='cmdp-validator-contract-') as temp:
 root=Path(temp)
 for kind in ('conservation','exit'):
  path=root/kind;shutil.copytree(str(a.zero_run),str(path))
  records=[json.loads(s) for s in (path/'monitor.jsonl').read_text().splitlines() if s.strip()]
  for r in records:
   if kind=='conservation' and r.get('event')=='final':r['terminals']['write']+=1
   if kind=='exit' and r.get('event')=='controller_exit':r['exit_code']=1
  (path/'monitor.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
  result=v.validate(path,require_admissions=False)
  assert not result['pass']
  expected='Session generation counts do not balance' if kind=='conservation' else 'Controller did not exit successfully'
  assert expected in result['errors'],result['errors']
print(json.dumps({'pass':True,'checks':5,'zero_run':str(a.zero_run),'nonzero_run':str(a.nonzero_run)}))
