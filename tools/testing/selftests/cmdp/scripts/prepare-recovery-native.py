#!/usr/bin/env python3
"""Prepare fresh native recovery evidence after the user boots the candidate.

No host configuration changes, tests, kernel installation or reboot. Require the
explicit validated implementation commit, installed image hash and running GNU
build ID; no receipt from the previous dirty-kernel boot is reusable.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import struct
import subprocess
import tempfile

RELEASE = '7.2.6-cmdp-rearm-production'
# Original study graph checksum recorded in the 2026-10-20 Twitter setup.
TWITTER_SHA256 = '4b7e5193a8e3384b8319359dc8c92534b8aee8e48d6c3bd0820451bb307d6eea'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def running_build_id(path=Path('/sys/kernel/notes')):
    raw = path.read_bytes()
    at, found = 0, set()
    while at + 12 <= len(raw):
        namesz, descsz, kind = struct.unpack_from('<III', raw, at)
        at += 12
        if not namesz and not descsz:
            break
        if at + ((namesz + 3) & ~3) + ((descsz + 3) & ~3) > len(raw):
            raise ValueError('Truncated running kernel ELF note')
        name = raw[at:at + namesz].rstrip(b'\0')
        at += (namesz + 3) & ~3
        desc = raw[at:at + descsz]
        at += (descsz + 3) & ~3
        if name == b'GNU' and kind == 3:
            found.add(desc.hex())
    if len(found) != 1:
        raise ValueError('Missing or ambiguous running GNU build ID')
    return found.pop()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-rearm-recovery'))
    p.add_argument('--bfs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/bfs'))
    p.add_argument('--pr', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/pr'))
    p.add_argument('--graph', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/benchmark/graphs/twitter.sg'))
    p.add_argument('--installation-record', type=Path, required=True)
    p.add_argument('--build-record', type=Path, required=True)
    p.add_argument('--kernel-commit', required=True)
    p.add_argument('--kernel-sha256', required=True)
    p.add_argument('--kernel-build-id', required=True)
    p.add_argument('--expected-source-commit', required=True)
    a = p.parse_args()
    assert os.geteuid() == 0 and subprocess.check_output(['uname', '-r']).decode().strip() == RELEASE
    assert running_build_id() == a.kernel_build_id.lower(), 'Wrong running kernel build ID'
    image = Path('/boot/vmlinuz-' + RELEASE)
    assert sha(image) == a.kernel_sha256.lower(), 'Installed kernel image differs from validated image'
    installed = json.loads(a.installation_record.read_text())
    build_record = json.loads(a.build_record.read_text())
    assert installed['pass'] and installed['release'] == RELEASE and all(installed['checks'].values())
    assert installed['build_record_sha256'] == sha(a.build_record)
    assert build_record['pass'] and build_record['release'] == RELEASE
    assert installed['commit'] == build_record['commit']
    assert build_record['source_freezes']['fixed']['commit'] == a.kernel_commit
    assert build_record['artifacts']['arch/x86/boot/bzImage']['sha256'] == a.kernel_sha256.lower()
    vmlinux = Path(build_record['build_path']) / 'vmlinux'
    assert sha(vmlinux) == build_record['artifacts']['vmlinux']['sha256']
    ids = set(re.findall(r'Build ID:\s*([0-9a-f]+)', subprocess.check_output(['readelf', '-n', str(vmlinux)]).decode()))
    assert ids == {a.kernel_build_id.lower()}, 'Running build ID must match the validated vmlinux'
    after_path = a.installation_record.parent / 'host-after.json'
    after = json.loads(after_path.read_text())
    installed_files = {str(Path('/boot/' + prefix + '-' + RELEASE)):
                       after['boot_files']['/boot/' + prefix + '-' + RELEASE]
                       for prefix in ('vmlinuz', 'System.map', 'config', 'initrd.img')}
    assert all(sha(Path(path)) == metadata['sha256'] for path, metadata in installed_files.items())
    assert a.bfs.parent == a.pr.parent
    with a.graph.open('rb') as f:
        head = f.read(17)
    assert len(head) == 17 and head[0] == 1
    assert int.from_bytes(head[1:9], 'little') == 1468364884
    assert int.from_bytes(head[9:17], 'little') == 61578415
    assert a.graph.stat().st_size == 12732173745
    graph_sha256 = sha(a.graph)
    assert graph_sha256 == TWITTER_SHA256, 'Graph differs from the original study Twitter graph'
    assert not a.base.exists(), 'Use a fresh experiment base'
    script = a.source / 'tools/testing/selftests/cmdp/scripts/prepare-dirty-native.py'
    spec = importlib.util.spec_from_file_location('prepare_previous', str(script))
    previous = importlib.util.module_from_spec(spec); spec.loader.exec_module(previous)
    previous.RELEASE, previous.KERNEL_COMMIT = RELEASE, a.kernel_commit
    # Adapt only the old helper's boot-file input shape. The real recovery
    # installation/build receipts above remain the authoritative provenance.
    with tempfile.TemporaryDirectory(prefix='cmdp-recovery-prepare-') as temporary:
        normalized = Path(temporary) / 'installed-files.json'
        normalized.write_text(json.dumps({'installed_files': installed_files}))
        helper_args = argparse.Namespace(**vars(a))
        helper_args.installation_record = normalized
        with contextlib.redirect_stdout(io.StringIO()):
            previous.prepare(helper_args)
    tools = a.base / 'tools'
    build = json.loads((tools / 'tools-build.json').read_text())
    source = a.source / 'tools/testing/selftests/cmdp/cmdp_recovery_test.c'
    binary = tools / 'bin/cmdp_recovery_test'
    command = ['gcc-13', '-O2', '-Wall', '-Wextra', '-Werror', '-std=gnu11', '-pthread',
               '-I' + str(tools / 'headers'), str(source), '-o', str(binary)]
    with (tools / 'build.log').open('a') as log:
        subprocess.run(command, stdout=log, stderr=log, check=True)
    build['builds'].append({'name': 'cmdp_recovery_test', 'argv': command, 'exit': 0,
                            'source_sha256': sha(source), 'binary_sha256': sha(binary)})
    (tools / 'tools-build.json').write_text(json.dumps(build, indent=2) + '\n')
    audit = json.loads((a.base / 'postboot-audit.json').read_text())
    audit.update({'recovery_ready': True, 'kernel_commit': a.kernel_commit,
                  'kernel_image_sha256': sha(image), 'kernel_build_id': running_build_id(),
                  'bfs_path': str(a.bfs), 'pr_path': str(a.pr),
                  'pr_sha256': sha(a.pr), 'graph': str(a.graph), 'graph_sha256': graph_sha256,
                  'graph_bytes': a.graph.stat().st_size,
                  'tools_build_sha256': sha(tools / 'tools-build.json'),
                  'installation_record_sha256': sha(a.installation_record),
                  'build_record_sha256': sha(a.build_record),
                  'installation_host_after_sha256': sha(after_path)})
    (a.base / 'postboot-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    print(json.dumps({'pass': True, 'kernel': RELEASE, 'boot_id': audit['boot_id'],
                      'kernel_build_id': audit['kernel_build_id'], 'tools': len(build['builds']),
                      'native_tests_run': False}))


if __name__ == '__main__':
    main()
