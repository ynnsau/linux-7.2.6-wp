#!/usr/bin/env python3
import hashlib,json,pathlib,tarfile,datetime,subprocess,sys
root=pathlib.Path(sys.argv[1])
base=root.parent
summary=json.loads((root/'verify-summary.json').read_text())
assert summary['status'] in ('complete','failed'), 'Do not archive mutable active evidence'
manifest={'at':datetime.datetime.utcnow().isoformat()+'Z','root':str(root),'verification_status':summary['status'],'files':{},'excluded_raw':{}}
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
 return h.hexdigest()
paths=[]
for p in sorted(root.rglob('*')):
 if not p.is_file() or p.is_symlink() or p.name.endswith('.lock') or '__pycache__' in p.parts:continue
 name=str(p.relative_to(root))
 if p.name=='latency.raw':
  receipt=json.loads((p.parent/'capture-receipt.json').read_text())
  manifest['excluded_raw'][name]={'bytes':p.stat().st_size,'sha256':receipt['files_sha256']['latency.raw'],'scope':'raw remains on BMP; receipt hash verified before analysis'}
 else:
  manifest['files'][name]={'bytes':p.stat().st_size,'sha256':sha(p)}; paths.append((p,name))
log=base/(root.name+'-launch.log')
manifest['files']['launch.log']={'bytes':log.stat().st_size,'sha256':sha(log)}
paths.append((log,'launch.log'))
out=base/(root.name+'-evidence-20261101.tar.gz')
assert not out.exists(), 'Preserve prior archive'
with tarfile.open(str(out),'w:gz') as archive:
 for p,name in paths:archive.add(str(p),arcname=name,recursive=False)
mp=base/(root.name+'-evidence-manifest-20261101.json')
manifest['archive']={'path':str(out),'bytes':out.stat().st_size,'sha256':sha(out)}
mp.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
print(json.dumps({'files':len(paths),'archive_bytes':out.stat().st_size,'status':summary['status']}))
