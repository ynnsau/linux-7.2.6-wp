#!/usr/bin/env python3
"""Bind successful VM checks to the exact committed production build."""
import hashlib
import json
import pathlib
import subprocess

base = pathlib.Path('/home/yans3/cmdp-v2')
src = base / 'linux'
records = base / 'periodic/records'

def git(*args):
    return subprocess.check_output(['git', '-C', str(src)] + list(args), universal_newlines=True).strip()

def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

assert not git('status', '--porcelain=v1'), 'Source tree must be clean'
commit = git('rev-parse', 'HEAD')
assert commit == (records / 'source-head-at-build').read_text().strip()
subprocess.check_call(['sha256sum', '-c', str(records / 'build-artifacts.sha256')])
for label in ('debug-final', 'production-final'):
    subprocess.check_call(['sha256sum', '-c', str(records / ('vm-' + label + '-inputs.sha256'))])
    result = json.loads((base / 'periodic/reports' / label / 'vm-validation.json').read_text())
    assert result['pass'], label + ' validation failed'

paths = [base / 'build/arch/x86/boot/bzImage', base / 'build/.config',
         base / 'build/System.map', base / 'periodic/vm/initramfs.cpio.gz',
         base / 'periodic/vm/test-binaries.sha256']
for label in ('debug-final', 'production-final'):
    paths += [base / 'periodic/logs' / ('vm-' + label + '.log'),
              records / ('vm-' + label + '-inputs.sha256'),
              base / 'periodic/reports' / label / 'vm-validation.json']
result = {'schema': 1, 'pass': True, 'source_commit': commit, 'kernel_release': (records / 'kernel-release.txt').read_text().strip(),
          'artifacts': {str(p): sha(p) for p in paths}}
(records / 'runtime-validation.json').write_text(json.dumps(result, indent=2) + '\n')
print('Recorded exact-build runtime validation for ' + commit)
