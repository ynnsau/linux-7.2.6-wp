#!/usr/bin/env python3
"""Audit the installed batched kernel and compile a fresh native tool bundle.

Reuses the recovery preparation checks with explicit batched-kernel identity.
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
RELEASE = '7.2.6-cmdp-batch-production'
KERNEL_COMMIT = '2919ec3ef02fe690a51a1bfded34845d9ce1eb10'
IMAGE_SHA256 = 'a120f974dd18909e73ab95a581caf36cb2294266fc618b23cc87788cb690809e'
BUILD_ID = 'e22097b63ad09515390570a4b00b246d0a46db14'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-rearm-recovery'))
    p.add_argument('--expected-source-commit', required=True)
    a = p.parse_args()
    spec = importlib.util.spec_from_file_location('prepare_batched_helpers', str(SCRIPTS / 'prepare-recovery-native.py'))
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    # Every original image/build-ID/receipt/source check still executes.
    prepare.RELEASE = RELEASE
    sys.argv = [str(SCRIPTS / 'prepare-recovery-native.py'), '--base', str(a.base),
                '--source', str(a.source), '--expected-source-commit', a.expected_source_commit,
                '--installation-record', '/home/yans3/cmdp-v2/periodic/native-install-batch-arm-20261022/installation-verification.json',
                '--build-record', '/home/yans3/cmdp-v2/periodic/batch-arm-20261022/records/build-verification.json',
                '--kernel-commit', KERNEL_COMMIT, '--kernel-sha256', IMAGE_SHA256,
                '--kernel-build-id', BUILD_ID]
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
    audit.update(batched_ready=True, tools_build_sha256=prepare.sha(tools / 'tools-build.json'),
                 prepare_source_sha256=prepare.sha(Path(__file__)))
    (a.base / 'postboot-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    print(json.dumps({'pass': True, 'kernel': RELEASE, 'tools': len(build['builds']), 'tests_run': False}))


if __name__ == '__main__':
    main()
