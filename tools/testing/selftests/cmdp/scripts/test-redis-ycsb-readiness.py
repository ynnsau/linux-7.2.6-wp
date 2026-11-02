#!/usr/bin/env python3
"""Replay actual eligibility-v2 ARM sweep timing and test warmup admission.

Embedded rows are selected from B-arm-v1 (2026-11-01). They preserve the
original successful sample-only pass, ARM-bearing pass2 and boundary clocks.
Optional --trace-directory replays the complete original evidence as well.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

P = pathlib.Path
spec = importlib.util.spec_from_file_location('readiness_runner',
        str(P(__file__).with_name('redis-ycsb-fixedfreq.py')))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

FIXTURE = {'early': {'completed': {'anchor_end_ns': 663173149523927,
                         'monotonic_ns': 663173149523457,
                         'realtime_ns': 1793567531700015616},
           'controllers': {'cmdp': [{'active': 1204870,
                                     'attach_ns': 663152855540876,
                                     'event': 'boundary_status',
                                     'first_arms': 1213215,
                                     'flags': 0,
                                     'history_entries': 4239907,
                                     'history_resets': 0,
                                     'now_ns': 663173138833763,
                                     'overruns': 0,
                                     'passes_completed': 1,
                                     'passes_started': 2,
                                     'period_ms': 10000,
                                     'pre_stop_active': 0,
                                     'protected_byte_ms': 26705915422346,
                                     'rearms': 0,
                                     'reports_dropped': 0,
                                     'resource_failures': 0,
                                     'session_id': 88,
                                     'state': 2,
                                     'stop_reason': 'none',
                                     'target_pid': 47961,
                                     'terminals': {'control_stop': 0,
                                                   'error': 0,
                                                   'exec': 0,
                                                   'fork': 0,
                                                   'invalidate': 0,
                                                   'none': 0,
                                                   'scope_change': 0,
                                                   'target_exit': 0,
                                                   'thp_change': 0,
                                                   'write': 8345}},
                                    {'cpu': 0,
                                     'error': 0,
                                     'event': 'cpu_status',
                                     'last_cpu': 0,
                                     'now_ns': 663173138846237,
                                     'offline_events': 0,
                                     'work_items': 2,
                                     'wrong_cpu_work_items': 0},
                                    {'clean': 1213215,
                                     'dirty': 4339129,
                                     'event': 'dirty_status',
                                     'flush_ns': 16189797,
                                     'flushes': 4339129,
                                     'invalidations': 0,
                                     'late_dirty': 0,
                                     'metadata_bytes': 678385120,
                                     'mode': 2,
                                     'now_ns': 663173138860222,
                                     'passes_completed': 1,
                                     'passes_started': 2,
                                     'primed': 4239907,
                                     'qualified': 1213215,
                                     'quiet_ms': 10000,
                                     'reset_ns': 195007553,
                                     'resets': 4339129,
                                     'resource_skips': 0,
                                     'sample_ns': 1155961956,
                                     'samples': 5552344,
                                     'unknown': 1472268,
                                     'valid_observations': 3026692,
                                     'wp_resets': 8345}]},
           'requested': {'anchor_end_ns': 663173135978496,
                         'monotonic_ns': 663173135977887,
                         'realtime_ns': 1793567531686470144},
           'scheduled_monotonic_ns': 663173135507822},
 'pid': 47961,
 'ready': {'completed': {'monotonic_ns': 663213922404388},
           'controllers': {'cmdp': [{'active': 3865388,
                                     'attach_ns': 663152855540876,
                                     'event': 'status',
                                     'first_arms': 3932515,
                                     'flags': 0,
                                     'history_entries': 4239920,
                                     'history_resets': 16,
                                     'now_ns': 663213922360692,
                                     'overruns': 3,
                                     'passes_completed': 3,
                                     'passes_started': 4,
                                     'period_ms': 10000,
                                     'pre_stop_active': 0,
                                     'protected_byte_ms': 519354235273066,
                                     'rearms': 1145,
                                     'reports_dropped': 0,
                                     'resource_failures': 0,
                                     'session_id': 88,
                                     'state': 2,
                                     'stop_reason': 'none',
                                     'target_pid': 47961,
                                     'terminals': {'control_stop': 0,
                                                   'error': 0,
                                                   'exec': 0,
                                                   'fork': 0,
                                                   'invalidate': 2,
                                                   'none': 0,
                                                   'scope_change': 0,
                                                   'target_exit': 0,
                                                   'thp_change': 0,
                                                   'write': 68270}},
                                    {'cpu': 0,
                                     'error': 0,
                                     'event': 'cpu_status',
                                     'last_cpu': 0,
                                     'now_ns': 663213922389389,
                                     'offline_events': 0,
                                     'work_items': 4,
                                     'wrong_cpu_work_items': 0},
                                    {'clean': 3950179,
                                     'dirty': 4935389,
                                     'event': 'dirty_status',
                                     'flush_ns': 124105316,
                                     'flushes': 4935389,
                                     'invalidations': 3,
                                     'late_dirty': 1,
                                     'metadata_bytes': 678387200,
                                     'mode': 2,
                                     'now_ns': 663213922404387,
                                     'passes_completed': 3,
                                     'passes_started': 4,
                                     'primed': 4288149,
                                     'qualified': 3933661,
                                     'quiet_ms': 10000,
                                     'reset_ns': 218727228,
                                     'resets': 4935389,
                                     'resource_skips': 0,
                                     'sample_ns': 1897389198,
                                     'samples': 8885568,
                                     'unknown': 2507046,
                                     'valid_observations': 354485,
                                     'wp_resets': 68270}]},
           'requested': {'monotonic_ns': 663213135507822},
           'scheduled_monotonic_ns': 663213135507822},
 'trace_rows': [{'active': 0,
                 'active_first': 0,
                 'active_rearm': 0,
                 'already_armed': 0,
                 'complete': 1,
                 'cursor': 140737488351232,
                 'eligible_pages': 4239879,
                 'end_ns': 663155251185296,
                 'error': 0,
                 'event': 'sweep',
                 'examined_pages': 4984293,
                 'first_arms': 0,
                 'observed_ns': 663155859878983,
                 'other_terminal': 0,
                 'pass_id': 1,
                 'rearms': 0,
                 'resident_pages': 4239896,
                 'resident_pages_scope': 'inspected_supported_vmas',
                 'skips': {'aliases': 0,
                           'busy': 0,
                           'nonresident': 744306,
                           'other': 0,
                           'page_type': 7,
                           'pins': 10,
                           'resource': 0,
                           'swapped': 0,
                           'thp_policy': 0,
                           'vma': 91},
                 'start_ns': 663152855961956,
                 'writable_pages': 4984293,
                 'write_revoked': 0},
                {'active': 3845874,
                 'active_first': 3845874,
                 'active_rearm': 0,
                 'already_armed': 0,
                 'complete': 1,
                 'cursor': 140737488351232,
                 'eligible_pages': 4239912,
                 'end_ns': 663201644070773,
                 'error': 0,
                 'event': 'sweep',
                 'examined_pages': 4984293,
                 'first_arms': 3888716,
                 'observed_ns': 663201909329536,
                 'other_terminal': 2,
                 'pass_id': 2,
                 'rearms': 0,
                 'resident_pages': 4239920,
                 'resident_pages_scope': 'inspected_supported_vmas',
                 'skips': {'aliases': 0,
                           'busy': 1,
                           'nonresident': 744282,
                           'other': 0,
                           'page_type': 6,
                           'pins': 2,
                           'resource': 0,
                           'swapped': 0,
                           'thp_policy': 0,
                           'vma': 91},
                 'start_ns': 663162920066501,
                 'writable_pages': 4984293,
                 'write_revoked': 42840},
                {'active': 3865388,
                 'attach_ns': 663152855540876,
                 'event': 'status',
                 'first_arms': 3932515,
                 'flags': 0,
                 'history_entries': 4239920,
                 'history_resets': 16,
                 'now_ns': 663213922360692,
                 'overruns': 3,
                 'passes_completed': 3,
                 'passes_started': 4,
                 'period_ms': 10000,
                 'pre_stop_active': 0,
                 'protected_byte_ms': 519354235273066,
                 'rearms': 1145,
                 'reports_dropped': 0,
                 'resource_failures': 0,
                 'session_id': 88,
                 'state': 2,
                 'stop_reason': 'none',
                 'target_pid': 47961,
                 'terminals': {'control_stop': 0,
                               'error': 0,
                               'exec': 0,
                               'fork': 0,
                               'invalidate': 2,
                               'none': 0,
                               'scope_change': 0,
                               'target_exit': 0,
                               'thp_change': 0,
                               'write': 68270}},
                {'cpu': 0,
                 'error': 0,
                 'event': 'cpu_status',
                 'last_cpu': 0,
                 'now_ns': 663213922389389,
                 'offline_events': 0,
                 'work_items': 4,
                 'wrong_cpu_work_items': 0},
                {'clean': 3950179,
                 'dirty': 4935389,
                 'event': 'dirty_status',
                 'flush_ns': 124105316,
                 'flushes': 4935389,
                 'invalidations': 3,
                 'late_dirty': 1,
                 'metadata_bytes': 678387200,
                 'mode': 2,
                 'now_ns': 663213922404387,
                 'passes_completed': 3,
                 'passes_started': 4,
                 'primed': 4288149,
                 'qualified': 3933661,
                 'quiet_ms': 10000,
                 'reset_ns': 218727228,
                 'resets': 4935389,
                 'resource_skips': 0,
                 'sample_ns': 1897389198,
                 'samples': 8885568,
                 'unknown': 2507046,
                 'valid_observations': 354485,
                 'wp_resets': 68270}],
 'used_memory': 17168011392}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.rows = copy.deepcopy(FIXTURE['trace_rows'])
        self.boundary = copy.deepcopy(FIXTURE['ready'])

    def report(self, boundary=None):
        return R.arm_readiness(self.rows, boundary or self.boundary,
                FIXTURE['used_memory'], FIXTURE['pid'])

    def failure(self, term, retryable=False):
        result = self.report()
        self.assertFalse(result['passed'])
        self.assertEqual(result['retryable'], retryable)
        self.assertTrue(any(term in s for s in result['reasons']), result)

    def test_real_twenty_second_boundary_rejects_partial_arm(self):
        report = self.report(FIXTURE['early'])
        self.assertFalse(report['passed'])
        self.assertTrue(report['retryable'], report)
        self.assertGreater(report['boundary_status']['first_arms'], 1000000)
        self.assertEqual(report['boundary_status']['passes_completed'], 1)

    def test_real_sixty_second_boundary_accepts_completed_arm(self):
        report = self.report()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['initial_arm_sweep']['pass_id'], 2)
        self.assertGreater(report['completion_margin_ns'], 10 * 10 ** 9)
        self.assertGreater(report['initial_arm_sweep']['first_arms'], 3000000)

    def test_partial_arm_does_not_qualify(self):
        self.rows[1]['complete'] = 0
        self.failure('No successful complete', retryable=True)

    def test_arm_end_after_scheduled_start_does_not_qualify(self):
        self.rows[1]['end_ns'] = self.boundary['scheduled_monotonic_ns'] + 1
        self.rows[1]['observed_ns'] = self.rows[1]['end_ns'] + 1
        self.failure('No successful complete', retryable=True)

    def test_candidate_observed_after_boundary_does_not_qualify(self):
        self.rows[1]['observed_ns'] = self.boundary['completed']['monotonic_ns'] + 1
        self.failure('No successful complete', retryable=True)

    def test_monotonic_ordering_rejected(self):
        self.rows[1]['start_ns'] = self.rows[1]['end_ns'] + 1
        self.failure('monotonic ordering')

    def test_boundary_clock_ordering_rejected(self):
        self.boundary['requested']['monotonic_ns'] = self.boundary['completed']['monotonic_ns'] + 1
        self.failure('monotonic ordering')

    def test_scan_error_is_hard_failure(self):
        self.rows[1]['error'] = -12
        self.failure('scan error')

    def test_thp_exclusion_is_hard_failure(self):
        self.rows[1]['skips']['thp_policy'] = 1
        self.failure('THP-policy')

    def test_low_eligibility_is_hard_failure(self):
        self.rows[1]['eligible_pages'] = 7
        self.failure('excludes substantial')

    def test_no_live_arm_floor(self):
        self.rows[1]['active'] = 0
        self.boundary['controllers']['cmdp'][0]['active'] = 0
        self.assertTrue(self.report()['passed'])

    def test_dropped_reports_is_hard_failure(self):
        self.boundary['controllers']['cmdp'][0]['reports_dropped'] = 1
        self.failure('dropped report')

    def test_wrong_target_is_hard_failure(self):
        self.boundary['controllers']['cmdp'][0]['target_pid'] += 1
        self.failure('PID differs')

    def test_missing_snapshot_is_hard_failure(self):
        self.boundary['controllers']['cmdp'] = []
        self.failure('Missing or ambiguous')

    def test_wrong_cpu_is_hard_failure(self):
        self.boundary['controllers']['cmdp'][1]['wrong_cpu_work_items'] = 1
        self.failure('CPU assignment')

    def test_dirty_resource_error_is_hard_failure(self):
        self.boundary['controllers']['cmdp'][2]['resource_skips'] = 1
        self.failure('resource skips')

    def test_future_shutdown_cannot_pollute_boundary(self):
        row = copy.deepcopy(self.rows[1])
        row.update(error=-108, complete=0,
                   observed_ns=self.boundary['completed']['monotonic_ns'] + 100)
        self.rows.append(row)
        self.assertTrue(self.report()['passed'])

    def test_evidence_file_and_retryable_exception(self):
        with tempfile.TemporaryDirectory() as td:
            d = P(td)
            content = ''.join(json.dumps(row) + '\n' for row in self.rows).encode()
            (d / 'cmdp.jsonl').write_bytes(content + b'{"unfinished":')
            identity = {'pid': FIXTURE['pid'], 'start_ticks': 1, 'boot_id': 'fixture'}
            with self.assertRaises(R.WarmupNotReady) as caught:
                R.check_capture_readiness(d, 'arm', FIXTURE['early'], FIXTURE['used_memory'],
                                          identity, required=True)
            report = json.loads((d / 'readiness.json').read_text())
            self.assertEqual(report, caught.exception.report)
            self.assertTrue(report['retryable'])
            self.assertEqual(report['trace_prefix']['bytes'], len(content))
            self.assertEqual(report['trace_prefix']['sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(report['trace_prefix']['ignored_partial_line_bytes'], 14)
            self.assertFalse((d / 'capture-receipt.json').exists())
            passed = R.check_capture_readiness(d, 'arm', self.boundary, FIXTURE['used_memory'],
                                              identity, required=True)
            self.assertTrue(passed['passed'])

    def test_controller_stderr_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            d = P(td)
            (d / 'cmdp.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
            (d / 'cmdp.err').write_text('ioctl failed\n')
            with self.assertRaises(RuntimeError) as caught:
                R.check_capture_readiness(d, 'arm', self.boundary, FIXTURE['used_memory'],
                                          {'pid': FIXTURE['pid']}, required=True)
            self.assertNotIsInstance(caught.exception, R.WarmupNotReady)
            report = json.loads((d / 'readiness.json').read_text())
            self.assertFalse(report['passed'])
            self.assertFalse(report['retryable'])

    def test_off_has_auditable_not_required_gate(self):
        with tempfile.TemporaryDirectory() as td:
            report = R.check_capture_readiness(P(td), 'off', self.boundary,
                    FIXTURE['used_memory'], {'pid': FIXTURE['pid']}, required=False)
            self.assertTrue(report['passed'])
            self.assertFalse(report['required'])
            self.assertFalse(report['retryable'])

    def test_target_override_does_not_mutate_config(self):
        config = copy.deepcopy(R.CONFIG)
        original = R.B
        class Helper:
            @staticmethod
            def ycsb_argv(directory, workload, count, threads, cores, seconds):
                assert threads == cores == 8 and seconds == 665
                return ['java', '-Xms4g', '-Xmx4g', '-p', 'old=1'], {'maxexecutiontime': seconds}
        try:
            R.B = Helper
            for workload, target in [('A', 66000), ('B', 59000), ('C', 58000)]:
                argv, props = R.ycsb_command(P('/test'), workload, 1, 60, 600, target_qps=target)
                self.assertEqual(props['target'], target)
                self.assertIn('target=%d' % target, argv)
                self.assertEqual(props['maxexecutiontime'], 665)
                self.assertEqual(props['measurement.interval'], 'both')
            self.assertEqual(R.CONFIG, config)
        finally:
            R.B = original

    def test_failed_warmup_cleans_owned_children_and_never_receipts(self):
        # Exercise the real capture try/finally with only OS/process I/O mocked.
        # The dedicated exception must reach the driver *after* clean detach.
        with tempfile.TemporaryDirectory() as td:
            base = P(td)
            (base / 'artifacts/golden').mkdir(parents=True)
            (base / 'artifacts/golden/manifest.json').write_text(json.dumps({
                'recordcount': 11500000, 'samples': [], 'rdb_sha256': 'test'}))
            (base / 'profile.json').write_text('{}')
            clients = []
            controllers = []
            clean = {'entries': 0, 'managed': 0, 'isolated': 0, 'waiters': 0}
            class Process:
                def __init__(self, pid):
                    self.pid, self.returncode = pid, None
                def poll(self):
                    return self.returncode
                def terminate(self):
                    self.returncode = -15
                def wait(self, **kwargs):
                    return self.returncode
            redis_process = Process(100)
            class Helper:
                STATUS_RE = R.re.compile(r'start:(.+) elapsed:(\d+)')
                @staticmethod
                def mkdir(path):
                    path.mkdir(parents=True)
                @staticmethod
                def policies():
                    return {'fixed': True}
                @staticmethod
                def start_redis(*args, **kwargs):
                    return redis_process, object(), 1
                @staticmethod
                def validate_db(*args):
                    return {'info': {'used_memory': 16 * 1024 ** 3}}
                @staticmethod
                def spawn(args, directory, log, *placement):
                    timestamp = R.datetime.datetime.now(R.datetime.timezone.utc) - R.datetime.timedelta(seconds=61)
                    log.write_text('start:' + timestamp.strftime('%Y-%m-%d %H:%M:%S:%f') + ' elapsed:0\n')
                    client = Process(101)
                    clients.append(client)
                    return client
                @staticmethod
                def stop_redis(process, redis):
                    process.returncode = 0
                @staticmethod
                def ycsb_argv(directory, workload, count, threads, cores, seconds):
                    return ['java', '-p', 'old=1'], {}
            class Controller:
                def __init__(self, name, *args):
                    self.name, self.p, self.stopped = name, Process(102), False
                    controllers.append(self)
                def request(self, label):
                    pass
                def wait_snapshot(self, label):
                    return []
                def stop(self):
                    self.stopped = True
                    self.p.returncode = 143
            original_read = P.read_text
            def read_text(path, *args, **kwargs):
                if str(path).startswith('/proc/'):
                    return 'mock proc contents'
                return original_read(path, *args, **kwargs)
            def reject(directory, mode, boundary, used, identity, required=False):
                self.assertTrue(required)
                report = {'passed': False, 'retryable': True, 'required': True}
                R.save(directory / 'readiness.json', report)
                raise R.WarmupNotReady(report)
            def fake_boundary(label, controls):
                stamp = R.clock()
                return {'requested': stamp, 'completed': stamp, 'controllers': {}}
            config = dict(R.CONFIG, kernel=R.os.uname().release)
            replacements = dict(ROOT=base, STUDY=base, FREQUENCY_ROOT=base,
                    ELIGIBILITY_REQUIRED=False, CONFIG=config, B=Helper, Control=Controller,
                    frequency_policy=lambda: 2100000, frequency_reference=lambda: {},
                    idle_cmdp=lambda: clean.copy(), read_stats=lambda: clean.copy(),
                    thermal_counts=lambda: {}, selected_sources=lambda: {}, emit=lambda *a, **kw: None,
                    eligibility_gate=lambda *a: {'passed': True}, boundary=fake_boundary,
                    process_identity=lambda pid: {'pid': pid, 'boot_id': 'test', 'start_ticks': 1},
                    check_capture_readiness=reject)
            directory = base / 'failed-arm'
            with mock.patch.multiple(R, **replacements), \
                    mock.patch.object(R.os, 'geteuid', return_value=0), \
                    mock.patch.object(R.os, 'sched_setaffinity', create=True), \
                    mock.patch.object(P, 'read_text', read_text), \
                    mock.patch.object(R.shutil, 'disk_usage', return_value=type('D', (), {'free': 100 * 1024 ** 3})()):
                with self.assertRaises(R.WarmupNotReady):
                    R.capture(directory, 'B', 'arm', warm=60, duration=600,
                              telemetry=False, target_qps=59000, require_arm_ready=True)
            self.assertEqual(redis_process.poll(), 0)
            self.assertIsNotNone(clients[0].poll())
            self.assertTrue(controllers[0].stopped)
            self.assertTrue(json.loads((directory / 'cleanup.json').read_text())['passed'])
            self.assertTrue((directory / 'failure.json').exists())
            self.assertFalse((directory / 'capture-receipt.json').exists())
            self.assertFalse((directory / 'receipt.json').exists())
            self.assertEqual(json.loads((directory / 'run.json').read_text())['target_qps'], 59000)
            self.assertEqual(json.loads((directory / 'command.json').read_text())['properties']['target'], 59000)

    def test_bad_targets_rejected(self):
        for bad in (False, True, 0, -1, 1.5, '59000'):
            with self.assertRaises(ValueError):
                R.effective_target(bad)


def replay_trace(directory):
    rows = [json.loads(line) for line in (directory / 'cmdp.jsonl').read_text().splitlines()]
    boundary = json.loads((directory / 'boundaries.json').read_text())['measurement_start']
    restore = json.loads((directory / 'restore.json').read_text())
    pid = restore['redis']['pid']
    used = restore['validation']['info']['used_memory']
    report = R.arm_readiness(rows, boundary, used, pid)
    assert not report['passed'] and report['retryable'], report
    later = copy.deepcopy(FIXTURE['ready'])
    report = R.arm_readiness(rows, later, used, pid)
    assert report['passed'] and report['initial_arm_sweep']['pass_id'] == 2, report
    print(json.dumps({'real_trace_replay': str(directory), 'passed': True,
                      'first_arm_pass': 2, 'completion_margin_ns': report['completion_margin_ns']}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trace-directory', type=P)
    args, rest = p.parse_known_args()
    result = unittest.main(argv=[__file__] + rest, exit=False)
    if not result.result.wasSuccessful():
        raise SystemExit(1)
    if args.trace_directory:
        replay_trace(args.trace_directory)
