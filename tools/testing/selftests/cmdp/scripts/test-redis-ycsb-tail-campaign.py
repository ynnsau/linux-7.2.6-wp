#!/usr/bin/env python3
"""Focused scheduler/recovery tests; no server or kernel access."""
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

P = pathlib.Path
spec = importlib.util.spec_from_file_location('tail_campaign', str(P(__file__).with_name('redis-ycsb-tail-campaign.py')))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


class NotReady(Exception):
    def __init__(self):
        self.report = {'required': True, 'passed': False, 'retryable': True}
        super().__init__('initial ARM sweep not complete')


class FakeRunner:
    WarmupNotReady = NotReady

    def __init__(self, root):
        self.ROOT = root
        (root / 'campaign-v1').mkdir()
        self.calls = []
        self.live = {}
        self.failure = None
        self.not_ready_warmups = []
        self.config_override = {}

    @staticmethod
    def now():
        return 'time'

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def emit(self, *args, **kwargs):
        pass

    def process_identity(self, pid):
        if pid == os.getpid():
            return {'pid': pid, 'start_ticks': 1, 'boot_id': 'boot'}
        if pid in self.live:
            return self.live[pid]
        raise FileNotFoundError()

    def idle_cmdp(self):
        return dict(entries=0, managed=0, isolated=0, waiters=0)

    def verify_capture_receipt(self, directory):
        receipt = C.read(directory / 'capture-receipt.json')
        assert receipt['passed']
        for name, digest in receipt['files_sha256'].items():
            assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest

    def capture(self, directory, workload, mode, **kwargs):
        self.calls.append((str(directory), workload, mode, kwargs))
        directory.mkdir()
        self.save(directory / 'processes.json', {'redis': {'pid': 999999, 'start_ticks': 2, 'boot_id': 'boot'}, 'controls': {}})
        self.save(directory / 'cleanup.json', {'passed': True})
        if self.failure:
            failure = self.failure
            self.failure = None
            raise failure
        if mode == 'arm' and kwargs['warm'] in self.not_ready_warmups:
            raise NotReady()
        run = dict(workload=workload, mode=mode, warmup_seconds=kwargs['warm'],
                   target_qps=kwargs['target_qps'], workers=8, measure_seconds=kwargs['duration'],
                   frequency_contract='fixed-policy')
        run.update(self.config_override)
        self.save(directory / 'run.json', run)
        self.save(directory / 'eligibility.json', {'passed': True})
        self.save(directory / 'readiness.json', {'required': mode == 'arm', 'passed': True, 'retryable': False})
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        self.save(directory / 'capture-receipt.json', {'passed': True, 'files_sha256': files})


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = FakeRunner(P(self.temp.name))
        self.block = list(C.schedule())[0]

    def tearDown(self):
        self.temp.cleanup()

    def test_rotating_schedule_is_exactly_27_unique_authorized_runs(self):
        blocks = list(C.schedule())
        self.assertEqual([x['workload'] for x in blocks], list('ABCBCACAB'))
        self.assertEqual([blocks[n]['modes'] for n in (0, 3, 6)],
                         [['off', 'sampling', 'arm'], ['sampling', 'arm', 'off'], ['arm', 'off', 'sampling']])
        pairs = [(b['workload'], b['repetition'], m) for b in blocks for m in b['modes']]
        self.assertEqual(len(set(pairs)), 27)

    def test_readiness_retry_preserves_attempt_and_repeats_all_modes_same_warmup(self):
        self.f.not_ready_warmups = [60]
        C.run_block(self.f, self.block)
        self.assertEqual([(c[2], c[3]['warm']) for c in self.f.calls],
                         [('off', 60), ('sampling', 60), ('arm', 60),
                          ('off', 120), ('sampling', 120), ('arm', 120)])
        entry = C.selected(self.f)['blocks']['A-r1']
        self.assertEqual(entry['warmup_seconds'], 120)
        first = self.f.ROOT / 'attempts/A-r1/a01-w060'
        self.assertEqual(C.read(first / 'attempt.json')['status'], 'warmup_insufficient')
        self.assertTrue((first / 'A-off-r1/capture-receipt.json').exists())
        self.assertTrue(all(p.resolve().parent.name == 'a02-w120' for p in (self.f.ROOT / 'campaign-v1').iterdir()))

    def test_hard_failure_does_not_retry_or_accept_partial_block(self):
        self.f.failure = RuntimeError('eligibility failure')
        with self.assertRaises(RuntimeError):
            C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(C.selected(self.f)['blocks'], {})
        self.assertEqual(list((self.f.ROOT / 'campaign-v1').iterdir()), [])

    def test_resume_after_interruption_preserves_old_attempt_and_repeats_block(self):
        self.f.failure = KeyboardInterrupt('SIGTERM')
        with self.assertRaises(KeyboardInterrupt):
            C.run_block(self.f, self.block)
        previous = self.f.ROOT / 'attempts/A-r1/a01-w060'
        self.assertEqual(C.read(previous / 'attempt.json')['status'], 'interrupted')
        old = (previous / 'attempt.json').read_bytes()
        C.run_block(self.f, self.block)
        self.assertEqual((previous / 'attempt.json').read_bytes(), old)
        self.assertEqual(len(self.f.calls), 4)
        self.assertTrue(C.selected(self.f)['blocks']['A-r1']['attempt'].endswith('a02-w060'))

    def test_live_identity_blocks_resume(self):
        self.f.failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            C.run_block(self.f, self.block)
        self.f.live[999999] = {'pid': 999999, 'start_ticks': 2, 'boot_id': 'boot'}
        with self.assertRaisesRegex(AssertionError, 'still owns live process'):
            C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 1)

    def test_complete_resume_verifies_receipts_and_does_not_recapture(self):
        C.run_block(self.f, self.block)
        C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 3)
        path = self.f.ROOT / 'campaign-v1/A-off-r1/run.json'
        path.write_text('{}')
        with self.assertRaises(AssertionError):
            C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 3)

    def test_resume_completes_partial_promotion_without_recapturing(self):
        original = P.symlink_to
        count = []

        def interrupted_link(path, target, target_is_directory=False):
            original(path, target, target_is_directory=target_is_directory)
            count.append(path)
            if len(count) == 1:
                raise KeyboardInterrupt('stop during promotion')

        with mock.patch.object(P, 'symlink_to', interrupted_link):
            with self.assertRaises(KeyboardInterrupt):
                C.run_block(self.f, self.block)
        attempt = self.f.ROOT / 'attempts/A-r1/a01-w060'
        self.assertEqual(C.read(attempt / 'attempt.json')['status'], 'captured')
        self.assertEqual(len(list((self.f.ROOT / 'campaign-v1').iterdir())), 1)
        C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 3)
        self.assertEqual(len(list((self.f.ROOT / 'campaign-v1').iterdir())), 3)
        self.assertEqual(C.selected(self.f)['blocks']['A-r1']['attempt'], 'attempts/A-r1/a01-w060')

    def test_maximum_warmup_fails_closed(self):
        self.f.not_ready_warmups = C.WARMUPS
        with self.assertRaisesRegex(AssertionError, 'maximum common warmup'):
            C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 12)
        self.assertFalse(C.selected(self.f)['blocks'])
        with self.assertRaisesRegex(AssertionError, 'maximum common warmup'):
            C.run_block(self.f, self.block)
        self.assertEqual(len(self.f.calls), 12)

    def test_capture_target_mismatch_never_promoted(self):
        self.f.config_override = {'target_qps': 70000}
        with self.assertRaisesRegex(AssertionError, 'does not match'):
            C.run_block(self.f, self.block)
        self.assertFalse(C.selected(self.f)['blocks'])

    def test_sigterm_raises_cleanup_compatible_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            C.interrupted(15, None)


if __name__ == '__main__':
    unittest.main()
