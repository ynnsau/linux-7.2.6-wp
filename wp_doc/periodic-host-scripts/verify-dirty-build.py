#!/usr/bin/env python3
"""Verify build artifacts against completed VM evidence; never installs anything."""
import argparse, hashlib, json, pathlib, re, subprocess, sys
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('run',type=pathlib.Path)
p.add_argument('source',type=pathlib.Path)
def build_job(value):
    if not re.fullmatch(r'production-build-[A-Za-z0-9][A-Za-z0-9_-]*',value):
        raise argparse.ArgumentTypeError('build job must be a safe production-build-* basename')
    return value
p.add_argument('--build-job',required=True,type=build_job,
               help='completed production build job to verify, for example production-build-v3')
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
selected_log=run/'logs'/(a.build_job+'.log')
selected_exit=run/'logs'/(a.build_job+'.exit')
require(selected_log.is_file(),'Missing selected production build log: '+a.build_job)
require(selected_exit.is_file(),'Missing selected production build exit status: '+a.build_job)
exit_status=selected_exit.read_text().strip() if selected_exit.is_file() else None
require(exit_status=='0','Selected production build did not exit successfully: '+a.build_job)
# Previous failed attempts remain evidence, but only the explicitly selected
# completed build determines the final compiler-diagnostic gate.
diagnostic_re=re.compile(r'\b(?:warning|error):|\bfatal(?:\s+error)?:|(?:^|[/\s])g?make(?:\[\d+\])?:\s+\*\*\*',re.I)
error_re=re.compile(r'\berror:|\bfatal(?:\s+error)?:',re.I)
make_error_re=re.compile(r'(?:^|[/\s])g?make(?:\[\d+\])?:\s+\*\*\*',re.I)
def diagnostics(log):
    return [line for line in log.read_text(errors='replace').splitlines()
            if diagnostic_re.search(line)]
warnings=diagnostics(selected_log) if selected_log.is_file() else []
require(not any(error_re.search(line) for line in warnings),
        'Compiler error or fatal diagnostic in selected production build')
require(not any(make_error_re.search(line) for line in warnings),
        'Make error in selected production build')
require(not any(re.search(r'\bcmdp(?:[._/\s:]|$)',line,re.I) and
                re.search(r'\bwarning:',line,re.I) for line in warnings),
        'CMD-P compiler warning in selected production build')
historical=[]
for log in sorted((run/'logs').glob('production-build-*.log')):
    if log==selected_log: continue
    prior_exit=log.with_suffix('.exit')
    historical.append({'job':log.stem,'log':str(log),
                       'exit_status':prior_exit.read_text().strip() if prior_exit.is_file() else None,
                       'diagnostics':diagnostics(log)})
source_hashes={str(x.relative_to(src)):sha(x) for x in [src/'mm/cmdp.c',src/'include/uapi/linux/cmdp.h']}
commit=subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD'],universal_newlines=True).strip()
status=subprocess.check_output(['git','-C',str(src),'status','--porcelain'],universal_newlines=True)
require(not status,'Source tree is not clean')
r={'pass':not errors,'errors':errors,'release':release,'commit':commit,'source_path':str(src),'build_path':str(out),'artifacts':artifacts,'module_count':len(modules),'missing_modules':missing,'build_job':a.build_job,'build_log':str(selected_log),'build_exit_status':exit_status,'compiler_diagnostics':warnings,'historical_build_diagnostics':historical,'source_sha256':source_hashes,'production_vm_suites':vm_runs}
(run/'records'/'build-verification.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'pass':r['pass'],'release':release,'commit':commit,'modules':len(modules),'build_job':a.build_job,'diagnostics':len(warnings),'historical_jobs':len(historical),'errors':errors}))
sys.exit(0 if r['pass'] else 1)
