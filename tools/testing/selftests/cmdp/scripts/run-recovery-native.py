#!/usr/bin/env python3
"""Run a gated native recovery phase after user activation; never change settings.

Use a fresh prepare-recovery-native.py audit. Sequence: small; verify BFS/PR at
one/four threads; BFS sizing at both thread counts; then four observe cells.
Every observation finishes whole trials. PR uses one/four trials for one/four
threads; BFS freezes a pilot-based count targeting 240 seconds total elapsed.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess

SCRIPTS = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


previous = load('previous_native', SCRIPTS / 'run-dirty-native.py')
prepare = load('recovery_prepare', SCRIPTS / 'prepare-recovery-native.py')
previous.RELEASE = prepare.RELEASE
save, sha, output = previous.save, prepare.sha, previous.output


class Recovery(previous.Harness):
    def __init__(self, a):
        a.operation = a.phase
        if a.phase != 'small':
            a.phase = a.application + '-' + a.phase
        super().__init__(a)
        # The inherited executor, process-group cancellation and /proc sampler
        # are unchanged. Save this protocol too, not only the imported module.
        (self.out / 'recovery-harness.py').write_bytes(Path(__file__).read_bytes())

    def preflight(self):
        a = self.a
        assert os.geteuid() == 0 and self.before['kernel'] == prepare.RELEASE
        audit = json.loads((a.base / 'postboot-audit.json').read_text())
        assert audit['recovery_ready'] and audit['boot_id'] == self.before['boot_id']
        assert audit['kernel'] == prepare.RELEASE and audit['kernel_commit'] == a.kernel_commit
        assert audit['kernel_image_sha256'] == a.kernel_sha256.lower()
        assert audit['kernel_build_id'] == a.kernel_build_id.lower() == prepare.running_build_id()
        assert sha(Path('/boot/vmlinuz-' + prepare.RELEASE)) == a.kernel_sha256.lower()
        head = output(['git', '-C', a.source, 'rev-parse', 'HEAD'])
        assert head == a.expected_source_commit == audit['source_commit']
        assert not output(['git', '-C', a.source, 'status', '--porcelain'])
        subprocess.run(['git', '-C', str(a.source), 'diff', '--exit-code', a.kernel_commit,
                        '--', '.', ':(exclude)wp_doc', ':(exclude)tools/testing/selftests/cmdp'],
                       check=True, stdout=subprocess.DEVNULL)
        build = json.loads((a.tools_dir / 'tools-build.json').read_text())
        assert sha(a.tools_dir / 'tools-build.json') == audit['tools_build_sha256']
        assert sha(a.source / 'include/uapi/linux/cmdp.h') == build['uapi_sha256']
        names = set()
        for item in build['builds']:
            name = item['name']; names.add(name)
            assert item['exit'] == 0 and sha(self.bindir / name) == item['binary_sha256']
            assert sha(a.source / 'tools/testing/selftests/cmdp' / (name + '.c')) == item['source_sha256']
        assert {'cmdp_test', 'cmdp_monitor_test', 'cmdp_dirty_test', 'cmdp_recovery_test', 'cmdp_monitor'} <= names
        for application in ('bfs', 'pr'):
            binary = getattr(a, application)
            assert sha(binary) == audit[application + '_sha256']
            assert output(['git', '-C', binary.parent, 'rev-parse', 'HEAD']) == audit['gapbs_commit']
            assert not output(['git', '-C', binary.parent, 'status', '--porcelain'])
        assert str(a.graph) == audit['graph'] and a.graph.stat().st_size == audit['graph_bytes']
        # Hash once per phase: metadata alone cannot bind graph provenance.
        assert sha(a.graph) == audit['graph_sha256']
        assert {0, 1, 2, 3} <= os.sched_getaffinity(0)
        node0 = set()
        for item in Path('/sys/devices/system/node/node0/cpulist').read_text().strip().split(','):
            bounds = [int(x) for x in item.split('-')]
            node0.update(range(bounds[0], bounds[-1] + 1))
        assert {0, 1, 2, 3} <= node0 and Path('/sys/devices/system/node/node1').exists()

        assert '[madvise]' in self.before['settings'][previous.SETTINGS[0]]
        assert self.before['numa_balancing'] == '1'
        mem = dict(re.findall(r'^(\w+):\s+(\d+)', self.before['meminfo'], re.M))
        assert int(mem['MemAvailable']) > 64 * 1024 * 1024
        assert int(mem['SwapFree']) >= 64 * 1024
        self.guard('preflight')
        save(self.out / 'source-provenance.json', {'commit': head, 'kernel_reference': a.kernel_commit,
             'kernel_image_sha256': a.kernel_sha256, 'running_build_id': a.kernel_build_id,
             'postboot_audit_sha256': sha(a.base / 'postboot-audit.json'),
             'graph_sha256': audit['graph_sha256'],
             'bfs_path': str(a.bfs), 'pr_path': str(a.pr),
             'bfs_sha256': audit['bfs_sha256'], 'pr_sha256': audit['pr_sha256']})
        save(self.out / 'phase-profile.json', {'phase': a.operation, 'application': a.application,
             'threads': a.threads, 'period_ms': 10000, 'quiet_ms': 10000, 'poll_ms': 1000,
             'target_total_elapsed_seconds': 240, 'host_settings_changed': False,
             'observation_scope': 'launch through exit and STOP, including graph load and initialization',
             'pr_max_iterations': 1000, 'pr_tolerance': 1e-4,
             'generated_matrix': {'applications': ['bfs', 'pr'], 'threads': [1, 4],
                                  'quiet_ms': 10, 'period_ms': 100} if a.operation == 'small' else None})

    def receipt(self, phase, app=None, threads=None):
        name = phase if phase == 'small' else '{}-{}-t{}'.format(app, phase, threads)
        path = self.a.base / name / 'summary.json'
        result = json.loads(path.read_text())
        assert result['pass'] and result['host_after']['boot_id'] == self.before['boot_id']
        assert result['kernel_commit'] == self.a.kernel_commit
        assert result['source_commit'] == self.a.expected_source_commit
        return result, path

    def tap(self, name, program, extra=()):
        pth, cmd = self.execute(name, [self.bindir / program] + list(extra), 210)
        text = (pth / 'stdout.log').read_text()
        plans = re.findall(r'^1\.\.([0-9]+)\s*$', text, re.M)
        observed = [int(x) for x in re.findall(r'^ok\s+([0-9]+)\b', text, re.M)]
        assert len(plans) == 1 and int(plans[0]) > 0
        assert observed == list(range(1, int(plans[0]) + 1))
        assert not re.search(r'^not ok|# SKIP', text, re.M)
        if program == 'cmdp_recovery_test':
            assert text.count('CMDP_RECOVERY_FIXED_VERIFIED recovered=yes') == 1
        self.results[name] = {'pass': True, 'checks': len(observed), 'elapsed_seconds': cmd['elapsed_seconds']}
        self.guard(name + '-after')

    def workload(self, name, app, threads, trials, generated=False, verify=False):
        binary = getattr(self.a, app)
        period, quiet = (100, 10) if generated else (10000, 10000)
        argv = [self.bindir / 'cmdp_monitor', '--dirty-qualify', '--quiet-ms', str(quiet),
                '--period-ms', str(period), '--poll-ms', '1000', '--max-pages',
                '262144' if generated else '8388608', '--max-history',
                '524288' if generated else '8388608', '--', binary]
        argv += ['-g', '18'] if generated else ['-f', self.a.graph]
        argv += ['-n', str(trials)]
        if app == 'pr': argv += ['-i', '1000', '-t', '1e-4', '-l']
        if verify: argv += ['-v']
        env = {'OMP_NUM_THREADS': str(threads), 'OMP_DYNAMIC': 'FALSE',
               'OMP_PROC_BIND': 'close', 'OMP_PLACES': 'cores'}
        pth, cmd = self.execute(name, argv, 300 if generated else 1200, env, monitored=True)
        validator = load('recovery_validator', self.scripts / 'validate-run.py')
        v = validator.validate(pth)
        save(pth / 'validation.json', v)
        assert v['pass'] and v['diagnostics'], v['errors']
        assert v['dirty']['mode'] == 2 and v['dirty']['quiet_ms'] == quiet
        dirty, final = v['dirty'], v['final']
        assert dirty['resource_skips'] == dirty['valid_observations'] == 0
        assert dirty['samples'] > 0 and dirty['metadata_bytes'] > 0
        assert dirty['passes_started'] == final['passes_started']
        assert dirty['passes_completed'] == final['passes_completed']
        records = [json.loads(x) for x in (pth / 'monitor.jsonl').read_text().splitlines() if x.strip()]
        requests = [r for r in records if r.get('event') == 'stop_request']
        returns = [r for r in records if r.get('event') == 'stop_return']
        assert len(requests) == len(returns) == 1 and returns[0]['errno'] == 0
        assert [r.get('exit_code') for r in records if r.get('event') == 'target_exit'] == [0]

        log = (pth / 'target.log').read_text()
        times = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)]
        assert len(times) == trials and all(math.isfinite(x) and x > 0 for x in times)
        marks = re.findall(r'Verification:\s*(PASS|FAIL)', log)
        assert marks == (['PASS'] * trials if verify else [])
        rows = [json.loads(x) for x in (pth / 'thread-samples.jsonl').read_text().splitlines() if x.strip()]
        matching = [r for r in rows if r['exe'] == str(binary) and len(r['threads']) == threads]
        assert matching and all(r['thp_enabled'] == '0' for r in matching)
        def bound(row):
            return (all(len(t['cpu_allowed']) == 1 for t in row['threads']) and
                    {t['cpu_allowed'][0] for t in row['threads']} == set(range(threads)))
        first = next((i for i, r in enumerate(matching) if bound(r)), None)
        assert first is not None and all(bound(r) for r in matching[first:])
        growth = []
        for x, y in zip(matching[first:], matching[first + 1:]):
            before = {t['tid']: t['utime_ticks'] + t['stime_ticks'] for t in x['threads']}
            after = {t['tid']: t['utime_ticks'] + t['stime_ticks'] for t in y['threads']}
            if set(before) == set(after) and all(after[t] > before[t] for t in before):
                growth.append((x['monotonic_ns'], y['monotonic_ns']))
        if not generated:
            assert growth, 'No interval proved CPU activity from every requested worker'
        result = {'pass': True, 'application': app, 'trial_count': trials, 'trial_seconds': sum(times),
                  'application_and_monitor_elapsed_seconds': cmd['elapsed_seconds'],
                  'verification_passes': len(marks), 'all_worker_active_intervals': len(growth),
                  'final': v['final'], 'dirty': v['dirty'], 'diagnostics': v['diagnostics'],
                  'observation_end': v['observation_end'], 'progress': v['progress']}
        save(pth / 'extra-validation.json', result)
        self.results[name] = result
        self.guard(name + '-after')

    def frozen_trials(self):
        a = self.a
        path = a.base / ('frozen-trials-' + a.application + '-t' + str(a.threads) + '.json')
        if path.exists():
            selection = json.loads(path.read_text())
            assert selection['boot_id'] == self.before['boot_id'] and selection['kernel_commit'] == a.kernel_commit
            assert selection['application'] == a.application and selection['threads'] == a.threads
            assert type(selection['trials']) is int and selection['trials'] > 0
            assert selection['source_commit'] == a.expected_source_commit
            return selection['trials']
        if a.application == 'pr':
            trials = 1 if a.threads == 1 else 4
            reason = {'selection': 'fixed complete converged trials from the agreed protocol'}
        else:
            sizing, receipt = self.receipt('twitter-sizing', 'bfs', a.threads)
            pilot = sizing['runs']['run']
            mean = pilot['trial_seconds'] / pilot['trial_count']
            overhead = max(0, pilot['application_and_monitor_elapsed_seconds'] - pilot['trial_seconds'])
            trials = max(1, int(math.floor((240 - overhead) / mean + .5)))
            reason = {'selection': 'nearest whole trial count to 240 seconds total', 'mean_trial_seconds': mean,
                      'non_trial_seconds': overhead, 'pilot_sha256': sha(receipt)}
        selection = dict(reason, trials=trials, boot_id=self.before['boot_id'], kernel_commit=a.kernel_commit,
                         application=a.application, threads=a.threads, source_commit=a.expected_source_commit)
        # Never silently overwrite a chosen workload size.
        with path.open('x') as f: json.dump(selection, f, indent=2); f.write('\n')
        return trials

    def run(self):
        self.preflight()
        a = self.a
        if a.operation == 'small':
            for name, program in (('legacy', 'cmdp_test'), ('monitor', 'cmdp_monitor_test'), ('dirty', 'cmdp_dirty_test')):
                self.tap(name, program)
            for threads in (1, 4): self.tap('recovery-t' + str(threads), 'cmdp_recovery_test', ('--threads', str(threads)))
            for app in ('bfs', 'pr'):
                for threads in (1, 4): self.workload(app + str(threads), app, threads, 8, generated=True, verify=True)
        else:
            self.receipt('small')
            if a.operation != 'twitter-verify': self.receipt('twitter-verify', a.application, a.threads)
            if a.operation == 'twitter-verify': trials = 3 if a.application == 'bfs' else 1
            elif a.operation == 'twitter-sizing':
                assert a.application == 'bfs', 'PR uses fixed converged trial counts, not another sizing run'
                trials = 16 if a.threads == 1 else 36
            else: trials = self.frozen_trials()
            self.workload('run', a.application, a.threads, trials, verify=a.operation == 'twitter-verify')
        final = self.guard('final')
        save(self.out / 'summary.json', {'pass': True, 'phase': a.operation, 'application': a.application,
             'threads': a.threads, 'kernel_commit': a.kernel_commit, 'source_commit': a.expected_source_commit,
             'at': previous.now(), 'runs': self.results, 'host_after': final})
        self.state('complete', passed=True)
        (self.out / 'exit').write_text('0\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase', choices=('small', 'twitter-verify', 'twitter-sizing', 'twitter-observe'))
    p.add_argument('--application', choices=('bfs', 'pr'), default='bfs')
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-rearm-recovery'))
    p.add_argument('--threads', type=int, choices=(1, 4), default=1)
    p.add_argument('--bfs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/bfs'))
    p.add_argument('--pr', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/pr'))
    p.add_argument('--graph', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/benchmark/graphs/twitter.sg'))
    p.add_argument('--kernel-commit', required=True)
    p.add_argument('--kernel-sha256', required=True)
    p.add_argument('--kernel-build-id', required=True)
    p.add_argument('--expected-source-commit', required=True)
    a = p.parse_args()
    a.tools_dir = a.base / 'tools'
    def interrupted(signum, frame):
        raise RuntimeError('Harness interrupted by signal ' + str(signum))
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    harness = Recovery(a)
    try: harness.run()
    except Exception as exc:
        harness.failure(exc)
        raise


if __name__ == '__main__':
    main()
