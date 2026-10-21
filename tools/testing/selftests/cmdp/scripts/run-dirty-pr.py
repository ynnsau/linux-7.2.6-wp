#!/usr/bin/env python3
"""Sequential native CMD-P dirty-policy validation; never changes host settings.

All runs launch the application under cmdp_monitor, so observation includes
graph loading, initialization, PageRank trials and process teardown. Verification
is a separate phase from the sustained measurement. The observation targets
120 seconds of total application/controller elapsed time using whole converged
trials, with the official GAPBS PageRank recipe (-i1000 -t1e-4).
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
SOURCE_COMMIT = 'b7c8523ec28de955a6e7e45c0e2f6b100fa5ddd3'
BFS_RUN = Path('/home/yans3/cmdp-v2/periodic/dirty-native-bfs-20261021')
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


def verification_sizing(source_dir, boot_id, threads):
    """Read a passed same-boot verification pilot, excluding verifier time."""
    summary_path = source_dir / 'summary.json'
    command_path = source_dir / 'run/command.json'
    log_path = source_dir / 'run/target.log'
    prior = json.loads(summary_path.read_text())
    assert prior['pass'] and prior['phase'] == 'twitter-verify'
    assert prior['threads'] == threads and prior['host_after']['boot_id'] == boot_id
    measured = prior['runs']['run']
    count = measured['trial_count']
    assert measured['pass'] and count > 0 and measured['verification_passes'] == count
    command = json.loads(command_path.read_text())
    assert command['exit'] == 0 and not command.get('timed_out') and '-v' in command['argv']
    log = log_path.read_text()
    verification_times = [float(x) for x in re.findall(r'Verification Time:\s*([0-9.eE+-]+)', log)]
    trial_times = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)]
    assert len(verification_times) == len(trial_times) == count
    assert re.findall(r'Verification:\s*(PASS|FAIL)', log) == ['PASS'] * count
    assert all(math.isfinite(x) and x > 0 for x in verification_times + trial_times)
    trial_seconds = sum(trial_times)
    verifier_seconds = sum(verification_times)
    elapsed = measured['application_and_monitor_elapsed_seconds']
    assert math.isfinite(elapsed) and elapsed > 0
    assert math.isclose(trial_seconds, measured['trial_seconds'], rel_tol=1e-12, abs_tol=1e-6)
    assert elapsed + count * 0.001 >= trial_seconds + verifier_seconds
    return {'mean_trial_seconds': trial_seconds / count,
            'non_trial_seconds': max(0, elapsed - trial_seconds - verifier_seconds),
            'verification_seconds_removed': verifier_seconds,
            'source_phase': 'twitter-verify', 'source': str(source_dir),
            'source_summary_sha256': sha(summary_path),
            'source_command': command, 'source_command_sha256': sha(command_path),
            'source_log_sha256': sha(log_path),
            'derivation': 'same-boot passed verification pilot; nontrial = elapsed - trial sum - verification sum'}


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
        assert sha(a.pr) == audit['pr_sha256']
        assert output(['git', '-C', a.pr.parent, 'rev-parse', 'HEAD']) == audit['gapbs_commit']
        assert not output(['git', '-C', a.pr.parent, 'status', '--porcelain'])
        reuse = audit['native_tools_reuse']
        assert str(a.tools_dir) == reuse['tools_dir']
        assert sha(a.tools_dir / 'tools-build.json') == reuse['build_record_sha256']
        assert sha(a.base / 'reused-tools-build.json') == reuse['build_record_sha256']
        assert sha(a.prior_small_receipt) == reuse['fixture_summary_sha256']
        assert reuse['checks'] == 173
        # Reuse the already-passed native kernel fixtures on this same boot;
        # repeat application correctness for PageRank below.
        prior = json.loads(a.prior_small_receipt.read_text())
        assert prior['pass'] and prior['phase'] == 'small'
        assert prior['host_after']['kernel'] == RELEASE
        assert prior['host_after']['boot_id'] == self.before['boot_id']
        for name, count in (('legacy', 18), ('monitor', 59), ('dirty', 96)):
            assert prior['runs'][name]['pass'] and prior['runs'][name]['checks'] == count
        save(self.out / 'prior-native-suite-receipt.json', {
            'source': str(a.prior_small_receipt), 'sha256': sha(a.prior_small_receipt),
            'fixture_checks': 173, 'same_boot': True, 'receipt': prior})
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
                   'target_total_elapsed_seconds': a.target_seconds,
                   'observation_scope': 'process launch through exit and STOP; includes graph loading and initialization',
                   'host_settings_changed': False, 'workload': 'PageRankPullGS',
                   'max_iterations': 1000, 'tolerance': 1e-4, 'iteration_logging': True}
        if a.phase == 'small':
            profile.update({'threads': [1, 4], 'period_ms': 100, 'quiet_ms': 10,
                            'dirty_mode': ['observe', 'qualify'],
                            'max_pages': 262144, 'max_history': 524288,
                            'limits_scope': 'generated PageRank commands'})
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
        iteration_groups = []
        current_iterations = []
        for line in log.splitlines():
            step = re.match(r'^\s*(\d+)\s+([0-9.eE+-]+)\s*$', line)
            if step:
                current_iterations.append((int(step.group(1)), float(step.group(2))))
            elif 'Trial Time:' in line:
                assert current_iterations, 'Missing PageRank iteration log'
                assert [x[0] for x in current_iterations] == list(range(len(current_iterations)))
                assert len(current_iterations) <= 1000
                assert math.isfinite(current_iterations[-1][1])
                # GAPBS prints only five decimal places. Record this rounded
                # iterate-change error separately from verified push residuals.
                assert current_iterations[-1][1] <= 0.000105, 'PageRank did not approach requested convergence'
                iteration_groups.append({'iterations': len(current_iterations),
                                         'last_logged_change_rounded': current_iterations[-1][1]})
                current_iterations = []
        assert len(iteration_groups) == trials and not current_iterations
        if verification is not None:
            assert re.findall(r'Verification:\s*(PASS|FAIL)', log) == ['PASS'] * verification, log[-3000:]
        else:
            assert not re.search(r'Verification:', log), 'Verifier activity must be separate from observation'
        matching = [r for r in (json.loads(s) for s in (pth / 'thread-samples.jsonl').read_text().splitlines())
                    if len(r['threads']) == threads and r['exe'] == str(self.a.pr)]
        assert matching, 'Did not observe expected PageRank thread count: ' + str(threads)
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
        ticks = {}
        for row in bound_samples:
            for task in row['threads']:
                value = task['utime_ticks'] + task['stime_ticks']
                bounds = ticks.setdefault(task['tid'], [value, value])
                bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)
        active_workers = sum(hi > lo for lo, hi in ticks.values())
        assert active_workers >= threads, 'Not all expected workers accumulated CPU ticks'
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
                   'workers_with_cpu_tick_growth': active_workers,
                   'pagerank_trials': iteration_groups,
                   'last_thread_sample': matching[-1], 'pass': True,
                   'application_and_monitor_elapsed_seconds': command['elapsed_seconds']}
        if self.a.phase == 'twitter-observe':
            summary['target_total_elapsed_seconds'] = self.a.target_seconds
            summary['target_elapsed_difference_seconds'] = command['elapsed_seconds'] - self.a.target_seconds
        save(pth / 'extra-validation.json', summary)
        return summary

    def pr_run(self, name, threads, trials, mode, period, quiet, generated=False, verify=False, compute=0):
        argv = [self.bindir / 'cmdp_monitor', '--dirty-' + mode, '--quiet-ms', str(quiet),
                '--period-ms', str(period), '--poll-ms', '1000', '--max-pages',
                '262144' if generated else '8388608', '--max-history',
                '524288' if generated else '8388608', '--', self.a.pr]
        argv += ['-g', '18'] if generated else ['-f', self.a.graph]
        argv += ['-n', str(trials), '-i', '1000', '-t', '1e-4', '-l']
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
            for mode in ('observe', 'qualify'):
                for threads in (1, 4):
                    self.pr_run(mode + '-pr' + str(threads), threads, 8,
                                 mode, 100, 10, generated=True, verify=True)
        else:
            prior = json.loads((a.base / 'small/summary.json').read_text())
            assert prior['pass'] and prior['host_after']['boot_id'] == self.before['boot_id']
            with a.graph.open('rb') as f:
                header = f.read(17)
            provenance = {'pr_sha256': sha(a.pr),
                          'gapbs_commit': output(['git', '-C', a.pr.parent, 'rev-parse', 'HEAD']),
                          'gapbs_status': output(['git', '-C', a.pr.parent, 'status', '--porcelain']),
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
                trials = 1
            if trials is None and a.phase == 'twitter-sizing':
                trials = 1
            if trials is None:
                if a.sizing_from_verification:
                    sizing = verification_sizing(a.base / ('twitter-verify-t' + str(a.threads)),
                                                 self.before['boot_id'], a.threads)
                else:
                    source_dir = a.base / ('twitter-sizing-t' + str(a.threads))
                    prior = json.loads((source_dir / 'summary.json').read_text())
                    assert prior['pass'] and prior['host_after']['boot_id'] == self.before['boot_id']
                    measured = prior['runs']['run']
                    sizing = {'source': str(source_dir), 'source_phase': 'twitter-sizing',
                              'mean_trial_seconds': measured['trial_seconds'] / measured['trial_count'],
                              'non_trial_seconds': max(0, measured['application_and_monitor_elapsed_seconds'] - measured['trial_seconds']),
                              'verification_seconds_removed': 0}
                mean, overhead = sizing['mean_trial_seconds'], sizing['non_trial_seconds']
                assert mean > 0
                # Whole trials preserve the algorithm's normal convergence.
                # Nearest positive integer minimizes estimated total-time error.
                ideal = (a.target_seconds - overhead) / mean
                trials = max(1, int(math.floor(ideal + 0.5)))
                sizing.update({'selected_trials': trials,
                     'target_total_elapsed_seconds': a.target_seconds,
                     'estimated_total_elapsed_seconds': overhead + trials * mean,
                     'ideal_fractional_trials': ideal,
                     'selection_rule': 'nearest positive whole trial count; excludes verification'})
                save(self.out / 'sizing-selection.json', sizing)
            self.pr_run('run', a.threads, trials, 'qualify', 10000, 10000,
                         verify=a.phase == 'twitter-verify',
                         compute=0)
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
    parser.add_argument('--base', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/dirty-native-pr-20261021'))
    parser.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-dirty-arm'))
    parser.add_argument('--tools-dir', type=Path, default=BFS_RUN / 'tools')
    parser.add_argument('--prior-small-receipt', type=Path)
    parser.add_argument('--pr', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/pr'))
    parser.add_argument('--graph', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs/benchmark/graphs/twitter.sg'))
    parser.add_argument('--threads', type=int, choices=(1, 4), default=1)
    parser.add_argument('--trials', type=int)
    parser.add_argument('--target-seconds', type=float, default=120)
    parser.add_argument('--sizing-from-verification', action='store_true',
                        help='For twitter-observe only, size from a passed verification pilot after subtracting verifier time')
    parser.add_argument('--expected-source-commit', default=SOURCE_COMMIT)
    args = parser.parse_args()
    if args.trials is not None and args.trials <= 0:
        parser.error('--trials must be positive')
    if args.target_seconds <= 0:
        parser.error('--target-seconds must be positive')
    if args.sizing_from_verification and args.phase != 'twitter-observe':
        parser.error('--sizing-from-verification is only valid for twitter-observe')
    if args.sizing_from_verification and args.trials is not None:
        parser.error('--sizing-from-verification selects trial count; do not combine with --trials')
    if args.prior_small_receipt is None:
        args.prior_small_receipt = args.base / 'reused-fixture-summary.json'
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
