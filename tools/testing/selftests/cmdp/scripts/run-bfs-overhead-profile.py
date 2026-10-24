#!/usr/bin/env python3
"""P0--P3 profiling without changing the matched timing executor or kernel.

Use the verified prior tools; freeze 38/127 BFS trials, 512/512 page caps,
three rotated repetitions per block. Collector startup/stop is outside the
unchanged complete-job timer. Python 3.6 compatible. No host policy changes.
"""
import argparse
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('profile_fused', str(SCRIPTS / 'run-fused-overhead.py'))
fused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fused)
matched = fused.matched
PERF = '/usr/lib/linux-tools/5.4.0-150-generic/perf'
VMLINUX = '/home/yans3/cmdp-v2/periodic/fused-scanner-20261022/build-production/vmlinux'
PMU = '{cycles,instructions,cache-misses,dtlb_load_misses.walk_completed,dtlb_store_misses.walk_completed}'
STAGES = ('P0', 'P1', 'P2', 'P3')


def collector_commands(stage, path, scanner):
    context = []
    for event in ('workqueue_execute_start', 'workqueue_execute_end'):
        context += ['-e', 'workqueue:' + event, '--filter', 'function == ' + scanner]
    context += ['-e', 'cmdp:cmdp_invalidation']
    common = ['taskset', '-c', '24,25', PERF, 'record', '-a', '--clockid', 'mono', '-m', '1024', '-o', str(path / 'perf.data')]
    if stage == 'P0':
        return []
    if stage == 'P1':
        args = ['-e', 'sched:sched_switch/call-graph=fp/', '-e', 'sched:sched_wakeup',
                '-e', 'sched:sched_wakeup_new', '-e', 'mmap_lock:*', '--filter', 'write == 1'] + context
        return [('record', common + args)]
    if stage == 'P2':
        # Hardware PMU sampling uses NMIs on this x86 host, avoiding timer
        # deferral through IRQ-disabled sections. Weights are cycles, not time.
        return [('record', common + ['-e', 'cycles/freq=99,call-graph=fp/'] + context)]
    assert stage == 'P3'
    return [('record', common + context + ['-e', 'tlb:tlb_flush', '--filter', 'reason != 0 || pages != 0']),
            ('stat', ['taskset', '-c', '24,25', PERF, 'stat', '-a', '-A', '-I', '1000',
                      '-x,', '-e', PMU, '-o', str(path / 'pmu.csv')])]


class Campaign(fused.Campaign):
    def __init__(self, a):
        self.profile_stage = a.stages[0]
        self.collectors = []
        self.collector_records = []
        super().__init__(a)

    def state(self, stage, **extra):
        extra.update(profile_stage=self.profile_stage,
                     measured_completed=sum(k.startswith('measured/') for k in self.summaries),
                     measured_total=18 * len(self.a.stages))
        super().state(stage, **extra)

    def preflight(self):
        matched.Campaign.preflight(self)
        assert matched.sha(self.a.reference / 'frozen-work.json') == fused.FROZEN_SHA256
        reference = json.loads((self.a.reference / 'provenance.json').read_text())
        current = json.loads((self.base / 'provenance.json').read_text())
        for key in ('graph_sha256', 'gapbs_commit'):
            assert current[key] == reference[key]
        assert current['binary_sha256']['bfs'] == reference['binary_sha256']['bfs']
        previous = json.loads((self.a.prior / 'host-initial.json').read_text())
        for key in ('numa_balancing', 'taint', 'settings', 'governors'):
            assert self.before[key] == previous[key], key
        symbols = Path('/proc/kallsyms').read_text()
        matches = re.findall(r'^([0-9a-f]+) [tT] cmdp_monitor_work$', symbols, re.M)
        assert len(matches) == 1 and int(matches[0], 16)
        self.scanner = '0x' + matches[0]
        for p in [Path(__file__), SCRIPTS / 'run-fused-overhead.py']:
            dest = self.base / 'source' / p.name
            if dest.exists():
                assert matched.sha(dest) == matched.sha(p)
            else:
                shutil.copyfile(str(p), str(dest))
        frozen = fused.plan(json.loads((self.a.reference / 'frozen-work.json').read_text()), 512, 512)
        frozen.update(stages=list(self.a.stages), measured_runs=18 * len(self.a.stages), collection_cpu_affinity=[24,25],
            mmap_capture='write-lock events only; read-side blocking from scheduler stacks',
            perf_clock='CLOCK_MONOTONIC; native lifecycle boundaries', cpu_sample_event='cycles', cpu_sample_frequency_hz=99, cpu_sample_weight_unit='cycles', collector_ready='all expected event FDs plus data mmap setup and 1s settle',
            perf=PERF, perf_sha256=matched.sha(Path(PERF)), vmlinux=VMLINUX,
            vmlinux_sha256=matched.sha(Path(VMLINUX)), scanner_symbol=self.scanner,
            exact_target_timer_unchanged=True, reused_tools_from=str(self.a.prior),
            pmu_scope='system-wide per CPU, 1s intervals; includes scanner and background activity',
            pmu_group=PMU, tlb_filter='reason != 0 || pages != 0', policy_changes=False,
            startup_shutdown_outside_timer=True, no_kernel_changes=True)
        self.reference_sources = {}
        if self.a.unprofiled_reference:
            ref = self.a.unprofiled_reference
            original = json.loads((ref / 'provenance.json').read_text())
            for key in ('kernel_commit', 'running_build_id', 'graph_sha256', 'gapbs_commit', 'binary_sha256', 'monitor_sha256'):
                assert original[key] == current[key], key
            refs = sorted((ref / 'measured/P0').glob('*/summary.json'))
            assert len(refs) == 18
            for receipt in refs:
                result = json.loads(receipt.read_text())
                assert result['pass'] and result['boot_id'] == self.before['boot_id']
                for name, digest in result['evidence_sha256'].items():
                    assert matched.sha(receipt.parent / name) == digest
            for threads in (1, 4):
                receipt = ref / ('measured/P0/b01-bfs-t{}-A/summary.json'.format(threads))
                self.reference_sources['bfs-t' + str(threads)] = json.loads(receipt.read_text())['sources']
            frozen['unprofiled_reference'] = str(ref)
            frozen['unprofiled_receipt_sha256'] = {str(p.relative_to(ref)): matched.sha(p) for p in refs}
        if self.a.scheduling_reference:
            ref = self.a.scheduling_reference
            original = json.loads((ref / 'provenance.json').read_text())
            for key in ('kernel_commit', 'running_build_id', 'graph_sha256', 'gapbs_commit', 'binary_sha256', 'monitor_sha256'):
                assert original[key] == current[key], key
            refs = sorted((ref / 'measured/P1').glob('*/summary.json'))
            assert len(refs) == 18
            for receipt in refs:
                result = json.loads(receipt.read_text())
                assert result['pass'] and result['boot_id'] == self.before['boot_id']
                assert result['sources'] == self.reference_sources['bfs-t' + str(result['threads'])]
                for name, digest in result['evidence_sha256'].items():
                    assert matched.sha(receipt.parent / name) == digest
                quality = json.loads((ref / 'quality' / ('P1-' + receipt.parent.name + '.json')).read_text())
                assert quality['pass_']
            frozen['scheduling_reference'] = str(ref)
            frozen['scheduling_receipt_sha256'] = {str(p.relative_to(ref)): matched.sha(p) for p in refs}
        if self.a.cpu_reference:
            ref = self.a.cpu_reference
            original = json.loads((ref / 'provenance.json').read_text())
            assert json.loads((ref / 'profile-plan.json').read_text())['cpu_sample_event'] == 'cycles'
            for key in ('kernel_commit', 'running_build_id', 'graph_sha256', 'gapbs_commit', 'binary_sha256', 'monitor_sha256'):
                assert original[key] == current[key], key
            refs = sorted((ref / 'measured/P2').glob('*/summary.json'))
            assert len(refs) == 18
            for receipt in refs:
                result = json.loads(receipt.read_text())
                assert result['pass'] and result['boot_id'] == self.before['boot_id']
                assert result['sources'] == self.reference_sources['bfs-t' + str(result['threads'])]
                for name, digest in result['evidence_sha256'].items():
                    assert matched.sha(receipt.parent / name) == digest
                quality = json.loads((ref / 'quality' / ('P2-' + receipt.parent.name + '.json')).read_text())
                assert quality['pass_']
            frozen['cpu_reference'] = str(ref)
            frozen['cpu_receipt_sha256'] = {str(p.relative_to(ref)): matched.sha(p) for p in refs}
        target = self.base / 'profile-plan.json'
        if target.exists():
            assert json.loads(target.read_text()) == frozen
        else:
            matched.save(target, frozen)
        self.plan = frozen

    def start_collectors(self, path):
        self.collectors = []
        self.collector_records = []
        online = Path('/sys/devices/system/cpu/online').read_text().strip().split(',')
        online_count = sum(int(x.split('-')[-1])-int(x.split('-')[0])+1 for x in online)
        for name, argv in collector_commands(self.profile_stage, path, self.scanner):
            log = (path / (name + '.log')).open('w')
            start = matched.ns()
            p = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True, env=dict(os.environ, PAGER='cat', PERF_PAGER='cat'))
            self.collectors.append((name, p, log))
            self.collector_records.append(dict(name=name, argv=argv, pid=p.pid, start_ns=start))
        # Before the original launcher timer. Check that recording has opened
        # the events, rather than racing startup against the BFS process.
        if self.collectors:
            deadline = time.monotonic() + 20
            while True:
                assert all(p.poll() is None for _, p, _ in self.collectors), 'Collector failed at startup'
                ready = True
                for collector_name, p, collector_log in self.collectors:
                    fds = list((Path('/proc') / str(p.pid) / 'fd').iterdir())
                    targets = []
                    for fd in fds:
                        try:
                            targets.append(os.readlink(str(fd)))
                        except FileNotFoundError:
                            pass  # perf closes transient files during startup
                    expected_events = (9 if self.profile_stage == 'P1' else 4) if collector_name == 'record' else 5
                    expected_fds = expected_events * online_count
                    count_fds = sum('perf_event' in target for target in targets)
                    ready &= count_fds >= expected_fds
                    if collector_name == 'record':
                        data_path = path / 'perf.data'
                        ready &= data_path.exists() and data_path.stat().st_size > 0
                    for record in self.collector_records:
                        if record['name'] == collector_name:
                            record.update(expected_perf_event_fds=expected_fds, observed_perf_event_fds=count_fds)
                if ready:
                    break
                assert time.monotonic() < deadline, 'Collector perf-event startup timed out'
                time.sleep(.02)
            time.sleep(1.0)
            for record in self.collector_records:
                record['ready_ns'] = matched.ns()
        matched.save(path / 'collectors.json', self.collector_records)

    def stop_collectors(self, path):
        errors = []
        for record, (name, p, log) in zip(self.collector_records, self.collectors):
            record['stop_requested_ns'] = matched.ns()
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
            try:
                code = p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill(); code = p.wait(); errors.append(name + ': stop timed out')
            log.close()
            record.update(exit_code=code, stopped_ns=matched.ns())
            if code not in (0, 130, -signal.SIGINT):
                errors.append(name + ': exit ' + str(code))
            content = (path / (name + '.log')).read_text()
            if re.search(r'\b(?:LOST|lost|throttl|failed|Error|error:)\b', content):
                errors.append(name + ': inspect collector log')
        self.collectors = []
        matched.save(path / 'collectors.json', self.collector_records)
        assert not errors, errors

    def execute(self, path, argv, env, monitored=True, timeout=1200):
        self.start_collectors(path)
        try:
            command = super().execute(path, argv, env, monitored, timeout)
        finally:
            self.stop_collectors(path)
        # Decode separately after the complete-job timer; preserve raw perf.data.
        if self.profile_stage != 'P0':
            argv = [PERF, 'script', '--ns', '-i', str(path / 'perf.data'), '-k', VMLINUX]
            with (path / 'decode.log').open('w') as err, gzip.open(str(path / 'perf-script.txt.gz'), 'wb') as out:
                p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err)
                for line in iter(p.stdout.readline, b''):
                    out.write(line)
                assert p.wait() == 0, 'perf script failed'
            text = (path / 'decode.log').read_text()
            assert not re.search(r'\blost\b|\bLOST\b|\bError\b', text), text
            if self.profile_stage == 'P3':
                text = (path / 'pmu.csv').read_text()
                assert '<not counted>' not in text and '<not supported>' not in text
                percentages = []
                for line in text.splitlines():
                    if line.startswith('#') or not line.strip():
                        continue
                    fields = line.split(',')
                    if len(fields) >= 7:
                        percentages.append(float(fields[6]))
                assert percentages and min(percentages) >= 95, ('PMU multiplexing', min(percentages or [0]))
        return command

    def finish(self, path, result):
        result.update(profile_stage=self.profile_stage, dirty_batch_pages=512, wp_batch_pages=512)
        return super().finish(path, result)

    def run(self):
        self.preflight()
        reference = dict(self.reference_sources)
        for stage in self.a.stages:
            self.profile_stage = stage
            for entry in self.plan['schedule']:
                cell, mode, block = entry['cell'], entry['mode'], entry['block']
                work = self.plan['cells'][cell]
                name = 'measured/{}/b{:02d}-{}-{}'.format(stage, block, cell, mode)
                result = self.workload(name, 'bfs', work['threads'], mode, work['trials'], sustained=True)
                if cell not in reference:
                    assert mode == 'A' and block == 1
                    reference[cell] = result['sources']
                    assert result['sources'][:work['pilot_trials']] == work['pilot_sources']
                assert result['sources'] == reference[cell]
            self.state('block-complete', block=stage)
        self.guard(self.base / 'host-final.json')
        matched.save(self.base / 'campaign-summary.json', dict(passed=True, at=matched.now(),
            source_commit=self.commit, stages=self.a.stages, measured_runs=len(self.summaries), runs=self.summaries))
        self.state('complete', passed=True)
        (self.base / 'campaign.exit').write_text('0\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    parser.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    parser.add_argument('--prior', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/fused-bfs-batch-sweep-20261023-v1'))
    parser.add_argument('--reference', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    parser.add_argument('--stages', nargs='+', choices=STAGES, default=list(STAGES))
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--unprofiled-reference', type=Path)
    parser.add_argument('--scheduling-reference', type=Path)
    parser.add_argument('--cpu-reference', type=Path)
    a = parser.parse_args()
    if 'P0' not in a.stages and not a.unprofiled_reference:
        parser.error('A completed --unprofiled-reference is required when omitting P0')
    a.dirty_batch_pages = a.wp_batch_pages = 512
    record = json.loads(fused.BUILD_RECORD.read_text())
    matched.KERNEL_COMMIT = record['source_freezes']['fixed']['commit']
    with (a.base / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(signum, frame):
            raise RuntimeError('Driver interrupted: ' + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        campaign = Campaign(a)
        try:
            campaign.run()
        except BaseException as exc:
            for _, p, log in campaign.collectors:
                if p.poll() is None:
                    p.send_signal(signal.SIGINT)
                p.wait(timeout=30)
                log.close()
            campaign.failure(exc)
            raise


if __name__ == '__main__':
    main()
