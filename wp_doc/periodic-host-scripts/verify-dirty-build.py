#!/usr/bin/env python3
"""Verify build artifacts against completed VM evidence; never installs anything."""
import argparse, hashlib, json, pathlib, re, subprocess, sys
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('run',type=pathlib.Path)
p.add_argument('source',type=pathlib.Path)
a=p.parse_args(); run=a.run; src=a.source; out=run/'build-production'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()
errors=[]
def require(ok,msg):
    if not ok: errors.append(msg)
release=(out/'include/config/kernel.release').read_text().strip()
require(release=='7.2.6-cmdp-dirty-production','Unexpected production release')
config=(out/'.config').read_text()
for flag in ('CMDP','CMDP_TEST','DEBUG_FS','SMP','MODULES','SWAP','VIRTIO_BLK','CHECKPOINT_RESTORE','MEM_SOFT_DIRTY','PROC_PAGE_MONITOR'):
    require('CONFIG_'+flag+'=y\n' in config,'Missing built-in '+flag)
artifacts={}
for rel in ('arch/x86/boot/bzImage','vmlinux','System.map','.config','Module.symvers','modules.order','modules.builtin','certs/signing_key.x509'):
    path=out/rel
    require(path.is_file(),'Missing '+rel)
    if path.is_file(): artifacts[rel]={'bytes':path.stat().st_size,'sha256':sha(path)}
ordered=[x.strip() for x in (out/'modules.order').read_text().splitlines() if x.strip()]
modules=[x[:-2]+'.ko' if x.endswith('.o') else x for x in ordered]
missing=[x for x in modules if not (out/x).is_file()]
require(len(modules)>5000,'Production module set unexpectedly small')
require(not missing,'Some modules.order artifacts missing')
image_hash=artifacts.get('arch/x86/boot/bzImage',{}).get('sha256')
vm_runs={}
for suite in ('dirty','small','scale','numa'):
    matches=sorted(run.glob('production-'+suite+'-v*/validation.json'))
    require(bool(matches),'Missing production VM suite '+suite)
    if not matches: continue
    path=matches[-1]; report=json.loads(path.read_text())
    hashes=(path.parent/'inputs.sha256').read_text().splitlines()
    verified_hash=hashes[0].split()[0]
    require(report.get('pass') is True,'Failing production VM suite '+suite)
    require(verified_hash==image_hash,'Production image differs from VM-tested image '+suite)
    vm_runs[suite]={'path':str(path),'pass':report.get('pass'),'kernel_sha256':verified_hash,'tap_checks':[t['planned'] for t in report.get('tap_suites',[])]}
logs=sorted((run/'logs').glob('production-build-*.log'))
warnings=[]
for log in logs:
    warnings += [line for line in log.read_text(errors='replace').splitlines() if re.search(r'warning:|error:',line)]
require(not any(re.search(r'\berror:',line) for line in warnings),'Compiler error in production log')
require(not any('mm/cmdp.c:' in line for line in warnings),'CMD-P compiler diagnostic')
source_hashes={str(x.relative_to(src)):sha(x) for x in [src/'mm/cmdp.c',src/'include/uapi/linux/cmdp.h']}
commit=subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD'],universal_newlines=True).strip()
status=subprocess.check_output(['git','-C',str(src),'status','--porcelain'],universal_newlines=True)
require(not status,'Source tree is not clean')
r={'pass':not errors,'errors':errors,'release':release,'commit':commit,'source_path':str(src),'build_path':str(out),'artifacts':artifacts,'module_count':len(modules),'missing_modules':missing,'compiler_diagnostics':warnings,'source_sha256':source_hashes,'production_vm_suites':vm_runs}
(run/'records'/'build-verification.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'pass':r['pass'],'release':release,'commit':commit,'modules':len(modules),'diagnostics':len(warnings),'errors':errors}))
sys.exit(0 if r['pass'] else 1)
