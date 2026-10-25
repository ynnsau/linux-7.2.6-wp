#!/usr/bin/env python3
"""Fixed BFS CPUs, scanner CPU1 versus CPU0, P0/P1, three matched repeats.

Retain the original complete-job timing executor and per-run host guards.
No kernel, host policy, shared-worker affinity, GRUB or boot changes.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal

SCRIPTS = Path(__file__).resolve().parent
s = importlib.util.spec_from_file_location('bound_placement', str(SCRIPTS / 'run-bfs-placement.py'))
placement = importlib.util.module_from_spec(s); s.loader.exec_module(placement)
profile, matched, fused = placement.profile, placement.matched, placement.fused
BUILD = Path('/home/yans3/cmdp-v2/periodic/scanner-cpu-20261025')
matched.prepare.RELEASE = '7.2.6-cmdp-cpu-production'
fused.BUILD_RECORD = BUILD / 'records/build-verification.json'
profile.VMLINUX = str(BUILD / 'build-production/vmlinux')

def expand_schedule(schedule):
    result = []
    for i, entry in enumerate(schedule):
        order = ('shared', 'separate') if i % 2 == 0 else ('separate', 'shared')
        result.extend(dict(entry, placement=p) for p in order)
    return result

class Campaign(placement.Campaign):
    def cpus(self, threads):
        return list(range(1, threads + 1))

    def scanner_cpu(self):
        return 1 if self.placement == 'shared' else 0

    def preflight(self):
        profile.Campaign.preflight(self)
        wanted = set(range(6)) | {24, 25}
        assert wanted <= os.sched_getaffinity(0)
        topology = {}
        for cpu in sorted(wanted):
            p = Path('/sys/devices/system/cpu/cpu' + str(cpu))
            topology[cpu] = {name: (p / 'topology' / name).read_text().strip()
                for name in ('physical_package_id', 'core_id', 'thread_siblings_list')}
            assert (p / 'node0').exists()
            assert topology[cpu]['thread_siblings_list'] == str(cpu)
        assert len({(v['physical_package_id'], v['core_id']) for v in topology.values()}) == len(wanted)
        actual = dict(self.plan, schedule=expand_schedule(self.plan['schedule']),
            measured_runs=36 * len(self.a.stages), controller_cpus=[5], observer_cpus=[5],
            application_cpus={'1': [1], '4': [1, 2, 3, 4]},
            placements={'shared': {'scanner_cpu': 1}, 'separate': {'scanner_cpu': 0}},
            topology=topology, scanner_policy='explicit per-session CPU; every worker entry validated; P1 independent trace',
            pair_order='alternate shared/separate first; adjacent same-work pairs',
            recipe='profile-plan.json is base recipe; scanner-cpu-plan.json governs execution',
            pilot_excluded=True, new_kernel_activated_by_user=True)
        target = self.base / 'scanner-cpu-plan.json'
        if target.exists(): assert json.loads(target.read_text()) == json.loads(json.dumps(actual))
        else: matched.save(target, actual)
        self.plan = actual
        names = ('prepare-bfs-scanner-cpu.py', 'run-bfs-scanner-cpu.py', 'run-bfs-placement.py',
                 'audit-perf-data.py', 'analyze-bfs-overhead-profile.py')
        for name in names:
            src, dest = SCRIPTS / name, self.base / 'source' / name
            if dest.exists(): assert matched.sha(src) == matched.sha(dest)
            else: shutil.copyfile(str(src), str(dest))
        src = self.a.source / 'wp_doc/bfs-scanner-cpu-protocol.md'
        dest = self.base / 'source' / src.name
        if dest.exists(): assert matched.sha(src) == matched.sha(dest)
        else: shutil.copyfile(str(src), str(dest))
        for name in ('quality', 'contexts'):
            (self.base / name).mkdir(exist_ok=True)

    def controller_args(self, mode, generated=False):
        argv = super().controller_args(mode, generated)
        if mode != 'A': argv += ['--scanner-cpu', str(self.scanner_cpu())]
        return argv

    def execute(self, path, argv, env, monitored=True, timeout=1200):
        # The driver's RSS observer/watchdog inherit CPU5 too. Restore the
        # broad mask between runs for topology preflight and routine guards.
        previous = os.sched_getaffinity(0)
        os.sched_setaffinity(0, {5})
        try:
            return super().execute(path, argv, env, monitored, timeout)
        finally:
            os.sched_setaffinity(0, previous)

    def validate_controller(self, path, mode, before, require_arm, minimum=1):
        v, timing = super().validate_controller(path, mode, before, require_arm, minimum)
        records = [r for r in matched.rows(path / 'monitor.jsonl') if r['event'] == 'cpu_status']
        if mode == 'A':
            assert not records
        else:
            assert records and records[-1]['work_items'] > 0
            for r in records:
                assert r['cpu'] == self.scanner_cpu()
                assert r['error'] == r['offline_events'] == r['wrong_cpu_work_items'] == 0
                if r['work_items']: assert r['last_cpu'] == self.scanner_cpu()
            v['cpu'] = records[-1]
        matched.save(path / 'validation.json', v)
        return v, timing

    def workload(self, name, app, threads, mode, count, generated=False, verify=False, sustained=False):
        assert app == 'bfs'
        path = self.base / name
        cached = self.receipt(path)
        if cached: return cached
        before = self.guard(path / 'host-before.json')
        if not generated: self.warm(path)
        cpus = self.cpus(threads)
        argv = ['taskset', '-c', '5'] + self.controller_args(mode, generated)
        argv += ['--target-cpus', ','.join(map(str, cpus)), '--', self.a.gapbs / app]
        argv += ['-g', '18'] if generated else ['-f', self.graph]
        argv += ['-n', str(count), '-l']
        if verify: argv += ['-v']
        env = dict(OMP_NUM_THREADS=str(threads), OMP_DYNAMIC='FALSE', OMP_PROC_BIND='close',
                   OMP_PLACES=','.join('{' + str(i) + '}' for i in cpus))
        command = self.execute(path, argv, env, timeout=300 if generated else 1200)
        v, timing = self.validate_controller(path, mode, before, mode == 'C' and sustained, minimum=0 if generated else 1)
        if mode != 'A':
            assert v['dirty']['quiet_ms'] == (10 if generated else 10000) and v['diagnostics']
        parsed = matched.parse_log((path / 'target.log').read_text(), app, count, verify)
        workers = self.check_workers(path, self.a.gapbs / app, threads, sustained=not generated)
        result = dict(application=app, threads=threads, mode=mode, trials=count, placement=self.placement,
            application_cpus=cpus, scanner_cpu=None if mode == 'A' else self.scanner_cpu(),
            controller_cpus=[5], generated=generated, verify=verify,
            elapsed_seconds=command['elapsed_seconds'], workers=workers, completed_sweeps=v['completed_sweeps'],
            dirty=v['dirty'], final=v['final'], progress=v['progress'], diagnostics=v['diagnostics'],
            cpu=v.get('cpu', {}), batch=v.get('batch', {}))
        result.update(parsed); result.update(timing)
        return self.finish(path, result)

    def check_trace(self, path):
        super().check_trace(path)
        target = self.base / 'contexts' / (path.parent.name + '-' + path.name + '.json')
        d = json.loads(target.read_text())
        cpus = {int(k) for k, v in d['scanner_cpu_distribution_s'].items() if v > 0}
        summary = json.loads((path / 'summary.json').read_text())
        if d['mode'] == 'A':
            assert not cpus and not d['scanner_work_items']
        else:
            assert cpus == {self.scanner_cpu()}, (cpus, self.scanner_cpu())
            assert d['scanner_work_items'] == summary['cpu']['work_items']
            assert d['separation_verified'] == (self.placement == 'separate')
        d['requested_scanner_cpu'] = summary['scanner_cpu']
        d['explicit_placement_verified'] = True
        matched.save(target, d)

    def pilot(self):
        self.preflight()
        self.profile_stage = 'P0'
        for threads in (1, 4):
            for policy in ('shared', 'separate'):
                self.placement = policy
                for mode in 'ABC':
                    self.workload('pilots/P0/generated-t{}-{}-{}'.format(threads, mode, policy),
                                  'bfs', threads, mode, 8, generated=True, verify=True)
        self.profile_stage = 'P1'
        for threads, mode, policy in ((1, 'B', 'shared'), (4, 'C', 'separate')):
            self.placement = policy
            name = 'pilots/P1/twitter-t{}-{}-{}'.format(threads, mode, policy)
            work = self.plan['cells']['bfs-t' + str(threads)]
            r = self.workload(name, 'bfs', threads, mode, work['trials'], sustained=True)
            assert r['sources'][:work['pilot_trials']] == work['pilot_sources']
            self.check_trace(self.base / name)
        self.guard(self.base / 'pilot-host-final.json')
        matched.save(self.base / 'pilot-summary.json', dict(passed=True, runs=self.summaries,
            source_commit=self.commit, boot_id=self.before['boot_id']))
        self.state('pilot-complete', passed=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--reference', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    p.add_argument('--stages', nargs='+', choices=('P0', 'P1'), default=['P0', 'P1'])
    p.add_argument('--phase', choices=('pilot', 'measured'), default='measured')
    p.add_argument('--resume', action='store_true')
    a = p.parse_args()
    assert a.stages == ['P0', 'P1'], 'Keep complete distinct timing and scheduling blocks'
    a.prior = a.base
    a.dirty_batch_pages = a.wp_batch_pages = 512
    a.unprofiled_reference = a.scheduling_reference = a.cpu_reference = None
    record = json.loads(fused.BUILD_RECORD.read_text())
    assert record['pass'] and record['release'] == matched.prepare.RELEASE
    matched.KERNEL_COMMIT = record['source_freezes']['fixed']['commit']
    with (a.base / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(signum, frame): raise RuntimeError('Driver interrupted: ' + str(signum))
        signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
        campaign = Campaign(a)
        try:
            if a.phase == 'pilot': campaign.pilot()
            else:
                pilot = json.loads((a.base / 'pilot-summary.json').read_text())
                assert pilot['passed'] and pilot['source_commit'] == campaign.commit
                assert pilot['boot_id'] == campaign.before['boot_id']
                campaign.run()
        except BaseException as exc:
            for _, proc, log in campaign.collectors:
                if proc.poll() is None: proc.send_signal(signal.SIGINT)
                proc.wait(timeout=30); log.close()
            campaign.failure(exc)
            raise

if __name__ == '__main__':
    main()
