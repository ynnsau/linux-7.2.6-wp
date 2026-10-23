#!/usr/bin/env python3
"""Audit the installed fused kernel and compile a fresh native tool bundle.

Reuses the recovery preparation checks with explicit fused-kernel identity.
Does not run CMD-P tests or change host configuration.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
RELEASE = '7.2.6-cmdp-fused-production'
BUILD_RECORD = Path('/home/yans3/cmdp-v2/periodic/fused-scanner-20261022/records/build-verification.json')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--expected-source-commit', required=True)
    a = p.parse_args()
    spec = importlib.util.spec_from_file_location('prepare_fused_helpers', str(SCRIPTS / 'prepare-recovery-native.py'))
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    record = json.loads(BUILD_RECORD.read_text())
    assert record['pass'] and record['release'] == RELEASE
    kernel_commit = record['source_freezes']['fixed']['commit']
    image_sha256 = record['artifacts']['arch/x86/boot/bzImage']['sha256']
    import re
    ids = re.findall(r'Build ID:\s*([0-9a-f]+)', subprocess.check_output(
        ['readelf', '-n', str(Path(record['build_path']) / 'vmlinux')]).decode())
    assert len(ids) == 1
    build_id = ids[0]
    # Every original image/build-ID/receipt/source check still executes.
    prepare.RELEASE = RELEASE
    sys.argv = [str(SCRIPTS / 'prepare-recovery-native.py'), '--base', str(a.base),
                '--source', str(a.source), '--expected-source-commit', a.expected_source_commit,
                '--installation-record', '/home/yans3/cmdp-v2/periodic/native-install-fused-scanner-20261022/installation-verification.json',
                '--build-record', '/home/yans3/cmdp-v2/periodic/fused-scanner-20261022/records/build-verification.json',
                '--kernel-commit', kernel_commit, '--kernel-sha256', image_sha256,
                '--kernel-build-id', build_id]
    prepare.main()
    tools = a.base / 'tools'
    build = json.loads((tools / 'tools-build.json').read_text())
    source = a.source / 'tools/testing/selftests/cmdp/cmdp_batch_test.c'
    binary = tools / 'bin/cmdp_batch_test'
    argv = ['gcc-13', '-O2', '-Wall', '-Wextra', '-Werror', '-std=gnu11', '-pthread',
            '-I' + str(tools / 'headers'), str(source), '-o', str(binary)]
    with (tools / 'build.log').open('a') as log:
        subprocess.run(argv, stdout=log, stderr=log, check=True)
    build['builds'].append(dict(name='cmdp_batch_test', argv=argv, exit=0,
                               source_sha256=prepare.sha(source), binary_sha256=prepare.sha(binary)))
    (tools / 'tools-build.json').write_text(json.dumps(build, indent=2) + '\n')
    monitor = next(x for x in build['builds'] if x['name'] == 'cmdp_monitor')
    shutil.copyfile(str(tools / 'bin/cmdp_monitor'), str(tools / 'cmdp_monitor'))
    (tools / 'cmdp_monitor').chmod(0o755)
    (tools / 'build-monitor.json').write_text(json.dumps(dict(
        argv=monitor['argv'], source_sha256=monitor['source_sha256'],
        uapi_sha256=build['uapi_sha256'], binary_sha256=monitor['binary_sha256'],
        compiler_version=subprocess.check_output(['gcc-13', '--version']).decode(), exit_code=0), indent=2) + '\n')
    audit = json.loads((a.base / 'postboot-audit.json').read_text())
    audit.update(fused_ready=True, tools_build_sha256=prepare.sha(tools / 'tools-build.json'),
                 prepare_source_sha256=prepare.sha(Path(__file__)))
    (a.base / 'postboot-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    print(json.dumps({'pass': True, 'kernel': RELEASE, 'tools': len(build['builds']), 'tests_run': False}))


if __name__ == '__main__':
    main()
