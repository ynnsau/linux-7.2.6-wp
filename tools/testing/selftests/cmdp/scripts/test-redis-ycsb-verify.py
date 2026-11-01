#!/usr/bin/env python3
"""Pure validation and failure-cleanup tests; no Redis or CMD-P is started."""
import copy
import importlib.util
import json
import os
import pathlib
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('verify', str(HERE / 'redis-ycsb-verify.py'))
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)


def rows(mode='sampling'):
    pages = 16 * V.GIB // V.PAGE
    status = {'event': 'status', 'state': 2, 'resource_failures': 0,
              'reports_dropped': 0, 'first_arms': 0, 'rearms': 0,
              'active': 0, 'pre_stop_active': 0, 'terminals': {'write': 0}}
    cohort = {'event': 'sweep', 'pass_id': 1, 'error': 0, 'complete': 1,
              'eligible_pages': pages, 'skips': {'thp_policy': 0, 'resource': 0}}
    dirty = {'event': 'dirty_status', 'resource_skips': 0, 'samples': pages}
    batch = {'event': 'batch_status', 'wp_pages': 0, 'dirty_pages': pages,
             'flush_calls': 8192}
    final = copy.deepcopy(status)
    final.update(event='final', state=4)
    return [status, cohort, dirty, batch, final]


class MonitoringTests(unittest.TestCase):
    def test_full_dataset_sampling_passes(self):
        result = V.monitoring_summary(rows(), 'sampling', 16 * V.GIB)
        self.assertEqual(result['eligible_bytes_max_complete_scan'], 16 * V.GIB)
        self.assertEqual(result['live_arm_peak_bytes_observed'], 0)

    def test_old_seven_page_regression_is_rejected(self):
        data = rows()
        data[1]['eligible_pages'] = data[2]['samples'] = 7
        with self.assertRaisesRegex(RuntimeError, 'substantial dataset'):
            V.monitoring_summary(data, 'sampling', 16 * V.GIB)

    def test_incomplete_scan_is_rejected(self):
        data = rows()
        data[1]['complete'] = 0
        with self.assertRaisesRegex(RuntimeError, 'No complete'):
            V.monitoring_summary(data, 'sampling', 16 * V.GIB)

    def test_thp_policy_exclusion_is_rejected(self):
        data = rows()
        data[1]['skips']['thp_policy'] = 1
        with self.assertRaisesRegex(RuntimeError, 'THP policy'):
            V.monitoring_summary(data, 'sampling', 16 * V.GIB)

    def test_sampling_arm_contamination_is_rejected(self):
        data = rows()
        data[0]['first_arms'] = 1
        with self.assertRaisesRegex(RuntimeError, 'performed ARM or WP'):
            V.monitoring_summary(data, 'sampling', 16 * V.GIB)

    def test_missing_sampling_work_is_rejected(self):
        data = rows()
        data[2]['samples'] = 7
        with self.assertRaisesRegex(RuntimeError, 'Dirty sampling did not'):
            V.monitoring_summary(data, 'sampling', 16 * V.GIB)

    def test_no_live_arm_floor_under_workload(self):
        result = V.monitoring_summary(rows(), 'arm', 16 * V.GIB)
        self.assertEqual(result['live_arm_peak_bytes_observed'], 0)
        self.assertIn('no workload minimum', result['arm_coverage_requirement'])

    def test_cleanup_and_resource_failures_rejected(self):
        for index, field in [(0, 'resource_failures'), (0, 'reports_dropped'), (4, 'active')]:
            data = rows()
            data[index][field] = 1
            with self.assertRaises(RuntimeError):
                V.monitoring_summary(data, 'arm', 16 * V.GIB)


class CleanupTests(unittest.TestCase):
    def test_failed_eligibility_stops_owned_redis_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            golden = root / 'artifacts/golden'
            golden.mkdir(parents=True)
            (golden / 'manifest.json').write_text(json.dumps(
                {'recordcount': 11500000, 'samples': {}, 'rdb_sha256': 'fixture'}))
            calls = []
            identity = {'pid': 10, 'start_ticks': 20, 'boot_id': 'test'}
            process = types.SimpleNamespace(pid=10, poll=lambda: None)
            connection = object()
            def save(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            def gate(*args):
                raise RuntimeError('intentional eligibility failure')
            def idle():
                calls.append('ownership-check')
                return dict(entries=0, managed=0, isolated=0, waiters=0)
            helper = types.SimpleNamespace(
                mkdir=lambda path: path.mkdir(parents=True), policies=lambda: {'test': True},
                start_redis=lambda *a, **k: (process, connection, 1),
                validate_db=lambda *a: {'info': {'used_memory': 16 * V.GIB}},
                stop_redis=lambda *a: calls.append('stop-redis'))
            runner = types.SimpleNamespace(
                ROOT=root, STUDY=root, B=helper, now=lambda: 'test', save=save,
                CONFIG={'kernel': os.uname().release},
                frequency_policy=lambda: 2100000, idle_cmdp=idle,
                selected_sources=lambda: {}, process_identity=lambda pid: identity,
                emit=lambda *a, **k: None, eligibility_gate=gate)
            with self.assertRaisesRegex(RuntimeError, 'intentional eligibility failure'):
                V.idle_check(runner, root / 'idle-arm-v1')
            self.assertEqual(calls, ['ownership-check', 'stop-redis', 'ownership-check'])
            report = json.loads((root / 'idle-arm-v1/idle-verification.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['cleanup_errors'], [])
            self.assertIn('intentional eligibility failure', report['failure']['error'])


if __name__ == '__main__':
    unittest.main()
