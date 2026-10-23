#!/usr/bin/env python3
"""Matched fixed-work A=off, B=dirty observe, C=dirty qualify campaign.

Python 3.6 compatible. No kernel/configuration changes. Every measured run
finishes whole trials; an emergency timeout fails the campaign. Evidence is
append-only by run directory. --resume skips only successful hashed receipts.
"""
import argparse
import fcntl
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

SCRIPTS = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = load('matched_native_helpers', 'run-dirty-native.py')
prepare = load('matched_prepare_helpers', 'prepare-recovery-native.py')
validator = load('matched_validator', 'validate-run.py')
save, output, now, sha = old.save, old.output, old.now, prepare.sha
KERNEL_COMMIT = '0b4d4a2a6174dc5de227252e5d900121b5fd9cb8'
CELLS = [(app, t) for app in ('bfs', 'pr', 'cc', 'bc') for t in (1, 4)]
HISTORY_BYTES_PER_PAGE = 192
SEED = 20261022


def ns():
    return int(time.monotonic() * 1e9)


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def git(repo, *args):
    return output(['git', '-c', 'safe.directory=' + str(repo), '-C', repo] + list(args))


def parse_log(text, app, count, verify):
    times = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', text)]
    assert len(times) == count and all(math.isfinite(x) and x > 0 for x in times), times
    marks = re.findall(r'Verification:\s*(PASS|FAIL)', text)
    assert marks == (['PASS'] * count if verify else []), marks
    sources = [int(x) for x in re.findall(r'^Source:\s*(\d+)', text, re.M)]
    if app in ('bfs', 'bc'):
        assert len(sources) == count, (app, sources, count)
    iterations = [int(x) for x in re.findall(r'^\s+(\d+)\s+[0-9.eE+-]+\s*$', text, re.M)]
    if app == 'pr' and not verify:
        assert iterations == list(range(21)) * count, iterations
    return dict(trial_times_s=times, trial_seconds=sum(times), sources=sources,
                pr_iteration_indices=iterations if app == 'pr' else [], verification=marks)


class Campaign:
    def __init__(self, a):
        self.a = a
        self.base = a.base
        self.monitor = self.base / 'tools/cmdp_monitor'
        self.fixture_bin = a.prior / 'tools/bin'
        self.graph = a.gapbs / 'benchmark/graphs/twitter.sg'
        self.active = None
        self.done = None
        self.summaries = {}
        self.before = old.snapshot()
        self.kernel_before = output(['dmesg']).splitlines()
        self.commit = git(a.source, 'rev-parse', 'HEAD')
        self.audit = json.loads((a.prior / 'postboot-audit.json').read_text())
        self.state('starting')

    def state(self, stage, **extra):
        record = dict(stage=stage, at=now(), driver_pid=os.getpid(), completed=len(self.summaries))
        record.update(extra)
        save(self.base / 'campaign-state.json', record)
        print(json.dumps(record), flush=True)

    def guard(self, path):
        state = old.snapshot()
        save(path, state)
        assert state['kernel'] == prepare.RELEASE
        assert state['boot_id'] == self.before['boot_id'] == self.audit['boot_id']
        assert state['numa_balancing'] == self.before['numa_balancing'] == '1'
        for field in ('taint', 'settings', 'governors'):
            assert state[field] == self.before[field], (field, state[field], self.before[field])
        counters = state['stats']
        assert all(counters[k] == 0 for k in ('entries', 'managed', 'isolated')), counters
        assert counters['arms'] == counters['publishes'] == counters['revokes'], counters
        log = output(['dmesg']).splitlines()
        assert log[:len(self.kernel_before)] == self.kernel_before, 'Kernel log continuity lost'
        delta = log[len(self.kernel_before):]
        (self.base / 'kernel-new.log').write_text('\n'.join(delta) + '\n')
        faults = [line for line in delta if re.search(
            r'BUG:|WARNING:|Oops:|KASAN:|Kernel panic|general protection fault|possible circular locking|'
            r'soft lockup|hard LOCKUP|blocked for more than|rcu.*stall', line, re.I)]
        assert not faults, faults
        assert git(self.a.source, 'rev-parse', 'HEAD') == self.commit, 'Source advanced during campaign'
        assert not git(self.a.source, 'status', '--porcelain'), 'Source is not committed'
        mem = dict(re.findall(r'^(\w+):\s+(\d+)', state['meminfo'], re.M))
        assert int(mem['MemAvailable']) > 64 * 1024 * 1024
        assert shutil.disk_usage(str(self.base)).free > 20 * 1024 ** 3
        return state

    def preflight(self):
        assert os.geteuid() == 0
        assert prepare.running_build_id() == self.audit['kernel_build_id']
        assert self.audit['kernel_commit'] == KERNEL_COMMIT
        for name, digest in self.audit['installed_files'].items():
            assert sha(Path(name)) == digest, name
        assert not git(self.a.source, 'diff', '--exit-code', KERNEL_COMMIT, '--', '.',
                       ':(exclude)wp_doc', ':(exclude)tools/testing/selftests/cmdp')
        assert git(self.a.gapbs, 'rev-parse', 'HEAD') == self.audit['gapbs_commit']
        assert not git(self.a.gapbs, 'status', '--porcelain')
        for app, unused in CELLS[::2]:
            assert sha(self.a.gapbs / app) == self.audit[app + '_sha256']
        assert self.graph.stat().st_size == self.audit['graph_bytes']
        assert sha(self.graph) == self.audit['graph_sha256']
        build = json.loads((self.base / 'tools/build-monitor.json').read_text())
        assert build['exit_code'] == 0 and sha(self.monitor) == build['binary_sha256']
        assert sha(self.a.source / 'tools/testing/selftests/cmdp/cmdp_monitor.c') == build['source_sha256']
        assert sha(self.a.source / 'include/uapi/linux/cmdp.h') == build['uapi_sha256']
        oldbuild = json.loads((self.a.prior / 'tools/tools-build.json').read_text())
        for item in oldbuild['builds']:
            if item['name'] == 'cmdp_monitor':
                continue
            assert sha(self.fixture_bin / item['name']) == item['binary_sha256']
            assert sha(self.a.source / 'tools/testing/selftests/cmdp' / (item['name'] + '.c')) == item['source_sha256']
        assert {0, 1, 2, 3} <= os.sched_getaffinity(0)
        node0 = set()
        for item in Path('/sys/devices/system/node/node0/cpulist').read_text().strip().split(','):
            bounds = [int(x) for x in item.split('-')]
            node0.update(range(bounds[0], bounds[-1] + 1))
        assert {0, 1, 2, 3} <= node0
        assert '[madvise]' in self.before['settings'][old.SETTINGS[0]]
        assert self.before['taint'] == self.audit['taint']
        source = self.base / 'source'
        source.mkdir(exist_ok=True)
        files = [SCRIPTS / x for x in ('run-matched-gapbs.py', 'run-dirty-native.py',
                 'prepare-recovery-native.py', 'validate-run.py', 'test-monitor-measurement.py')]
        files += [self.a.source / 'tools/testing/selftests/cmdp/cmdp_monitor.c',
                  self.a.source / 'include/uapi/linux/cmdp.h']
        for path in files:
            dest = source / path.name
            if dest.exists():
                assert sha(dest) == sha(path), 'Resume requires exact source snapshot'
            else:
                shutil.copyfile(str(path), str(dest))
        provenance = dict(source_commit=self.commit, kernel_commit=KERNEL_COMMIT,
            running_build_id=prepare.running_build_id(), prior_audit_sha256=sha(self.a.prior / 'postboot-audit.json'),
            graph_sha256=self.audit['graph_sha256'], graph=str(self.graph),
            gapbs_commit=self.audit['gapbs_commit'],
            binary_sha256={app: sha(self.a.gapbs / app) for app, t in CELLS[::2]},
            source_sha256={p.name: sha(p) for p in files}, monitor_sha256=sha(self.monitor),
            host_settings_changed=False, primary_clock='monotonic; before Popen through blocking wait return',
            primary_scope='launcher + graph loading + initialization + whole trials + teardown + STOP/drain',
            rss='1s /proc/PID/status in every mode; target peak from wait4; approximate, excludes kernel metadata')
        path = self.base / 'provenance.json'
        if path.exists():
            assert json.loads(path.read_text()) == provenance
        else:
            save(path, provenance)
            save(self.base / 'host-initial.json', self.before)
            (self.base / 'kernel-initial.log').write_text('\n'.join(self.kernel_before) + '\n')
            shutil.copyfile(str(self.a.prior / 'postboot-audit.json'), str(self.base / 'prior-audit.json'))
        self.guard(self.base / 'preflight-host.json')

    def sample(self, pid):
        begin = ns()
        proc = Path('/proc') / str(pid)
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        status = (proc / 'status').read_text()
        memory = {key: int(value) for key, value in re.findall(
            r'^(VmRSS|RssAnon|RssFile|RssShmem|VmSize|VmHWM):\s+(\d+)\s+kB', status, re.M)}
        threads = []
        for task in sorted((proc / 'task').iterdir()):
            fields = (task / 'stat').read_text().rsplit(')', 1)[1].split()
            threads.append(dict(tid=int(task.name), cpu_allowed=sorted(os.sched_getaffinity(int(task.name))),
                utime_ticks=int(fields[11]), stime_ticks=int(fields[12]), processor=int(fields[36])))
        thp = re.search(r'^THP_enabled:\s*(\d+)', status, re.M)
        return dict(read_begin_ns=begin, read_end_ns=ns(), pid=pid, start_ticks=int(stat[19]),
                    exe=os.readlink(str(proc / 'exe')), memory_kib=memory, threads=threads,
                    thp_enabled=thp.group(1) if thp else None)

    def observe(self, path, done):
        pid, identity, deadline = None, None, time.monotonic()
        with (path / 'memory-samples.jsonl').open('w') as stream:
            while not done.is_set():
                try:
                    if pid is None:
                        with (path / 'monitor.jsonl').open() as f:
                            first = json.loads(f.readline())
                        assert first['event'] == 'controller'
                        pid = first['pid']
                    sample = self.sample(pid)
                    if identity is None:
                        identity = sample['start_ticks']
                    assert sample['start_ticks'] == identity, 'PID identity changed'
                except (OSError, ValueError, IndexError, AssertionError) as exc:
                    sample = dict(read_end_ns=ns(), pid=pid, error=str(exc))
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                # Only poll controller startup rapidly; actual /proc reads are
                # one second apart once a target is known, identically A/B/C.
                deadline += 1.0 if pid is not None else .01
                done.wait(max(0, deadline - time.monotonic()))

    def execute(self, path, argv, env, monitored=True, timeout=1200):
        record = dict(argv=[str(x) for x in argv], environment=env, started_at=now(), timeout_seconds=timeout)
        save(path / 'command.json', record)
        self.state('running', run=str(path.relative_to(self.base)), argv=record['argv'])
        done = threading.Event()
        self.done = done
        def watchdog():
            if done.wait(timeout):
                return
            record['timed_out'] = True
            try:
                self.active.send_signal(signal.SIGTERM)
                if not done.wait(45):
                    os.killpg(self.active.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout_name = 'monitor.jsonl' if monitored else 'stdout.log'
        with (path / stdout_name).open('w') as out, (path / 'target.log').open('w') as err:
            begin = ns()
            p = subprocess.Popen(record['argv'], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                env=dict(os.environ, **env), start_new_session=True)
            self.active = p
            watch = threading.Thread(target=watchdog, daemon=True)
            observer = threading.Thread(target=self.observe, args=(path, done), daemon=True) if monitored else None
            watch.start()
            if observer:
                observer.start()
            # No timeout here: Python's timeout wait polls and biases fast exits.
            code = p.wait()
            end = ns()
            done.set()
            watch.join()
            if observer:
                observer.join()
        self.active = None
        record.update(exit=code, launcher_begin_ns=begin, launcher_end_ns=end,
                      elapsed_seconds=(end - begin) / 1e9, ended_at=now(), controller_pid=p.pid)
        save(path / 'command.json', record)
        (path / 'controller.exit').write_text(str(code) + '\n')
        assert code == 0 and not record.get('timed_out'), record
        return record

    def receipt(self, path):
        if not path.exists():
            path.mkdir(parents=True)
            return None
        assert self.a.resume, 'Existing run; use --resume only for verified receipts: ' + str(path)
        result = json.loads((path / 'summary.json').read_text())
        assert result['pass'] and result['source_commit'] == self.commit
        assert result['boot_id'] == self.before['boot_id']
        for name, digest in result['evidence_sha256'].items():
            assert sha(path / name) == digest, name
        self.summaries[str(path.relative_to(self.base))] = result
        return result

    def finish(self, path, result):
        self.guard(path / 'host-after.json')
        result.update(pass_=True)
        result['pass'] = result.pop('pass_')
        result.update(source_commit=self.commit, boot_id=self.before['boot_id'], at=now())
        result['evidence_sha256'] = {p.name: sha(p) for p in sorted(path.iterdir()) if p.is_file()}
        save(path / 'summary.json', result)
        self.summaries[str(path.relative_to(self.base))] = result
        self.state('passed', run=str(path.relative_to(self.base)), elapsed_seconds=result.get('elapsed_seconds'))
        return result

    def warm(self, path):
        begin = ns()
        size = 0
        with self.graph.open('rb') as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
                size += len(chunk)
        assert size == self.audit['graph_bytes']
        save(path / 'warm-cache.json', dict(bytes=size, begin_ns=begin, end_ns=ns(), outside_primary_timer=True))

    def controller_args(self, mode, generated=False):
        if mode == 'A':
            return [self.monitor, '--monitor-off']
        return [self.monitor, '--dirty-observe' if mode == 'B' else '--dirty-qualify',
                '--quiet-ms', '10' if generated else '10000', '--period-ms', '100' if generated else '10000',
                '--poll-ms', '1000', '--max-pages', '262144' if generated else '8388608',
                '--max-history', '524288' if generated else '8388608']

    def validate_controller(self, path, mode, before, require_arm, minimum=1):
        records = rows(path / 'monitor.jsonl')
        controllers = [r for r in records if r.get('event') == 'controller']
        exits = [r for r in records if r.get('event') == 'target_exit']
        ends = [r for r in records if r.get('event') == 'controller_exit']
        assert len(controllers) == len(exits) == len(ends) == 1
        c, e, end = controllers[0], exits[0], ends[0]
        assert c['launched'] and c['thp_disabled_before_exec'] and c['monitor_enabled'] == (mode != 'A')
        assert e['exit_code'] == end['exit_code'] == e['wait4_errno'] == 0
        assert c['controller_start_ns'] <= c['launch_ns'] <= c['exec_ack_ns'] <= e['exit_observed_ns'] <= end['now_ns']
        if mode == 'A':
            assert [r['event'] for r in records] == ['controller', 'target_exit', 'controller_exit']
            assert old.stats()['arms'] == before['stats']['arms'], 'Baseline caused ARM activity'
            result = dict(pass_=True, completed_sweeps=0, final={}, dirty={}, progress={}, diagnostics={})
            result['pass'] = result.pop('pass_')
        else:
            result = validator.validate(path, minimum=minimum, require_admissions=require_arm)
            save(path / 'validation.json', result)
            assert result['pass'], result['errors']
            dirty, final = result['dirty'], result['final']
            assert dirty['mode'] == (1 if mode == 'B' else 2)
            assert dirty['resource_skips'] == dirty['valid_observations'] == 0
            assert dirty['samples'] > 0 and dirty['metadata_bytes'] > 0
            assert dirty['passes_started'] == final['passes_started']
            assert dirty['passes_completed'] == final['passes_completed']
            assert dirty['metadata_bytes'] == final['history_entries'] * HISTORY_BYTES_PER_PAGE
            assert len([r for r in records if r['event'] == 'stop_request']) == 1
            assert len([r for r in records if r['event'] == 'stop_return']) == 1
            if mode == 'B':
                assert old.stats()['arms'] == before['stats']['arms']
        save(path / 'validation.json', result)
        return result, dict(child_lifetime_seconds=(e['exit_observed_ns'] - c['launch_ns']) / 1e9,
             target_rusage=e, controller_tail_seconds=(end['now_ns'] - e['exit_observed_ns']) / 1e9)

    def check_workers(self, path, binary, threads, sustained):
        samples = rows(path / 'memory-samples.jsonl')
        valid = [r for r in samples if not r.get('error') and r['exe'] == str(binary)]
        assert valid and all(r['thp_enabled'] == '0' for r in valid)
        assert len(set(r['start_ticks'] for r in valid)) == 1
        matching = [r for r in valid if len(r['threads']) == threads]
        def bound(r):
            return all(len(t['cpu_allowed']) == 1 for t in r['threads']) and {
                t['cpu_allowed'][0] for t in r['threads']} == set(range(threads))
        bound_rows = [r for r in matching if bound(r)]
        if sustained:
            assert bound_rows, 'Expected OpenMP workers/binding never observed'
            first = matching.index(bound_rows[0])
            assert all(bound(r) for r in matching[first:])
            active = set()
            for x, y in zip(bound_rows, bound_rows[1:]):
                previous = {t['tid']: t['utime_ticks'] + t['stime_ticks'] for t in x['threads']}
                for t in y['threads']:
                    if t['tid'] in previous and t['utime_ticks'] + t['stime_ticks'] > previous[t['tid']]:
                        active.add(t['tid'])
            assert len(active) == threads, ('Active workers', active, threads)
        return dict(samples=len(samples), valid_samples=len(valid), bound_samples=len(bound_rows),
                    sample_errors=sum(bool(r.get('error')) for r in samples),
                    sampled_peak_rss_kib=max(r['memory_kib'].get('VmRSS', 0) for r in valid))

    def workload(self, name, app, threads, mode, count, generated=False, verify=False, sustained=False):
        path = self.base / name
        cached = self.receipt(path)
        if cached:
            return cached
        before = self.guard(path / 'host-before.json')
        if not generated:
            self.warm(path)
        argv = ['taskset', '-c', ','.join(str(i) for i in range(threads))] + self.controller_args(mode, generated)
        argv += ['--', self.a.gapbs / app, '-g', '18'] if generated else ['--', self.a.gapbs / app, '-f', self.graph]
        argv += ['-n', str(count), '-l']
        if app == 'pr':
            argv += ['-i', '1000' if verify else '21', '-t', '1e-4' if verify else '0']
        if app == 'bc':
            argv += ['-i', '1']
        if verify:
            argv += ['-v']
        env = dict(OMP_NUM_THREADS=str(threads), OMP_DYNAMIC='FALSE', OMP_PROC_BIND='close',
                   OMP_PLACES=','.join('{' + str(i) + '}' for i in range(threads)))
        command = self.execute(path, argv, env, timeout=300 if generated else 1200)
        v, timing = self.validate_controller(path, mode, before, mode == 'C' and sustained, minimum=0 if generated else 1)
        if mode != 'A':
            assert v['dirty']['quiet_ms'] == (10 if generated else 10000)
            assert v['diagnostics'], 'Missing invalidation/rejection diagnostics'
        parsed = parse_log((path / 'target.log').read_text(), app, count, verify)
        workers = self.check_workers(path, self.a.gapbs / app, threads, sustained=not generated)
        result = dict(application=app, threads=threads, mode=mode, trials=count,
                      generated=generated, verify=verify, elapsed_seconds=command['elapsed_seconds'],
                      workers=workers, completed_sweeps=v['completed_sweeps'],
                      dirty=v['dirty'], final=v['final'], progress=v['progress'], diagnostics=v['diagnostics'])
        result.update(parsed)
        result.update(timing)
        return self.finish(path, result)

    def tap(self, name, program, count, extra=()):
        path = self.base / 'checks' / name
        if self.receipt(path):
            return
        self.guard(path / 'host-before.json')
        command = self.execute(path, [self.fixture_bin / program] + list(extra), {}, monitored=False, timeout=300)
        text = (path / 'stdout.log').read_text()
        assert re.findall(r'^ok\s+(\d+)\b', text, re.M) == [str(i) for i in range(1, count + 1)]
        assert re.findall(r'^1\.\.(\d+)\s*$', text, re.M) == [str(count)]
        assert not re.search(r'^not ok|# SKIP', text, re.M), text
        self.finish(path, dict(checks=count, elapsed_seconds=command['elapsed_seconds']))

    def gates(self):
        for name, program, count, extra in [('legacy', 'cmdp_test', 18, ()),
            ('monitor', 'cmdp_monitor_test', 59, ()), ('dirty', 'cmdp_dirty_test', 96, ()),
            ('recovery-t1', 'cmdp_recovery_test', 62, ('--threads', '1')),
            ('recovery-t4', 'cmdp_recovery_test', 86, ('--threads', '4'))]:
            self.tap(name, program, count, extra)
        for mode in 'ABC':
            path = self.base / 'checks' / ('controller-' + mode)
            if self.receipt(path):
                continue
            before = self.guard(path / 'host-before.json')
            command = self.execute(path, self.controller_args(mode, True) + ['--', self.fixture_bin / 'cmdp_fixture',
                                  '--mode', 'hotcold', '--seconds', '4'], {})
            v, timing = self.validate_controller(path, mode, before, require_arm=mode == 'C')
            result = dict(mode=mode, elapsed_seconds=command['elapsed_seconds'], validation=v)
            result.update(timing)
            self.finish(path, result)
        for app, threads in CELLS:
            for mode in 'ABC':
                self.workload('generated/{}-t{}-{}'.format(app, threads, mode), app, threads, mode, 8,
                              generated=True, verify=True)
        # Full-input verification is separate from timing. All modes have just
        # passed generated correctness with this launcher; dirty-qualify on the
        # full graph was also verified on this exact boot in prior campaigns.
        for app, threads in CELLS:
            self.workload('correctness/{}-t{}-A'.format(app, threads), app, threads, 'A', 1, verify=True)

    def freeze(self):
        pilots, work = {}, {}
        counts = {'bfs': {1: 8, 4: 16}, 'pr': {1: 1, 4: 1}, 'cc': {1: 4, 4: 8}, 'bc': {1: 1, 4: 2}}
        for app, threads in CELLS:
            cell = '{}-t{}'.format(app, threads)
            pilot = self.workload('pilots/' + cell + '-A', app, threads, 'A', counts[app][threads])
            mean = pilot['trial_seconds'] / pilot['trials']
            nontrial = max(0, pilot['elapsed_seconds'] - pilot['trial_seconds'])
            n = max(1, int(math.floor((120 - nontrial) / mean + .5)))
            pilots[cell] = pilot
            work[cell] = dict(application=app, threads=threads, trials=n, pilot_mean_trial_s=mean,
                pilot_nontrial_s=nontrial, predicted_total_s=nontrial + mean * n,
                pilot_sources=pilot['sources'], pilot_trials=pilot['trials'])
        rng = random.Random(SEED)
        orders = {}
        for cell in sorted(work):
            remaining = list(itertools.permutations('ABC'))[1:]
            rng.shuffle(remaining)
            orders[cell] = ['ABC'] + [''.join(x) for x in remaining]
        schedule = []
        for block in range(6):
            cells = sorted(work)
            rng.shuffle(cells)
            for cell in cells:
                for mode in orders[cell][block]:
                    schedule.append(dict(block=block + 1, cell=cell, mode=mode))
        frozen = dict(seed=SEED, target_seconds=120, whole_trials=True, cells=work, schedule=schedule,
                      counts_frozen_before_measurement=True, first_block_ABC_for_source_reference=True,
                      period_ms=10000, quiet_ms=10000, poll_ms=1000, max_pages=8388608, max_history=8388608)
        path = self.base / 'frozen-work.json'
        if path.exists():
            assert json.loads(path.read_text()) == frozen
        else:
            save(path, frozen)
        return frozen

    def run(self):
        self.preflight()
        self.gates()
        frozen = self.freeze()
        work = frozen['cells']['bfs-t1']
        for mode in 'ABC':
            self.workload('smoke/bfs-t1-' + mode, 'bfs', 1, mode, work['trials'], sustained=True)
        reference = {}
        for index, entry in enumerate(frozen['schedule']):
            cell, mode, block = entry['cell'], entry['mode'], entry['block']
            work = frozen['cells'][cell]
            name = 'measured/b{:02d}-{}-{}'.format(block, cell, mode)
            result = self.workload(name, work['application'], work['threads'], mode, work['trials'], sustained=True)
            if cell not in reference:
                assert mode == 'A' and block == 1
                reference[cell] = result['sources']
                prefix = work['pilot_sources'][:min(work['trials'], work['pilot_trials'])]
                assert result['sources'][:len(prefix)] == prefix
            assert result['sources'] == reference[cell], 'Source sequence differs across matched runs'
            self.state('measured-progress', measured_completed=index + 1, measured_total=len(frozen['schedule']),
                       last_run=name, elapsed_seconds=result['elapsed_seconds'])
        self.guard(self.base / 'host-final.json')
        summary = dict(at=now(), source_commit=self.commit,
                       measured_runs=len(frozen['schedule']), runs=self.summaries)
        summary['pass'] = True
        save(self.base / 'campaign-summary.json', summary)
        self.state('complete', passed=True, measured_completed=len(frozen['schedule']))
        (self.base / 'campaign.exit').write_text('0\n')

    def failure(self, exc):
        p = self.active
        if p is not None and p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
        if self.done:
            self.done.set()
        save(self.base / 'failure-{}.json'.format(int(time.time())), dict(at=now(), error=str(exc), traceback=traceback.format_exc()))
        self.state('failed', error=str(exc))
        (self.base / 'campaign.exit').write_text('1\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-rearm-recovery'))
    parser.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    parser.add_argument('--prior', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/recovery-ccbc-20261022-v2'))
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    assert args.base.is_dir() and sys.version_info >= (3, 6)
    with (args.base / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(signum, frame):
            raise RuntimeError('Driver interrupted by signal ' + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        campaign = Campaign(args)
        try:
            campaign.run()
        except BaseException as exc:
            campaign.failure(exc)
            raise


if __name__ == '__main__':
    main()
