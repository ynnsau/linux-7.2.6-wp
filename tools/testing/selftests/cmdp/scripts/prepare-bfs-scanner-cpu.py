#!/usr/bin/env python3
"""Audit the reboot and stage the exact VM-tested controller; no compilation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SCRIPTS = Path(__file__).resolve().parent
RELEASE = '7.2.6-cmdp-cpu-production'
BUILD = Path('/home/yans3/cmdp-v2/periodic/scanner-cpu-20261025')
INSTALL = Path('/home/yans3/cmdp-v2/periodic/native-install-scanner-cpu-20261025')

def load(name, filename):
    s = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--expected-source-commit', required=True)
    a = p.parse_args()
    old = load('bound_native', 'run-dirty-native.py')
    prepare = load('bound_prepare', 'prepare-recovery-native.py')
    sha, save, output = prepare.sha, old.save, old.output
    assert os.geteuid() == 0 and not a.base.exists()
    record_path = BUILD / 'records/build-verification.json'
    record = json.loads(record_path.read_text())
    install_path = INSTALL / 'installation-verification.json'
    installed = json.loads(install_path.read_text())
    assert record['pass'] and installed['pass'] and all(installed['checks'].values())
    assert installed['release'] == record['release'] == RELEASE
    assert installed['build_record_sha256'] == sha(record_path)
    assert installed['commit'] == record['commit']
    vmlinux = Path(record['build_path']) / 'vmlinux'
    assert sha(vmlinux) == record['artifacts']['vmlinux']['sha256']
    ids = re.findall(r'Build ID:\s*([0-9a-f]+)', output(['readelf', '-n', vmlinux]))
    assert ids == [prepare.running_build_id()]
    after = json.loads((INSTALL / 'host-after.json').read_text())
    files = {}
    for prefix in ('vmlinuz', 'System.map', 'config', 'initrd.img'):
        path = '/boot/' + prefix + '-' + RELEASE
        files[path] = sha(Path(path))
        assert files[path] == after['boot_files'][path]['sha256']
    assert files['/boot/vmlinuz-' + RELEASE] == record['artifacts']['arch/x86/boot/bzImage']['sha256']
    state = old.snapshot()
    assert state['kernel'] == RELEASE and state['numa_balancing'] == '1'
    assert all(state['stats'][x] == 0 for x in ('entries', 'managed', 'isolated'))
    assert state['stats']['arms'] == state['stats']['publishes'] == state['stats']['revokes']
    git = lambda *args: output(['git', '-C', a.source] + list(args))
    assert git('rev-parse', 'HEAD') == a.expected_source_commit
    assert not git('status', '--porcelain')
    kernel_commit = record['source_freezes']['fixed']['commit']
    assert not git('diff', '--exit-code', kernel_commit, '--', '.',
                   ':(exclude)wp_doc', ':(exclude)tools/testing/selftests/cmdp')
    graph = a.gapbs / 'benchmark/graphs/twitter.sg'
    graph_sha = sha(graph)
    assert graph_sha == prepare.TWITTER_SHA256 and graph.stat().st_size == 12732173745
    assert not output(['git', '-C', a.gapbs, 'status', '--porcelain'])
    previous = json.loads(Path('/home/yans3/cmdp-v2/periodic/bfs-placement-20261024-v1/host-initial.json').read_text())
    for key in ('numa_balancing', 'taint', 'settings', 'governors'):
        assert state[key] == previous[key], key
    controller = json.loads((BUILD / 'records/native-controller.json').read_text())
    binary = Path(controller['path'])
    assert controller['tested'] and sha(binary) == controller['sha256']
    # Bind the native binary to the independently archived production VM inputs.
    assert sha(Path(controller['source_vm_binary'])) == controller['sha256']
    assert controller['sha256'] == 'a29a999bd5f497b7a1843046bf5d2aaa291bbadd134b096ee07ef048733e9439'
    tools = a.base / 'tools'
    (tools / 'bin').mkdir(parents=True)
    for dest in (tools / 'cmdp_monitor', tools / 'bin/cmdp_monitor'):
        shutil.copyfile(str(binary), str(dest)); dest.chmod(0o755)
    source = a.source / 'tools/testing/selftests/cmdp/cmdp_monitor.c'
    uapi = a.source / 'include/uapi/linux/cmdp.h'
    build = dict(exit_code=0, binary_sha256=sha(binary), source_sha256=sha(source),
        uapi_sha256=sha(uapi), reused_vm_tested_binary=str(binary),
        native_controller_receipt_sha256=sha(BUILD / 'records/native-controller.json'), compilation_performed=False)
    save(tools / 'build-monitor.json', build)
    save(tools / 'tools-build.json', dict(builds=[dict(build, name='cmdp_monitor')],
        fixtures_staged=False, purpose='BFS-only pilot and campaign; no native hotplug or fixture suites'))
    audit = dict(state, kernel_build_id=ids[0], kernel_commit=kernel_commit,
        source_commit=a.expected_source_commit, installed_files=files,
        graph_sha256=graph_sha, graph_bytes=graph.stat().st_size,
        gapbs_commit=output(['git', '-C', a.gapbs, 'rev-parse', 'HEAD']),
        installation_record_sha256=sha(install_path), build_record_sha256=sha(record_path))
    for app in ('bfs', 'pr', 'cc', 'bc'):
        audit[app + '_sha256'] = sha(a.gapbs / app)
    save(a.base / 'postboot-audit.json', audit)
    save(a.base / 'host-initial.json', state)
    (a.base / 'kernel-boot.log').write_text(output(['dmesg']) + '\n')
    print(json.dumps(dict(passed=True, kernel=RELEASE, build_id=ids[0], boot_id=state['boot_id'],
        controller_sha256=sha(binary), native_tests_run=False)))

if __name__ == '__main__':
    main()
