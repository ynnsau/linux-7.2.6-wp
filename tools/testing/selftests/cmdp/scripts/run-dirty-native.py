#!/usr/bin/env python3
"""Sequential native CMD-P dirty-policy validation; never changes host settings.

All runs launch the application under cmdp_monitor, so observation includes
graph loading, initialization, BFS trials and process teardown. Verification
is a separate phase from the sustained measurement.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback

RELEASE = '7.2.6-cmdp-dirty-production'
KERNEL_COMMIT = 'f614f90e6b5482c52f14f9135ac1e04c6bef9541'
SETTINGS = ('/sys/kernel/mm/transparent_hugepage/enabled',
            '/proc/sys/kernel/watchdog', '/proc/sys/kernel/nmi_watchdog',
            '/proc/sys/kernel/hung_task_timeout_secs', '/proc/sys/kernel/panic',
            '/proc/sys/kernel/panic_on_oops')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def output(argv):
    return subprocess.check_output([str(x) for x in argv]).decode(errors='replace').strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats():
    return {k: int(v) for k, v in (s.split() for s in
            Path('/sys/kernel/debug/cmdp/stats').read_text().splitlines())}


def snapshot():
    return {'at': now(), 'kernel': output(['uname', '-r']),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'stats': stats(), 'numa_balancing': Path('/proc/sys/kernel/numa_balancing').read_text().strip(),
            'taint': Path('/proc/sys/kernel/tainted').read_text().strip(),
            'meminfo': Path('/proc/meminfo').read_text(),
            'swaps': Path('/proc/swaps').read_text(),
            'settings': {p: Path(p).read_text().strip() for p in SETTINGS},
            'governors': sorted(set(p.read_text().strip() for p in
                Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_governor'))),
            'loadavg': Path('/proc/loadavg').read_text().strip()}


class Harness:
    def __init__(self, args):
        self.a = args
        self.scripts = args.source / 'tools/testing/selftests/cmdp/scripts'
        self.bindir = args.tools_dir / 'bin'
        suffix = '' if args.phase == 'small' else '-t' + str(args.threads)
        self.out = args.base / (args.phase + suffix)
        self.out.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(__file__, str(self.out / 'harness.py'))
        self.before = snapshot()
        self.log_before = output(['dmesg']).splitlines()
        save(self.out / 'host-before.json', self.before)
        (self.out / 'kernel-before.log').write_text('\n'.join(self.log_before) + '\n')
        self.results = {}
        self.active_process = None

    def state(self, stage, **extra):
        save(self.out / 'state.json', dict({'stage': stage, 'at': now()}, **extra))

    def guard(self, label):
        s = snapshot()
        save(self.out / (label + '-host.json'), s)
        assert s['kernel'] == RELEASE and s['boot_id'] == self.before['boot_id']
        assert s['numa_balancing'] == self.before['numa_balancing'] == '1'
        for key in ('taint', 'settings', 'governors'):
            assert s[key] == self.before[key], (key, s[key], self.before[key])
        assert all(s['stats'][k] == 0 for k in ('entries', 'managed', 'isolated')), s['stats']
        assert s['stats']['arms'] == s['stats']['publishes'] == s['stats']['revokes'], s['stats']
        log = output(['dmesg']).splitlines()
        assert log[:len(self.log_before)] == self.log_before, 'Kernel log continuity lost'
        delta = log[len(self.log_before):]
        (self.out / 'kernel-new.log').write_text('\n'.join(delta) + '\n')
        faults = [line for line in delta if re.search(
            r'BUG:|WARNING:|Oops:|KASAN:|Kernel panic|general protection fault|possible circular locking|'
            r'soft lockup|hard LOCKUP|blocked for more than|rcu.*stall', line, re.I)]
        assert not faults, faults
        return s

    def preflight(self):
        a = self.a
        assert os.geteuid() == 0 and self.before['kernel'] == RELEASE
        assert self.before['numa_balancing'] == '1'
        source = {'commit': output(['git', '-C', a.source, 'rev-parse', 'HEAD']),
                  'status': output(['git', '-C', a.source, 'status', '--porcelain']),
                  'kernel_reference': KERNEL_COMMIT}
        save(self.out / 'source-provenance.json', source)
        assert not source['status'], 'Commit source changes before running native tests'
        if a.expected_source_commit:
            assert source['commit'] == a.expected_source_commit
        # Documentation/harness commits may advance HEAD without changing the
        # installed and VM-validated kernel implementation.
        subprocess.run(['git', '-C', str(a.source), 'diff', '--exit-code', KERNEL_COMMIT,
                        '--', '.', ':(exclude)wp_doc',
                        ':(exclude)tools/testing/selftests/cmdp'], check=True,
                       stdout=subprocess.DEVNULL)
        build = json.loads((a.tools_dir / 'tools-build.json').read_text())
        names = set()
        for item in build['builds']:
            assert item['exit'] == 0
            names.add(item['name'])
            assert sha(self.bindir / item['name']) == item['binary_sha256']
            assert sha(a.source / 'tools/testing/selftests/cmdp' / (item['name'] + '.c')) == item['source_sha256']
        assert {'cmdp_test', 'cmdp_monitor_test', 'cmdp_dirty_test', 'cmdp_monitor'} <= names
        assert sha(a.source / 'include/uapi/linux/cmdp.h') == build['uapi_sha256']
        save(self.out / 'tools-build.json', build)
        audit = json.loads((a.base / 'postboot-audit.json').read_text())
        assert audit['kernel'] == RELEASE and audit['boot_id'] == self.before['boot_id']
        assert sha(a.bfs) == audit['bfs_sha256']
        assert output(['git', '-C', a.bfs.parent, 'rev-parse', 'HEAD']) == audit['gapbs_commit']
        mem = dict(re.findall(r'^(\w+):\s+(\d+)', self.before['meminfo'], re.M))
        assert int(mem['MemAvailable']) > 64 * 1024 * 1024
        assert shutil.disk_usage(str(a.base)).free > 20 * 1024 ** 3
        assert '[madvise]' in self.before['settings'][SETTINGS[0]]
        assert {0, 1, 2, 3} <= os.sched_getaffinity(0)
        assert Path('/sys/devices/system/node/node0').exists()
        assert Path('/sys/devices/system/node/node1').exists()
        assert int(mem['SwapFree']) >= 64 * 1024, 'Dirty fixture needs existing swap for its own 8 MiB mapping'
        self.guard('preflight')
        profile = {'phase': a.phase, 'threads': a.threads, 'period_ms': 10000,
                   'poll_ms': 1000, 'quiet_ms': 10000, 'dirty_mode': 'qualify',
                   'max_pages': 8388608, 'max_history': 8388608,
                   'target_compute_seconds': a.target_seconds,
                   'observation_scope': 'process launch through exit and STOP; includes graph loading and initialization',
                   'host_settings_changed': False}
        if a.phase == 'small':
            profile.update({'threads': [1, 4], 'period_ms': 100, 'quiet_ms': 10,
                            'dirty_mode': ['observe', 'qualify'],
                            'max_pages': 262144, 'max_history': 524288,
                            'limits_scope': 'generated BFS commands; direct TAP fixtures use their own limits'})
        save(self.out / 'phase-profile.json', profile)

    def sample_threads(self, target_pid):
        proc = Path('/proc') / str(target_pid)
        status = (proc / 'status').read_text()
        sample = {'monotonic_ns': int(time.monotonic() * 1e9), 'target_pid': target_pid,
                  'exe': os.readlink(str(proc / 'exe')), 'threads': [],
                  'thp_enabled': re.search(r'^THP_enabled:\s*(.*)$', status, re.M).group(1)}
        for task in sorted((proc / 'task').iterdir()):
            fields = (task / 'stat').read_text().rsplit(')', 1)[1].split()
            sample['threads'].append({'tid': int(task.name),
                'cpu_allowed': sorted(os.sched_getaffinity(int(task.name))),
                'utime_ticks': int(fields[11]), 'stime_ticks': int(fields[12]),
                'processor': int(fields[36])})
        return sample

    def execute(self, name, argv, seconds, env=None, monitored=False):
        pth = self.out / name
        pth.mkdir()
        stdout = pth / ('monitor.jsonl' if monitored else 'stdout.log')
        record = {'argv': [str(x) for x in argv], 'started_at': now(),
                  'timeout_seconds': seconds, 'environment': env or {}, 'monitored': monitored}
        begin = time.monotonic()
        with stdout.open('w') as out, (pth / 'target.log').open('w') as err:
            p = subprocess.Popen(record['argv'], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 env=dict(os.environ, **(env or {})), start_new_session=True)
            self.active_process = p
            record['pid'] = p.pid
            save(pth / 'command.json', record)
            self.state(name, pid=p.pid, command=record['argv'])
            target_pid = None
            previous_progress = None
            with (pth / 'thread-samples.jsonl').open('w') as samples, (pth / 'trial-progress.jsonl').open('w') as trial_progress:
                while p.poll() is None:
                    if time.monotonic() - begin > seconds:
                        record['timed_out'] = True
                        # Let the controller STOP/drain before escalating to the
                        # process group that this harness itself created.
                        p.send_signal(signal.SIGTERM)
                        try:
                            p.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            os.killpg(p.pid, signal.SIGKILL)
                            try:
                                p.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                record['unreaped_after_kill'] = True
                                save(pth / 'command.json', record)
                                raise RuntimeError('Owned process did not exit; stop all further tests')
                        break
                    if monitored:
                        target_log = (pth / 'target.log').read_text(errors='replace')
                        observed_progress = (len(re.findall(r'Trial Time:', target_log)),
                                             len(re.findall(r'Verification:\s*PASS', target_log)))
                        if observed_progress != previous_progress:
                            trial_progress.write(json.dumps({'monotonic_ns': int(time.monotonic() * 1e9),
                                'trial_lines_observed': observed_progress[0],
                                'verification_pass_lines_observed': observed_progress[1],
                                'note': 'Log-observation time; application buffering may delay output'}) + '\n')
                            trial_progress.flush()
                            previous_progress = observed_progress
                        if target_pid is None:
                            try:
                                with stdout.open() as first:
                                    row = json.loads(first.readline())
                                if row.get('event') == 'controller':
                                    target_pid = row['pid']
                            except (ValueError, OSError):
                                pass
                        if target_pid:
                            try:
                                samples.write(json.dumps(self.sample_threads(target_pid)) + '\n')
                                samples.flush()
                            except (OSError, AttributeError, ValueError, IndexError):
                                pass  # /proc entries may disappear during normal exit.
                    time.sleep(.1 if self.a.phase == 'small' else .5)
        record.update({'exit': p.returncode, 'ended_at': now(),
                       'elapsed_seconds': time.monotonic() - begin})
        save(pth / 'command.json', record)
        (pth / 'controller.exit').write_text(str(p.returncode) + '\n')
        assert p.returncode == 0 and not record.get('timed_out'), record
        self.active_process = None
        return pth, record

    def validate(self, pth, threads, trials, mode, quiet_ms, verification=None, compute=0):
        cmd = ['python3', str(self.scripts / 'validate-run.py'), str(pth),
               '--min-sweeps', '1', '--min-compute-seconds', str(compute)]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        (pth / 'validator.log').write_text(result.stdout + result.stderr)
        subprocess.run(['python3', str(self.scripts / 'report-run.py'), str(pth)],
                       check=True, stdout=subprocess.DEVNULL)
        assert result.returncode == 0, (pth / 'validator.log').read_text()
        rows = [json.loads(s) for s in (pth / 'monitor.jsonl').read_text().splitlines() if s.strip()]
        requests = [r for r in rows if r.get('event') == 'stop_request']
        returns = [r for r in rows if r.get('event') == 'stop_return']
        assert len(requests) == len(returns) == 1 and returns[0]['errno'] == 0
        assert [r.get('exit_code') for r in rows if r.get('event') == 'target_exit'] == [0]
        log = (pth / 'target.log').read_text()
        assert len(re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)) == trials
        if verification is not None:
            assert re.findall(r'Verification:\s*(PASS|FAIL)', log) == ['PASS'] * verification, log[-3000:]
        else:
            assert not re.search(r'Verification:', log), 'Verifier activity must be separate from observation'
        matching = [r for r in (json.loads(s) for s in (pth / 'thread-samples.jsonl').read_text().splitlines())
                    if len(r['threads']) == threads and r['exe'] == str(self.a.bfs)]
        assert matching, 'Did not observe expected BFS thread count: ' + str(threads)
        assert all(r['thp_enabled'] == '0' for r in matching)
        # exec is visible before libgomp applies OMP_PROC_BIND. Require binding
        # once it has appeared, preserving startup samples as separate evidence.
        def bound(row):
            return (all(len(t['cpu_allowed']) == 1 for t in row['threads']) and
                    len(set(t['cpu_allowed'][0] for t in row['threads'])) == threads)
        first_bound = next((i for i, row in enumerate(matching) if bound(row)), None)
        assert first_bound is not None, 'OpenMP binding was never observed'
        bound_samples = matching[first_bound:]
        assert all(bound(row) for row in bound_samples), 'Thread binding changed after initialization'
        v = json.loads((pth / 'validation.json').read_text())
        d = v['dirty']
        assert d['mode'] == mode and d['quiet_ms'] == quiet_ms
        assert d['samples'] > 0 and d['metadata_bytes'] > 0
        assert d['resource_skips'] == d['valid_observations'] == 0
        assert v['final']['resource_failures'] == v['final']['reports_dropped'] == 0
        assert d['passes_started'] == v['final']['passes_started']
        assert d['passes_completed'] == v['final']['passes_completed']
        assert d['metadata_bytes'] == v['final']['history_entries'] * 192
        if mode == 2:
            assert v['final']['first_arms'] + v['final']['rearms'] > 0
        command = json.loads((pth / 'command.json').read_text())
        assert not any(arg in command['argv'] for arg in ('--duration-sec', '--sweeps', '--once'))
        summary = {'completed_sweeps': v['completed_sweeps'], 'trial_count': v['trial_count'],
                   'trial_seconds': v['total_trial_seconds'], 'observation_end': v['observation_end'],
                   'progress': v['progress'], 'dirty': d, 'final': v['final'], 'stop_return': returns[0],
                   'verification_passes': verification, 'thread_samples': len(matching),
                   'startup_samples_before_binding': first_bound,
                   'bound_thread_samples': len(bound_samples),
                   'last_thread_sample': matching[-1], 'pass': True,
                   'application_and_monitor_elapsed_seconds': command['elapsed_seconds']}
        save(pth / 'extra-validation.json', summary)
        return summary

    def bfs_run(self, name, threads, trials, mode, period, quiet, generated=False, verify=False, compute=0):
        argv = [self.bindir / 'cmdp_monitor', '--dirty-' + mode, '--quiet-ms', str(quiet),
                '--period-ms', str(period), '--poll-ms', '1000', '--max-pages',
                '262144' if generated else '8388608', '--max-history',
                '524288' if generated else '8388608', '--', self.a.bfs]
        argv += ['-g', '18'] if generated else ['-f', self.a.graph]
        argv += ['-n', str(trials)]
        if verify:
            argv += ['-v']
        env = {'OMP_NUM_THREADS': str(threads), 'OMP_DYNAMIC': 'FALSE',
               'OMP_PROC_BIND': 'close', 'OMP_PLACES': 'cores'}
        pth, unused = self.execute(name, argv, 180 if generated else 900, env, monitored=True)
        self.results[name] = self.validate(pth, threads, trials, 1 if mode == 'observe' else 2,
                                          quiet, trials if verify else None, compute)
        self.guard(name + '-after')

    def run(self):
        self.preflight()
        a = self.a
        if a.phase == 'small':
            for name, program, count in [('legacy', 'cmdp_test', 18),
                                         ('monitor', 'cmdp_monitor_test', 59),
                                         ('dirty', 'cmdp_dirty_test', 96)]:
                pth, command = self.execute(name, [self.bindir / program], 210)
                tap = (pth / 'stdout.log').read_text()
                assert len(re.findall(r'^ok\s+\d+', tap, re.M)) == count, tap
                assert not re.search(r'^not ok|# SKIP', tap, re.M), tap
                assert re.search(r'^1\.\.' + str(count) + r'\s*$', tap, re.M), tap
                self.results[name] = {'checks': count, 'pass': True,
                                      'elapsed_seconds': command['elapsed_seconds']}
                self.guard(name + '-after')
            for mode in ('observe', 'qualify'):
                for threads in (1, 4):
                    self.bfs_run(mode + '-bfs' + str(threads), threads, 8,
                                 mode, 100, 10, generated=True, verify=True)
        else:
            prior = json.loads((a.base / 'small/summary.json').read_text())
            assert prior['pass'] and prior['host_after']['boot_id'] == self.before['boot_id']
            with a.graph.open('rb') as f:
                header = f.read(17)
            provenance = {'bfs_sha256': sha(a.bfs),
                          'gapbs_commit': output(['git', '-C', a.bfs.parent, 'rev-parse', 'HEAD']),
                          'gapbs_status': output(['git', '-C', a.bfs.parent, 'status', '--porcelain']),
                          'graph': str(a.graph), 'graph_bytes': a.graph.stat().st_size,
                          'directed': header[0], 'edges': int.from_bytes(header[1:9], 'little'),
                          'vertices': int.from_bytes(header[9:17], 'little'),
                          'graph_full_hash_recomputed': False}
            save(self.out / 'application-provenance.json', provenance)
            assert not provenance['gapbs_status']
            assert provenance['graph_bytes'] == 12732173745 and provenance['directed'] == 1
            assert provenance['edges'] == 1468364884 and provenance['vertices'] == 61578415
            if a.phase != 'twitter-verify':
                prior = json.loads((a.base / ('twitter-verify-t' + str(a.threads)) / 'summary.json').read_text())
                assert prior['pass'] and prior['host_after']['boot_id'] == self.before['boot_id']
            trials = a.trials
            if trials is None and a.phase == 'twitter-verify':
                trials = 3
            if trials is None and a.phase == 'twitter-sizing':
                trials = 16 if a.threads == 1 else 36
            if trials is None:
                prior = json.loads((a.base / ('twitter-sizing-t' + str(a.threads)) / 'summary.json').read_text())
                assert prior['pass'] and prior['host_after']['boot_id'] == self.before['boot_id']
                measured = prior['runs']['run']
                mean = measured['trial_seconds'] / measured['trial_count']
                overhead = max(0, measured['application_and_monitor_elapsed_seconds'] - measured['trial_seconds'])
                trials = max(1, math.ceil(a.target_seconds / mean))
                save(self.out / 'sizing-selection.json', {'source': str(a.base / ('twitter-sizing-t' + str(a.threads))),
                     'mean_trial_seconds': mean, 'non_trial_seconds': overhead, 'selected_trials': trials,
                     'target_compute_seconds': a.target_seconds})
            self.bfs_run('run', a.threads, trials, 'qualify', 10000, 10000,
                         verify=a.phase == 'twitter-verify',
                         compute=100 if a.phase == 'twitter-observe' else 0)
        final = self.guard('final')
        save(self.out / 'summary.json', {'pass': True, 'phase': a.phase, 'threads': a.threads,
                                       'at': now(), 'runs': self.results, 'host_after': final})
        self.state('complete', passed=True)
        (self.out / 'exit').write_text('0\n')

    def failure(self, exc):
        # On an unexpected harness exception, drain the owned controller first.
        # Never let an interrupted validation advance into another experiment.
        p = self.active_process
        if p is not None:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                    p.wait(timeout=45)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait(timeout=5)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        save(self.out / 'summary.json', {'pass': False, 'phase': self.a.phase, 'threads': self.a.threads,
                                       'at': now(), 'error': str(exc), 'traceback': traceback.format_exc(),
                                       'completed_runs': self.results})
        self.state('failed', error=str(exc))
        (self.out / 'exit').write_text('1\n')
        for raw in self.out.glob('*/monitor.jsonl'):
            if not (raw.parent / 'validation.json').exists():
                with (raw.parent / 'failure-validation.log').open('w') as f:
                    subprocess.run(['python3', str(self.scripts / 'validate-run.py'), str(raw.parent)], stdout=f, stderr=f)
                    subprocess.run(['python3', str(self.scripts / 'report-run.py'), str(raw.parent)], stdout=f, stderr=f)
        try:
            self.guard('failure')
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('small', 'twitter-verify', 'twitter-sizing', 'twitter-observe'))
    parser.add_argument('--base', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/dirty-native-bfs-20261021'))
    parser.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-dirty-arm'))
    parser.add_argument('--tools-dir', type=Path)
    parser.add_argument('--bfs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/bfs'))
    parser.add_argument('--graph', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/benchmark/graphs/twitter.sg'))
    parser.add_argument('--threads', type=int, choices=(1, 4), default=1)
    parser.add_argument('--trials', type=int)
    parser.add_argument('--target-seconds', type=float, default=130)
    parser.add_argument('--expected-source-commit', default=KERNEL_COMMIT)
    args = parser.parse_args()
    if args.trials is not None and args.trials <= 0:
        parser.error('--trials must be positive')
    if args.target_seconds <= 0:
        parser.error('--target-seconds must be positive')
    if args.tools_dir is None:
        args.tools_dir = args.base / 'tools'
    def interrupted(signum, unused_frame):
        raise RuntimeError('Harness interrupted by signal ' + str(signum))
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    harness = Harness(args)
    try:
        harness.run()
    except Exception as exc:
        harness.failure(exc)
        raise


if __name__ == '__main__':
    main()
