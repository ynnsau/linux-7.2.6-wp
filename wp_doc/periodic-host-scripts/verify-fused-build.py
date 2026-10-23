#!/usr/bin/env python3
"""Verify batched build artifacts against completed VM evidence; never installs anything."""
import argparse, hashlib, json, pathlib, re, shlex, subprocess, sys
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('run',type=pathlib.Path)
p.add_argument('source',type=pathlib.Path)
def build_job(value):
    if not re.fullmatch(r'production-build-[A-Za-z0-9][A-Za-z0-9_-]*',value):
        raise argparse.ArgumentTypeError('build job must be a safe production-build-* basename')
    return value
p.add_argument('--build-job',required=True,type=build_job,
               help='completed production build job to verify, for example production-build-v3')
a=p.parse_args(); run=a.run.resolve(); src=a.source.resolve(); out=run/'build-production'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()
errors=[]
def require(ok,msg):
    if not ok: errors.append(msg)
release=(out/'include/config/kernel.release').read_text().strip()
require(release=='7.2.6-cmdp-fused-production','Unexpected production release')
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
# The build is incremental from a preserved previous output tree. Verify every
# module's release, not only the count or a few installed driver samples.
vermagic_prefix = ('vermagic=' + release + ' ').encode()
bad_vermagic = [name for name in modules if (out / name).is_file() and
                vermagic_prefix not in (out / name).read_bytes()]
require(not bad_vermagic, 'Stale or mismatched module release')

image_hash=artifacts.get('arch/x86/boot/bzImage',{}).get('sha256')
SUITES = ('fused', 'batch', 'dirty', 'small', 'scale', 'numa', 'recovery', 'recovery-numa')
KERNEL_INPUTS = ('mm/cmdp.c', 'mm/mprotect.c', 'mm/mremap.c',
                 'include/linux/cmdp.h', 'include/trace/events/cmdp.h',
                 'include/uapi/linux/cmdp.h')

def json_record(path, label):
    try:
        value = json.loads(path.read_text())
        require(isinstance(value, dict), 'Non-object ' + label)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError) as exc:
        require(False, 'Cannot read ' + label + ': ' + str(exc))
        return {}

def latest_vm(prefix, suite):
    # Numeric versions avoid incorrectly selecting v9 over v10. Keep failed
    # latest runs visible: never silently select an older passing run.
    pattern = re.compile(re.escape(prefix + '-' + suite) + r'-v([0-9]+)')
    candidates = []
    for directory in run.glob(prefix + '-' + suite + '-v*'):
        match = pattern.fullmatch(directory.name)
        if directory.is_dir() and match:
            candidates.append((int(match.group(1)), directory / 'validation.json'))
    require(bool(candidates), 'Missing ' + prefix + ' VM suite ' + suite)
    return max(candidates)[1] if candidates else None

def verify_vm(prefix, suite, kernel, expected_image_hash):
    path = latest_vm(prefix, suite)
    if path is None:
        return {}
    if not path.is_file():
        require(False, 'Newest VM attempt lacks validation: ' + str(path.parent))
        return {'path': str(path), 'pass': False}
    report = json_record(path, prefix + ' ' + suite + ' validation')
    vm = path.parent
    label = prefix + ' ' + suite
    require(report.get('pass') is True and not report.get('errors'), 'Failing VM suite ' + label)
    require(report.get('suite') == suite, 'Wrong suite in validation: ' + label)
    taps = report.get('tap_suites', [])
    require(bool(taps) and all(t.get('pass') is True and t.get('planned', 0) > 0 and
                              t.get('planned') == t.get('observed') for t in taps),
            'Incomplete TAP suites: ' + label)
    evidence = {'path': str(path), 'pass': report.get('pass'),
                'validation_sha256': sha(path), 'tap_checks': [t.get('planned') for t in taps]}
    try:
        serial = vm / 'serial.log'
        serial_hash = sha(serial)
        require(serial_hash == report.get('serial_sha256'), 'Serial log differs from validation: ' + label)
        require((vm / 'qemu.exit').read_text().strip() == '0', 'QEMU exit is not zero: ' + label)
        for guard in ('host-boot-id', 'host-uname', 'host-cmdp'):
            require((vm / (guard + '.before')).read_bytes() == (vm / (guard + '.after')).read_bytes(),
                    'Host guard changed: ' + label + ' ' + guard)
        inputs = {}
        for line in (vm / 'inputs.sha256').read_text().splitlines():
            digest, filename = line.split(None, 1)
            filename = filename.lstrip('* ')
            require(bool(re.fullmatch('[0-9a-f]{64}', digest)), 'Invalid input hash: ' + label)
            require(filename not in inputs, 'Duplicate VM input: ' + label + ' ' + filename)
            inputs[filename] = digest
            target = pathlib.Path(filename)
            require(target.is_file() and sha(target) == digest, 'Changed VM input: ' + label + ' ' + filename)
        verified_hash = inputs.get(str(kernel))
        require(bool(expected_image_hash) and verified_hash == expected_image_hash,
                'Image differs from VM-tested image: ' + label)
        command = shlex.split((vm / 'qemu-command.txt').read_text())
        require('-kernel' in command and command[command.index('-kernel') + 1] == str(kernel),
                'VM command does not use expected image: ' + label)
        append = command[command.index('-append') + 1] if '-append' in command else ''
        require('cmdp_suite=' + suite in append.split(), 'VM command suite mismatch: ' + label)
        require('-initrd' in command and command[command.index('-initrd') + 1] in inputs,
                'VM initramfs is not bound to an input hash: ' + label)
        evidence.update({'kernel_sha256': verified_hash, 'serial_sha256': serial_hash,
                         'inputs_sha256': sha(vm / 'inputs.sha256')})
        if suite == 'recovery-baseline':
            lines = serial.read_text(errors='replace').splitlines()
            configs = [dict(re.findall(r'(\w+)=([^ ]+)', x)) for x in lines
                       if x.startswith('CMDP_RECOVERY_CONFIG ')]
            require(len(taps) == 2 and len(configs) == 2 and
                    sorted(c.get('threads') for c in configs) == ['1', '4'] and
                    all(c.get('expected') == 'stranded' for c in configs),
                    'Baseline did not run explicit one/four-thread expected-stranding cases')
            require(sum(x.strip() == 'CMDP_RECOVERY_EXPECTED_FAILURE_REPRODUCED stranded=yes'
                        for x in lines) == 2, 'Baseline lacks actual expected-stranding evidence')
            require(not any('CMDP_RECOVERY_FIXED_VERIFIED' in x for x in lines),
                    'Baseline incorrectly contains fixed-behavior success marker')
    except (OSError, ValueError, IndexError) as exc:
        require(False, 'Cannot verify raw VM evidence ' + label + ': ' + str(exc))
    return evidence

vm_runs = {suite: verify_vm('production', suite, out / 'arch/x86/boot/bzImage', image_hash)
           for suite in SUITES}
debug_out = run / 'build-debug'
debug_image = debug_out / 'arch/x86/boot/bzImage'
require(debug_image.is_file(), 'Missing final debug image')
debug_hash = sha(debug_image) if debug_image.is_file() else None
debug_config = (debug_out / '.config').read_text()
require((debug_out / 'include/config/kernel.release').read_text().strip() == '7.2.6-cmdp-fused-debug',
        'Unexpected debug release')
for flag in ('CMDP', 'CMDP_TEST', 'PROVE_LOCKING', 'DEBUG_ATOMIC_SLEEP', 'DEBUG_VM'):
    require('CONFIG_' + flag + '=y\n' in debug_config, 'Debug image missing ' + flag)
debug_runs = {suite: verify_vm('debug', suite, debug_image, debug_hash) for suite in SUITES}
# A later harness/documentation commit is allowed. Bind the six changed kernel
# inputs to their pre-build freeze and the git objects at that freeze's commit,
# rather than requiring HEAD to remain the build-start commit.
source_hashes = {name: sha(src / name) for name in KERNEL_INPUTS}
source_freezes = {}
for label in ('fixed',):
    path = run / 'records' / (label + '-kernel-source.json')
    frozen = json_record(path, label + ' kernel source freeze')
    commit_at_freeze = frozen.get('commit', '')
    require(bool(re.fullmatch('[0-9a-f]{40}', commit_at_freeze)), 'Invalid ' + label + ' freeze commit')
    hashes = frozen.get('source_sha256', {})
    require(set(KERNEL_INPUTS) <= set(hashes), 'Incomplete ' + label + ' kernel source freeze')
    for name in KERNEL_INPUTS:
        if label == 'fixed':
            require(hashes.get(name) == source_hashes[name], 'Kernel source changed after fixed freeze: ' + name)
        if re.fullmatch('[0-9a-f]{40}', commit_at_freeze):
            try:
                blob = subprocess.check_output(['git', '-C', str(src), 'show', commit_at_freeze + ':' + name])
                require(hashlib.sha256(blob).hexdigest() == hashes.get(name),
                        label + ' frozen hash differs from committed kernel input: ' + name)
            except subprocess.CalledProcessError:
                require(False, 'Cannot read ' + label + ' committed kernel input: ' + name)
    source_freezes[label] = {'path': str(path), 'sha256': sha(path) if path.is_file() else None,
                             'commit': commit_at_freeze, 'source_sha256': hashes}
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
commit=subprocess.check_output(['git','-C',str(src),'rev-parse','HEAD'],universal_newlines=True).strip()
status=subprocess.check_output(['git','-C',str(src),'status','--porcelain'],universal_newlines=True)
require(not status,'Source tree is not clean')
r={'pass':not errors,'errors':errors,'release':release,'commit':commit,'source_path':str(src),'build_path':str(out),'artifacts':artifacts,'module_count':len(modules),'missing_modules':missing,'bad_module_vermagic':bad_vermagic,'module_vermagic_checked':len(modules),'build_job':a.build_job,'build_log':str(selected_log),'build_exit_status':exit_status,'compiler_diagnostics':warnings,'historical_build_diagnostics':historical,'source_sha256':source_hashes,'source_freezes':source_freezes,'production_vm_suites':vm_runs,'debug_vm_suites':debug_runs,'debug_artifacts':{'arch/x86/boot/bzImage':{'sha256':debug_hash},'.config':{'sha256':sha(debug_out/'.config')}}}
(run/'records'/'build-verification.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'pass':r['pass'],'release':release,'commit':commit,'modules':len(modules),'build_job':a.build_job,'diagnostics':len(warnings),'historical_jobs':len(historical),'errors':errors}))
sys.exit(0 if r['pass'] else 1)
