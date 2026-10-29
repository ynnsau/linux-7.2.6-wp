#!/usr/bin/env python3
"""Reproducible Redis/YCSB fixed-frequency CMD-P campaign (Python 3.6+).

Root supervises unprivileged Redis/Java. No kernel/boot changes. Explicit
frequency qualification is separate; a validated lock is left in place.
"""
import argparse
import concurrent.futures
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback

P = pathlib.Path
STUDY = P('/home/yans3/cmdp-v2/redis-ycsb-study')
ROOT = STUDY / 'fixedfreq-v1'
REPO = P('/home/yans3/cmdp-v2/linux-fused-scanner')
SCRIPTS = REPO / 'tools/testing/selftests/cmdp/scripts'
HELPER = STUDY / 'tools/redis-ycsb-study.py'
HELPER_SHA = '1abe8e924b5c500ecc31290aeb42ede216f861b4fc650f6b3213ed862f36b796'
B = None
CONFIG = {'version': 1, 'workloads': ['A', 'B', 'C'],
          'modes': ['off', 'sampling', 'arm'], 'repetitions': 3,
          'workers': 8, 'target_qps': 70000, 'warmup_seconds': 20,
          'measure_seconds': 600, 'jvm_heap_gib': 12, 'redis_cpu': 1,
          'redis_node': 0, 'client_cpus': '26-33', 'client_node': 1,
          'scanner_cpu': 0, 'observer_cpu': 24, 'period_ms': 10000,
          'quiet_ms': 10000, 'dirty_batch_pages': 512, 'wp_batch_pages': 512,
          'frequency_interval_ms': 11000, 'export_timeout_seconds': 1800,
          'kernel': '7.2.6-cmdp-cpu-production', 'leave_frequency_locked': True}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def clock():
    a = time.monotonic()
    w = time.time()
    z = time.monotonic()
    return {'monotonic_ns': int(a * 1e9), 'realtime_ns': int(w * 1e9),
            'anchor_end_ns': int(z * 1e9)}


def save(path, value):
    path = P(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_helper():
    global B
    assert sha(HELPER) == HELPER_SHA, 'Frozen baseline helper changed'
    spec = importlib.util.spec_from_file_location('baseline_helpers', str(HELPER))
    B = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(B)


def emit(event, **values):
    values.update({'event': event, 'at': now()})
    print(json.dumps(values, sort_keys=True), flush=True)


def read_stats():
    return {k: int(v) for k, v in (s.split() for s in
            P('/sys/kernel/debug/cmdp/stats').read_text().splitlines())}


def idle_cmdp():
    s = read_stats()
    assert all(s[k] == 0 for k in ('entries', 'managed', 'isolated', 'waiters')), s
    return s


def frequency_policy():
    profile = json.loads((ROOT / 'profile.json').read_text())
    assert profile['status'] == 'qualified_locked', 'Frequency qualification is not complete'
    assert profile['boot_id'] == P('/proc/sys/kernel/random/boot_id').read_text().strip()
    passed = profile['attempts'][-1]
    assert passed['status'] == 'passed' and passed['seconds'] >= 1800, 'Full frequency qualification required'
    khz = int(profile['selected_khz'])
    policies = sorted(P('/sys/devices/system/cpu/cpufreq').glob('policy*'))
    assert len(policies) == 52
    for p in policies:
        assert (p / 'scaling_governor').read_text().strip() == 'performance', str(p)
        for k in ('scaling_min_freq', 'scaling_max_freq'):
            assert int((p / k).read_text()) == khz, (str(p), k)
        assert int((p / 'boost').read_text()) == 0, str(p)
    assert P('/sys/devices/system/cpu/online').read_text().strip() == '0-51'
    assert int(P('/sys/devices/system/cpu/cpufreq/boost').read_text()) == 0
    return khz


def interrupts():
    a = clock()
    text = P('/proc/interrupts').read_text()
    cpus = [int(x[3:]) for x in text.splitlines()[0].split()]
    line = next(s for s in text.splitlines() if s.lstrip().startswith('TLB:'))
    counts = [int(x) for x in line.split()[1:1 + len(cpus)]]
    return {'begin': a, 'end': clock(), 'cpus': dict(zip(map(str, cpus), counts))}


def thermal_counts():
    return {str(p): int(p.read_text()) for p in
            P('/sys/devices/system/cpu').glob('cpu[0-9]*/thermal_throttle/*_throttle_count')}


def process_identity(pid):
    s = P('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(s[19]),
            'boot_id': P('/proc/sys/kernel/random/boot_id').read_text().strip()}


class Control:
    def __init__(self, name, args, directory):
        self.name = name
        self.path = directory / (name + '.jsonl')
        self.write_fd = None
        rd, wr = os.pipe()
        self.log = open(str(self.path), 'wb')
        self.err = open(str(directory / (name + '.err')), 'wb')
        self.p = subprocess.Popen(list(map(str, args)) + ['--control-fd', str(rd)],
                                  stdout=self.log, stderr=self.err, pass_fds=(rd,),
                                  start_new_session=True)
        os.close(rd)
        self.write_fd = wr
        self.reader = open(str(self.path))
        self.buffer = ''

    def rows(self):
        self.buffer += self.reader.read()
        parts = self.buffer.split('\n')
        self.buffer = parts.pop()
        return [json.loads(s) for s in parts if s]

    def request(self, label):
        assert self.p.poll() is None, (self.name, self.p.returncode)
        os.write(self.write_fd, ('snapshot %s\n' % label).encode())

    def wait_snapshot(self, label, timeout=30):
        deadline = time.monotonic() + timeout
        captured = []
        inside = False
        while time.monotonic() < deadline:
            for row in self.rows():
                if row.get('event') == 'boundary_begin' and row.get('label') == label:
                    inside = True
                if inside:
                    captured.append(row)
                if row.get('event') == 'boundary_end' and row.get('label') == label:
                    assert row.get('ok', True), row
                    return captured
            assert self.p.poll() is None, (self.name, self.p.returncode)
            time.sleep(0.005)
        raise RuntimeError('No %s snapshot from %s' % (label, self.name))

    def stop(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
        self.p.wait(timeout=180)
        self.log.close()
        self.err.close()
        self.reader.close()
        assert self.p.returncode in (0, 143), (self.name, self.p.returncode)


def boundary(label, controls):
    requested = clock()
    for c in controls:
        c.request(label)
    tlb = interrupts()
    rows = {c.name: c.wait_snapshot(label) for c in controls}
    return {'label': label, 'requested': requested, 'tlb': tlb,
            'controllers': rows, 'completed': clock()}


def export_report(path):
    report = {}
    for line in path.read_text(errors='replace').splitlines():
        m = re.match(r'^\[([^]]+)\], ([^,]+), (.+)$', line)
        if not m:
            continue
        group, key, value = m.groups()
        try:
            value = float(value)
        except ValueError:
            pass
        report.setdefault(group, {})[key] = value
    assert 'OVERALL' in report, 'YCSB export not complete'
    for group, metrics in report.items():
        for k, v in metrics.items():
            if k.startswith('Return=') and k != 'Return=OK':
                assert v == 0, (group, k, v)
        if group in ('READ', 'UPDATE'):
            assert metrics['Total Operations'] == metrics.get('Return=OK'), (group, metrics)
            assert metrics['Total Operations'] == report['Intended-' + group]['Total Operations']
    return report


def ycsb_command(directory, workload, count, warm, duration):
    args, props = B.ycsb_argv(directory, workload, count, 8, 8,
                              seconds=warm + duration + 5)
    args = args[:args.index('-p')]
    args = ['-Xms12g' if x == '-Xms4g' else '-Xmx12g' if x == '-Xmx4g' else x for x in args]
    props = {k: v for k, v in props.items() if not k.startswith('hdrhistogram.')}
    props.update({'measurementtype': 'raw', 'measurement.interval': 'both',
                  'measurement.raw.no_summary': 'true',
                  'measurement.raw.output_file': str(directory / 'latency.raw'),
                  'target': 70000})
    for k, v in sorted(props.items()):
        args += ['-p', '%s=%s' % (k, v)]
    return args, props


def counter_summary(start, end, mode):
    result = {'scope': 'post-warmup; excludes STOP cleanup', 'mode': mode}
    ac, zc = start['tlb']['cpus'], end['tlb']['cpus']
    d = {k: zc[k] - v for k, v in ac.items()}
    result['tlb_shootdown_interrupts'] = {'per_cpu': d, 'all_host': sum(d.values()),
                                       'redis_cpu1': d['1'], 'scanner_cpu0': d['0'],
                                       'client_pool': sum(d[str(i)] for i in range(26, 34))}
    if mode == 'off':
        result.update({'first_arms': 0, 'rearms': 0, 'arm_events': 0, 'wp_events': 0,
                       'flush_calls': 0, 'passes_completed': 0})
        return result
    ar = start['controllers']['cmdp']
    zr = end['controllers']['cmdp']
    a = next(x for x in ar if 'terminals' in x)
    z = next(x for x in zr if 'terminals' in x)
    for k in ('first_arms', 'rearms', 'passes_started', 'passes_completed', 'overruns',
              'protected_byte_ms', 'resource_failures', 'reports_dropped'):
        if k in a:
            result[k] = z[k] - a[k]
    result['arm_events'] = result['first_arms'] + result['rearms']
    result['wp_events'] = z['terminals']['write'] - a['terminals']['write']
    result['active_start_pages'] = a['active']
    result['active_end_pages'] = z['active']
    result['status_window_ns'] = [a['now_ns'], z['now_ns']]
    ab = next(x for x in ar if x.get('event') == 'batch_status')
    zb = next(x for x in zr if x.get('event') == 'batch_status')
    for k in ('flush_calls', 'shared_flush_calls', 'dirty_only_flush_calls',
              'wp_only_flush_calls', 'flush_ns', 'dirty_pages', 'wp_pages'):
        result[k] = zb[k] - ab[k]
    assert result['flush_calls'] == sum(result[k] for k in
            ('shared_flush_calls', 'dirty_only_flush_calls', 'wp_only_flush_calls'))
    result['flush_window_ns'] = [ab['now_ns'], zb['now_ns']]
    ap = next(x for x in ar if 'write_calls' in x)
    zp = next(x for x in zr if 'write_calls' in x)
    for k in ('write_calls', 'write_ns'):
        result[k] = zp[k] - ap[k]
    assert result.get('resource_failures', 0) == result.get('reports_dropped', 0) == 0
    if mode == 'sampling':
        assert result['arm_events'] == result['wp_events'] == result['active_end_pages'] == 0
    return result


def validate_control(directory):
    directory = P(directory)
    if (directory / 'cmdp.jsonl').exists():
        rows = [json.loads(s) for s in (directory / 'cmdp.jsonl').read_text().splitlines()]
        err = directory / 'cmdp.err'
        assert not err.exists() or not err.read_text().strip(), 'CMD-P controller stderr is nonempty'
        exits = [x for x in rows if x.get('event') == 'controller_exit']
        assert len(exits) == 1 and exits[0]['exit_code'] == 143, exits
        stops = [x for x in rows if x.get('event') == 'stop_return']
        assert len(stops) == 1 and stops[0]['errno'] == 0, stops
        final = [x for x in rows if x.get('event') == 'final']
        assert len(final) == 1, final
        final = final[0]
        assert final['state'] == 4 and final['stop_reason'] == 'control_stop', final
        assert final['active'] == final['resource_failures'] == final['reports_dropped'] == 0
        cpu = [x for x in rows if x.get('event') == 'cpu_status']
        assert cpu
        for x in cpu:
            assert x['cpu'] == 0 and x['error'] == x['wrong_cpu_work_items'] == x['offline_events'] == 0, x
            if x['work_items']:
                assert x['last_cpu'] == 0, x
    if (directory / 'frequency.jsonl').exists():
        rows = [json.loads(s) for s in (directory / 'frequency.jsonl').read_text().splitlines()]
        exits = [x for x in rows if x.get('event') == 'frequency_exit']
        assert len(exits) == 1 and exits[0]['exit_code'] == 0, exits


def analyze_frequency(directory, timeline, khz):
    argv = [sys.executable, str(SCRIPTS / 'report-redis-ycsb-fixedfreq.py'), 'frequency',
            '--input', str(directory / 'frequency.jsonl'),
            '--start-ns', str(int(timeline['start_monotonic'] * 1e9)),
            '--end-ns', str(int(timeline['end_monotonic'] * 1e9)),
            '--selected-khz', str(khz), '--expected-cpus', '0-51', '--require-boundaries',
            '--output', str(directory / 'frequency-validation.json')]
    with open(str(directory / 'frequency-analysis.log'), 'w') as log:
        subprocess.check_call(argv, stdout=log, stderr=subprocess.STDOUT)
    report = json.loads((directory / 'frequency-validation.json').read_text())
    assert report['validation']['passed'], report['validation']
    return report


def selected_sources():
    paths = [P(__file__).resolve(), HELPER, SCRIPTS / 'analyze-redis-ycsb-raw.py',
             SCRIPTS / 'cpu-frequency.py', SCRIPTS / 'report-redis-ycsb-fixedfreq.py',
             ROOT / 'bin/cmdp_monitor', ROOT / 'bin/frequency_collect']
    return {str(p): sha(p) for p in paths}


def verify_capture_receipt(directory):
    directory = P(directory)
    receipt = json.loads((directory / 'capture-receipt.json').read_text())
    assert receipt['passed']
    for relative, digest in receipt['files_sha256'].items():
        assert sha(directory / relative) == digest, 'Changed capture artifact: ' + relative
    return receipt


def capture(directory, workload, mode, warm=20, duration=600, telemetry=True):
    assert os.geteuid() == 0, 'Root supervision required'
    directory = P(directory)
    assert not directory.exists(), 'Preserve prior attempts; use a fresh run directory'
    if directory.parent == ROOT / 'campaign-v1':
        expected = json.loads((ROOT / 'source-manifest.json').read_text())['hashes']
        assert selected_sources() == expected, 'Frozen campaign sources changed'
    B.mkdir(directory)
    os.sched_setaffinity(0, {24})
    khz = frequency_policy()
    assert os.uname().release == CONFIG['kernel']
    assert shutil.disk_usage(str(ROOT)).free > 12 * 1024 ** 3, 'Insufficient export disk reserve'
    before = idle_cmdp()
    policies = B.policies()
    original_thermal = thermal_counts()
    manifest = json.loads((STUDY / 'artifacts/golden/manifest.json').read_text())
    save(directory / 'run.json', {'at': now(), 'workload': workload, 'mode': mode,
         'warmup_seconds': warm, 'measure_seconds': duration, 'target_qps': 70000,
         'workers': 8, 'selected_khz': khz, 'telemetry': telemetry,
         'policy': policies, 'source_hashes': selected_sources()})
    p = r = client = None
    controls = []
    stopped = False
    observations = []
    boundaries = {}
    identities = {'controls': {}}
    emit('restore_start', run=str(directory), workload=workload, mode=mode)
    try:
        p, r, restore_s = B.start_redis(directory, STUDY / 'artifacts/golden')
        identities['redis'] = process_identity(p.pid)
        save(directory / 'processes.json', identities)
        validation = B.validate_db(r, manifest['recordcount'], manifest['samples'])
        assert abs(validation['info']['used_memory'] / (16 * 1024 ** 3) - 1) <= .05
        save(directory / 'restore.json', {'seconds': restore_s, 'validation': validation,
              'rdb_sha256': manifest['rdb_sha256'], 'redis': process_identity(p.pid)})
        for name in ('numa_maps', 'smaps_rollup'):
            (directory / ('redis-' + name + '-before.txt')).write_text(P('/proc/%d/%s' % (p.pid, name)).read_text())
        if mode != 'off':
            argv = [ROOT / 'bin/cmdp_monitor', '--pid', p.pid, '--scanner-cpu', 0,
                    '--period-ms', 10000, '--quiet-ms', 10000,
                    '--dirty-batch-pages', 512, '--wp-batch-pages', 512,
                    '--dirty-observe' if mode == 'sampling' else '--dirty-qualify']
            controls.append(Control('cmdp', argv, directory))
            identities['controls']['cmdp'] = process_identity(controls[-1].p.pid)
            save(directory / 'processes.json', identities)
        if telemetry:
            controls.append(Control('frequency', [ROOT / 'bin/frequency_collect',
                            '--interval-ms', CONFIG['frequency_interval_ms']], directory))
            identities['controls']['frequency'] = process_identity(controls[-1].p.pid)
            save(directory / 'processes.json', identities)
        for c in controls:
            c.request('ready')
        for c in controls:
            c.wait_snapshot('ready')
        args, props = ycsb_command(directory, workload, manifest['recordcount'], warm, duration)
        save(directory / 'command.json', {'argv': args, 'properties': props})
        client = B.spawn(args, directory, directory / 'ycsb.log', '26-33', 1)
        identities['ycsb'] = process_identity(client.pid)
        save(directory / 'processes.json', identities)
        launched = time.monotonic()
        next_observation = launched
        first_status = None
        timeline = None
        clock_offset_max_change_ns = 0
        log_reader = open(str(directory / 'ycsb.log'))
        log_buffer = ''
        export_started = None
        with open(str(directory / 'observations.jsonl'), 'w') as obs:
            while client.poll() is None:
                t = time.monotonic()
                log_buffer += log_reader.read()
                lines = log_buffer.split('\n')
                log_buffer = lines.pop()
                for line in lines:
                    m = B.STATUS_RE.search(line)
                    if m and first_status is None:
                        dt = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S:%f').replace(tzinfo=datetime.timezone.utc)
                        first_status = {'epoch': dt.timestamp(), 'elapsed_s': int(m.group(2))}
                        assert first_status['elapsed_s'] == 0, 'Missing initial YCSB status anchor'
                        anchor = clock()
                        zero_mono = anchor['monotonic_ns'] / 1e9 + first_status['epoch'] - anchor['realtime_ns'] / 1e9
                        timeline = {'query_epoch_ms': int(round(first_status['epoch'] * 1000)),
                                    'start_monotonic': zero_mono + warm,
                                    'end_monotonic': zero_mono + warm + duration,
                                    'start_ms': int(round((first_status['epoch'] + warm) * 1000)),
                                    'end_ms': int(round((first_status['epoch'] + warm + duration) * 1000)),
                                    'anchor': anchor}
                        save(directory / 'timeline.json', timeline)
                        emit('query_start', run=directory.name, timeline=timeline)
                    if 'Maximum time elapsed.' in line:
                        export_started = t
                if first_status is None and t - launched > 60:
                    raise RuntimeError('YCSB did not start within 60 seconds')
                if not stopped:
                    for c in controls:
                        assert c.p.poll() is None, ('Premature controller exit', c.name, c.p.returncode)
                if timeline:
                    for name, deadline in (('measurement_start', timeline['start_monotonic']),
                                           ('measurement_end', timeline['end_monotonic'])):
                        if name not in boundaries and t >= deadline:
                            boundaries[name] = boundary(name, controls)
                            boundaries[name]['scheduled_monotonic_ns'] = int(deadline * 1e9)
                            boundaries[name]['request_lateness_ns'] = boundaries[name]['requested']['monotonic_ns'] - int(deadline * 1e9)
                            save(directory / 'boundaries.json', boundaries)
                            emit(name, run=directory.name)
                    if 'measurement_end' in boundaries and not stopped:
                        for c in controls:
                            c.stop()
                        stopped = True
                        export_started = t
                        validate_control(directory)
                    if export_started and t - export_started > CONFIG['export_timeout_seconds']:
                        raise RuntimeError('YCSB raw export exceeded separate 30-minute deadline')
                if t >= next_observation and not stopped:
                    row = {'clock': clock(), 'redis': B.proc(p.pid), 'client': B.proc(client.pid),
                           'info': r.info('memory', 'stats', 'cpu', 'persistence'), 'cmdp': read_stats(),
                           'redis_threads': B.affinity(p.pid, [1]),
                           'client_threads': B.affinity(client.pid, list(range(26, 34))),
                           'selected_khz': frequency_policy()}
                    if timeline:
                        anchor = timeline['anchor']
                        offset = row['clock']['realtime_ns'] - row['clock']['monotonic_ns']
                        original_offset = anchor['realtime_ns'] - anchor['monotonic_ns']
                        clock_offset_max_change_ns = max(clock_offset_max_change_ns, abs(offset - original_offset))
                        assert clock_offset_max_change_ns < 100000000, 'Wall/monotonic clock mapping changed by >=100ms'
                    assert row['redis']['vm_swap_kib'] == row['client']['vm_swap_kib'] == 0
                    assert row['info']['evicted_keys'] == row['info']['expired_keys'] == 0
                    assert row['info']['rdb_bgsave_in_progress'] == row['info']['aof_enabled'] == 0
                    if mode == 'off':
                        assert row['cmdp'] == before, 'Unexpected CMD-P activity in off mode'
                    obs.write(json.dumps(row, sort_keys=True) + '\n')
                    obs.flush()
                    observations.append(row)
                    next_observation = t + 1
                time.sleep(.01 if not stopped else .25)
        log_reader.close()
        assert client.returncode == 0, ('YCSB exit', client.returncode)
        assert 'measurement_end' in boundaries, 'Client ended before measurement window'
        report = export_report(directory / 'ycsb.log')
        save(directory / 'export.json', report)
        counts = {k: int(v['Total Operations']) for k, v in report.items() if 'Total Operations' in v}
        save(directory / 'expected-counts.json', counts)
        counters = counter_summary(boundaries['measurement_start'], boundaries['measurement_end'], mode)
        counters['snapshot_timing'] = {
            k: {'request_lateness_ns': v['request_lateness_ns'],
                'snapshot_elapsed_ns': v['completed']['monotonic_ns'] - v['requested']['monotonic_ns']}
            for k, v in boundaries.items()}
        save(directory / 'counters.json', counters)
        assert B.policies() == policies
        assert frequency_policy() == khz
        assert thermal_counts() == original_thermal, 'New thermal throttling during run'
        after = idle_cmdp()
        if mode == 'off':
            assert after == before
        validate_control(directory)
        if telemetry:
            analyze_frequency(directory, timeline, khz)
        assert r.call('DBSIZE') == manifest['recordcount'] + 1
        assert r.call('ZCARD', '_indices') == manifest['recordcount']
        for name in ('numa_maps', 'smaps_rollup'):
            (directory / ('redis-' + name + '-after.txt')).write_text(P('/proc/%d/%s' % (p.pid, name)).read_text())
        B.stop_redis(p, r)
        r = None
        window = [x for x in observations if timeline['start_ms'] * 1000000 <= x['clock']['realtime_ns'] < timeline['end_ms'] * 1000000]
        summary = {'at': now(), 'status': 'captured_pending_raw_analysis', 'workload': workload,
                   'mode': mode, 'workers': 8, 'target_qps': 70000, 'selected_khz': khz,
                   'timeline': timeline, 'restore_seconds': restore_s, 'counters': counters,
                   'redis_rss_peak': max(x['redis']['rss_bytes'] for x in window),
                   'client_rss_peak': max(x['client']['rss_bytes'] for x in window),
                   'redis_used_memory_mean': sum(x['info']['used_memory'] for x in window) / len(window),
                   'cmdp_before': before, 'cmdp_after': after, 'frequency_telemetry': telemetry,
                   'clock_offset_max_change_ns': clock_offset_max_change_ns,
                   'query_and_export_seconds': time.monotonic() - launched}
        save(directory / 'capture-summary.json', summary)
        hashes = {str(x.relative_to(directory)): sha(x) for x in directory.rglob('*') if x.is_file()}
        save(directory / 'capture-receipt.json', {'at': now(), 'passed': True, 'files_sha256': hashes})
        emit('capture_complete', run=directory.name, summary=summary)
        return summary
    except BaseException as e:
        save(directory / 'failure.json', {'at': now(), 'error': str(e), 'traceback': traceback.format_exc()})
        raise
    finally:
        if not stopped:
            for c in controls:
                try:
                    c.stop()
                except Exception as e:
                    emit('controller_cleanup_error', error=str(e), controller=c.name)
        if client is not None and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=20)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
        if p is not None and r is not None:
            B.stop_redis(p, r)


def analyze_run(directory):
    directory = P(directory)
    verify_capture_receipt(directory)
    timeline = json.loads((directory / 'timeline.json').read_text())
    argv = [sys.executable, str(SCRIPTS / 'analyze-redis-ycsb-raw.py'),
            '--input', str(directory / 'latency.raw'), '--start-ms', str(timeline['start_ms']),
            '--end-ms', str(timeline['end_ms']), '--output', str(directory / 'latency.json'),
            '--ycsb-log', str(directory / 'ycsb.log'), '--require-complete']
    with open(str(directory / 'analysis.log'), 'w') as log:
        subprocess.check_call(argv, stdout=log, stderr=subprocess.STDOUT)
    summary = json.loads((directory / 'capture-summary.json').read_text())
    summary['status'] = 'complete'
    summary['latency'] = json.loads((directory / 'latency.json').read_text())
    save(directory / 'summary.json', summary)
    subprocess.check_call([sys.executable, str(SCRIPTS / 'report-redis-ycsb-fixedfreq.py'),
                           'run', '--directory', str(directory), '--output', str(directory / 'report.json')])
    hashes = {str(x.relative_to(directory)): sha(x) for x in directory.rglob('*')
              if x.is_file() and x.name != 'receipt.json'}
    save(directory / 'receipt.json', {'at': now(), 'passed': True, 'files_sha256': hashes})
    emit('analysis_complete', run=directory.name)


def suite():
    """Durable serial pilots/capture, then parallel analysis with no live workloads."""
    assert os.geteuid() == 0
    assert json.loads((ROOT / 'config.json').read_text()) == CONFIG
    runner = str(P(__file__).resolve())
    stages = [('B-' + mode + '-smoke-v1', 'B', mode, 40, True)
              for mode in CONFIG['modes']]
    stages += [('A-off-smoke-v1', 'A', 'off', 40, True)]
    stages += [('B-off-full-export-v1', 'B', 'off', 600, True)]
    for rep in range(1, 4):
        for telemetry in ([True, False] if rep % 2 else [False, True]):
            stages.append(('C-observer-%s-r%d' % ('on' if telemetry else 'off', rep),
                           'C', 'off', 120, telemetry))
    for name, workload, mode, seconds, telemetry in stages:
        directory = ROOT / 'pilots' / name
        if (directory / 'receipt.json').exists():
            B.verify_receipt(directory)
            continue
        assert not directory.exists(), 'Preserve and review incomplete pilot before resuming suite: ' + str(directory)
        save(ROOT / 'suite-state.json', {'at': now(), 'status': 'pilot', 'pilot': name,
                                        'supervisor': process_identity(os.getpid())})
        argv = [sys.executable, runner, 'pilot', '--name', name, '--workload', workload,
                '--mode', mode, '--seconds', str(seconds)]
        if not telemetry:
            argv.append('--no-frequency-telemetry')
        subprocess.check_call(argv)
    save(ROOT / 'suite-state.json', {'at': now(), 'status': 'campaign'})
    subprocess.check_call([sys.executable, runner, 'resume'])
    save(ROOT / 'suite-state.json', {'at': now(), 'status': 'analysis'})
    subprocess.check_call([sys.executable, runner, 'analyze', '--jobs', '8'])
    subprocess.check_call([sys.executable, str(SCRIPTS / 'report-redis-ycsb-fixedfreq.py'),
                           'campaign', '--directory', str(ROOT / 'campaign-v1'),
                           '--output-dir', str(ROOT / 'analysis')])
    save(ROOT / 'suite-state.json', {'at': now(), 'status': 'complete'})
    emit('suite_complete')


def matrix():
    for rep in range(3):
        for workload in CONFIG['workloads'][rep:] + CONFIG['workloads'][:rep]:
            for mode in CONFIG['modes'][rep:] + CONFIG['modes'][:rep]:
                yield '%s-%s-r%d' % (workload, mode, rep + 1), workload, mode


def prepare():
    B.mkdir(ROOT / 'campaign-v1')
    B.mkdir(ROOT / 'pilots')
    golden = STUDY / 'artifacts/golden'
    golden_manifest = json.loads((golden / 'manifest.json').read_text())
    golden_sha = sha(golden / 'dump.rdb')
    assert golden_sha == golden_manifest['rdb_sha256'], 'Golden RDB checksum differs'
    save(ROOT / 'golden-verification.json', {'at': now(), 'sha256': golden_sha,
         'bytes': (golden / 'dump.rdb').stat().st_size, 'recordcount': golden_manifest['recordcount']})
    if (ROOT / 'config.json').exists():
        assert json.loads((ROOT / 'config.json').read_text()) == CONFIG
    else:
        save(ROOT / 'config.json', CONFIG)
    hashes = selected_sources()
    manifest_path = ROOT / 'source-manifest.json'
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if any((ROOT / 'campaign-v1').iterdir()):
            assert previous['hashes'] == hashes, 'Cannot refreeze modified sources over campaign evidence'
        elif previous['hashes'] != hashes:
            archive = ROOT / 'source-manifests' / (datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ') + '.json')
            save(archive, previous)
            save(manifest_path, {'at': now(), 'hashes': hashes,
                 'commit': B.command(['git', '-C', REPO, 'rev-parse', 'HEAD'])})
    else:
        save(manifest_path, {'at': now(), 'hashes': hashes,
             'commit': B.command(['git', '-C', REPO, 'rev-parse', 'HEAD'])})
    wrapper = ROOT / 'bin/redis-ycsb-campaign'
    wrapper.write_text('#!/bin/sh\nexec python3 ' + str(P(__file__).resolve()) + ' "$@"\n')
    wrapper.chmod(0o755)
    emit('prepared', root=str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare', 'pilot', 'run', 'resume', 'suite', 'status', 'analyze'])
    p.add_argument('--workload', choices=['A', 'B', 'C'], default='B')
    p.add_argument('--mode', choices=['off', 'sampling', 'arm'], default='off')
    p.add_argument('--seconds', type=int, default=40)
    p.add_argument('--warmup', type=int, default=20)
    p.add_argument('--name')
    p.add_argument('--no-frequency-telemetry', action='store_true')
    p.add_argument('--directory', type=P)
    p.add_argument('--jobs', type=int, default=1)
    args = p.parse_args()
    load_helper()
    if args.action == 'suite':
        ROOT.mkdir(parents=True, exist_ok=True)
        with open(str(ROOT / 'suite.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            suite()
        return
    if args.action == 'status':
        captured = sorted((ROOT / 'campaign-v1').glob('*/capture-receipt.json'))
        analyzed = sorted((ROOT / 'campaign-v1').glob('*/receipt.json'))
        failures = sorted((ROOT / 'campaign-v1').glob('*/failure.json'))
        emit('status', captured=len(captured), analyzed=len(analyzed), expected=27,
             failures=[str(x) for x in failures], state=json.loads((ROOT / 'campaign-state.json').read_text())
             if (ROOT / 'campaign-state.json').exists() else None)
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(str(ROOT / 'experiment.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'prepare':
            prepare()
        elif args.action == 'pilot':
            assert args.name, '--name is required to preserve pilot attempts'
            capture(ROOT / 'pilots' / args.name, args.workload, args.mode,
                    args.warmup, args.seconds, not args.no_frequency_telemetry)
            analyze_run(ROOT / 'pilots' / args.name)
        elif args.action in ('run', 'resume'):
            assert json.loads((ROOT / 'config.json').read_text()) == CONFIG
            expected = json.loads((ROOT / 'source-manifest.json').read_text())['hashes']
            assert selected_sources() == expected, 'Frozen campaign sources changed'
            frequency_policy()
            os.sched_setaffinity(0, {24})
            for name, workload, mode in matrix():
                directory = ROOT / 'campaign-v1' / name
                if (directory / 'capture-receipt.json').exists():
                    verify_capture_receipt(directory)
                    continue
                if directory.exists():
                    processes = directory / 'processes.json'
                    if processes.exists():
                        ids = json.loads(processes.read_text())
                        all_ids = [ids[k] for k in ('redis', 'ycsb') if k in ids] + list(ids['controls'].values())
                        for identity in all_ids:
                            try:
                                current = process_identity(identity['pid'])
                            except FileNotFoundError:
                                continue
                            assert current != identity, 'Prior attempt still owns live processes; inspect before resume'
                    archive = ROOT / 'failed-attempts' / (name + '-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    assert not archive.exists()
                    directory.rename(archive)
                save(ROOT / 'campaign-state.json', {'at': now(), 'status': 'running', 'run': name,
                     'supervisor': process_identity(os.getpid())})
                capture(directory, workload, mode)
            save(ROOT / 'campaign-state.json', {'at': now(), 'status': 'captured', 'count': 27})
        elif args.action == 'analyze':
            directories = [args.directory] if args.directory else sorted((ROOT / 'campaign-v1').glob('*'))
            directories = [d for d in directories if (d / 'capture-receipt.json').exists()
                           and not (d / 'receipt.json').exists()]
            assert 1 <= args.jobs <= 8
            idle_cmdp()
            os.sched_setaffinity(0, set(range(26, 34)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                list(pool.map(analyze_run, directories))
            emit('analysis_finished')


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt('signal %d' % signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except BaseException as e:
        emit('failure', error=str(e))
        raise
