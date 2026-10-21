#!/usr/bin/env python3
"""Audit the booted dirty kernel and build native CMD-P userspace tools.

The output schema matches run-dirty-native.py. Use a fresh output base for a
new experiment. This script does not install a kernel, change host settings,
or launch the tests. The installed kernel implementation must remain identical
to the validated f614f90 reference; later documentation/test-tool commits can
be selected explicitly with --expected-source-commit.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

RELEASE = '7.2.6-cmdp-dirty-production'
KERNEL_COMMIT = 'f614f90e6b5482c52f14f9135ac1e04c6bef9541'
DEFAULT_SOURCE = Path('/home/yans3/cmdp-v2/linux-dirty-arm')
DEFAULT_BASE = Path('/home/yans3/cmdp-v2/periodic/dirty-native-bfs-20261021')
DEFAULT_BFS = Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/bfs')
DEFAULT_INSTALLATION_RECORD = Path(
    '/home/yans3/cmdp-v2/periodic/native-install-dirty-arm-20261021/installed-files.json')

EXAMPLE = """After a documentation/test-tool commit, prepare a fresh run on bmp:
  cmdp_src=/home/yans3/cmdp-v2/linux-dirty-arm
  cmdp_scripts="$cmdp_src/tools/testing/selftests/cmdp/scripts"
  cmdp_run=/home/yans3/cmdp-v2/periodic/dirty-native-bfs-NEW-RUN
  cmdp_head=$(git -C "$cmdp_src" rev-parse HEAD)
  sudo python3 "$cmdp_scripts/prepare-dirty-native.py" \\
    --base "$cmdp_run" --expected-source-commit "$cmdp_head" || exit 1
  sudo python3 "$cmdp_scripts/run-dirty-native.py" small \\
    --base "$cmdp_run" --expected-source-commit "$cmdp_head" || exit 1
  for cmdp_threads in 1 4; do
    for cmdp_phase in twitter-verify twitter-sizing twitter-observe; do
      sudo python3 "$cmdp_scripts/run-dirty-native.py" "$cmdp_phase" \\
        --base "$cmdp_run" --threads "$cmdp_threads" \\
        --expected-source-commit "$cmdp_head" || exit 1
    done
  done

Require preparation and small checks to succeed before continuing. The default
phase settings are three verified Twitter trials, 16/36 sizing trials for one/
four threads, and an observation sized for about 130 seconds of BFS computation.
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv):
    return subprocess.check_output([str(x) for x in argv]).decode().strip()


def prepare(args):
    base, src, bfs = args.base, args.source, args.bfs
    tools = base / 'tools'
    stats = {key: int(value) for key, value in (line.split() for line in
             Path('/sys/kernel/debug/cmdp/stats').read_text().splitlines())}
    audit = {
        'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'kernel': command(['uname', '-r']),
        'cmdline': Path('/proc/cmdline').read_text().strip(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'taint': Path('/proc/sys/kernel/tainted').read_text().strip(),
        'stats': stats,
        'meminfo': Path('/proc/meminfo').read_text(),
        'swaps': Path('/proc/swaps').read_text(),
        'numa_balancing': Path('/proc/sys/kernel/numa_balancing').read_text().strip(),
        'cpu_topology': command(['lscpu']),
        'bfs_sha256': sha(bfs),
        'gapbs_commit': command(['git', '-C', bfs.parent, 'rev-parse', 'HEAD']),
        'source_commit': command(['git', '-C', src, 'rev-parse', 'HEAD']),
        'source_status': command(['git', '-C', src, 'status', '--porcelain']),
    }
    assert audit['kernel'] == RELEASE
    assert all(stats[key] == 0 for key in ('entries', 'managed', 'isolated'))
    assert audit['source_commit'] == args.expected_source_commit
    assert not audit['source_status'], 'Commit source changes before preparing native tests'
    # Compare the entire repository, allowing only documentation and CMD-P
    # selftest/tool changes after the validated kernel reference. This covers
    # kernel implementation/configuration paths beyond the four CMD-P files.
    subprocess.run(['git', '-C', str(src), 'diff', '--exit-code', KERNEL_COMMIT,
                    '--', '.', ':(exclude)wp_doc',
                    ':(exclude)tools/testing/selftests/cmdp'],
                   check=True, stdout=subprocess.DEVNULL)
    assert audit['numa_balancing'] == '1'
    available = re.search(r'^MemAvailable:\s+(\d+)', audit['meminfo'], re.M)
    assert available and int(available.group(1)) > 64 * 1024 * 1024
    assert len(audit['swaps'].splitlines()) > 1
    installed = json.loads(args.installation_record.read_text())
    audit['installed_files'] = {path: sha(Path(path)) for path in installed['installed_files']}
    assert all(audit['installed_files'][path] == value['sha256']
               for path, value in installed['installed_files'].items())

    tools.mkdir(parents=True, exist_ok=False)
    (tools / 'bin').mkdir()
    (tools / 'headers/linux').mkdir(parents=True)
    (base / 'postboot-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    (base / 'kernel-boot.log').write_bytes(subprocess.check_output(['dmesg']))
    shutil.copyfile(str(src / 'include/uapi/linux/cmdp.h'), str(tools / 'headers/linux/cmdp.h'))
    build = {'at': audit['at'], 'commit': audit['source_commit'], 'worktree_clean': True,
             'uapi_sha256': sha(src / 'include/uapi/linux/cmdp.h'), 'builds': []}
    with (tools / 'build.log').open('w') as log:
        for name in ('cmdp_test', 'cmdp_monitor_test', 'cmdp_range_test',
                     'cmdp_dirty_test', 'cmdp_monitor', 'cmdp_fixture'):
            source = src / 'tools/testing/selftests/cmdp' / (name + '.c')
            binary = tools / 'bin' / name
            argv = ['gcc-13', '-O2', '-Wall', '-Wextra', '-Werror', '-std=gnu11',
                    '-pthread', '-I' + str(tools / 'headers'), str(source), '-o', str(binary)]
            log.write(' '.join(argv) + '\n')
            log.flush()
            process = subprocess.run(argv, stdout=log, stderr=log)
            item = {'name': name, 'argv': argv, 'exit': process.returncode, 'source_sha256': sha(source)}
            if process.returncode == 0:
                item['binary_sha256'] = sha(binary)
            build['builds'].append(item)
            (tools / 'tools-build.json').write_text(json.dumps(build, indent=2) + '\n')
            assert process.returncode == 0, name + ' failed to build'
        process = subprocess.run([str(tools / 'bin/cmdp_monitor'), '--help'], stdout=log, stderr=log)
        assert process.returncode == 0
    (tools / 'build.exit').write_text('0\n')
    print(json.dumps({'pass': True, 'kernel': audit['kernel'],
                      'boot_id': audit['boot_id'], 'tools': len(build['builds'])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', type=Path, default=DEFAULT_BASE)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--bfs', type=Path, default=DEFAULT_BFS)
    parser.add_argument('--installation-record', type=Path, default=DEFAULT_INSTALLATION_RECORD)
    parser.add_argument('--expected-source-commit', default=KERNEL_COMMIT)
    prepare(parser.parse_args())


if __name__ == '__main__':
    main()
