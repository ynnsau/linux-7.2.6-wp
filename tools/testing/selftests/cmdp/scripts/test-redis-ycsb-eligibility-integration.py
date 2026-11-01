#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Mock-only integration regressions; never launch Redis/YCSB or touch policies."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
              str(Path(__file__).with_name(name + '.py')))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(
            pw_uid=os.getuid(), pw_gid=os.getgid())):
        spec.loader.exec_module(module)
    return module


class Integration(unittest.TestCase):
    def test_target_and_child_options(self):
        r = load('redis-ycsb-fixedfreq')
        r.ROOT = Path('/new/evidence')
        r.FREQUENCY_ROOT = Path('/old/frequency')
        r.ELIGIBILITY_REQUIRED = True
        r.CONFIG['target_qps'] = 50000
        helper = mock.Mock()
        helper.ycsb_argv.return_value = (
            ['java', '-Xms4g', '-Xmx4g', '-threads', '8', '-p', 'old=1'],
            {'measurementtype': 'hdrhistogram', 'hdrhistogram.fileoutput': 'true'})
        r.B = helper
        argv, props = r.ycsb_command(Path('/example'), 'B', 11500000, 20, 120)
        helper.ycsb_argv.assert_called_once_with(Path('/example'), 'B', 11500000,
                                               8, 8, seconds=145)
        self.assertIn('target=50000', argv)
        self.assertEqual(props['target'], 50000)
        self.assertIn('-Xms12g', argv)
        self.assertIn('-Xmx12g', argv)
        self.assertEqual(props['measurement.interval'], 'both')
        self.assertEqual(r.runtime_options(), ['--output-root', '/new/evidence',
                         '--frequency-profile-root', '/old/frequency', '--target-qps', '50000'])

    def test_historical_root_refuses_mutating_actions(self):
        for action in ('prepare', 'pilot', 'run', 'resume', 'suite'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                r = load('redis-ycsb-fixedfreq')
                r.ROOT = r.LEGACY_ROOT = Path(td) / 'historical'
                r.load_helper = mock.Mock()
                with mock.patch.object(sys, 'argv', ['runner', action]):
                    with self.assertRaisesRegex(RuntimeError, 'Historical.*read-only'):
                        r.main()
                r.load_helper.assert_not_called()
                self.assertFalse(r.ROOT.exists())

    def test_loaded_verification_root_refuses_generic_workload_actions(self):
        for action in ('pilot', 'run', 'resume', 'suite'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                r = load('redis-ycsb-fixedfreq')
                root = Path(td) / 'verification-only'
                root.mkdir()
                config = json.dumps({'verification_only': True, 'target_qps': 50000,
                                     'frequency_contract': 'fixed-policy',
                                     'workloads': ['B'], 'repetitions': 1,
                                     'measure_seconds': 120})
                (root / 'config.json').write_text(config)
                r.load_helper = mock.Mock()
                r.bootstrap_tools = mock.Mock()
                r.capture = mock.Mock()
                r.suite = mock.Mock()
                with mock.patch.object(sys, 'argv', ['runner', action, '--output-root', str(root)]):
                    with self.assertRaisesRegex(RuntimeError, 'Verification-only root cannot run'):
                        r.main()
                for function in (r.load_helper, r.bootstrap_tools, r.capture, r.suite):
                    function.assert_not_called()
                self.assertEqual((root / 'config.json').read_text(), config)
                self.assertEqual([p.name for p in root.iterdir()], ['config.json'])

    def test_bootstrap_preserves_frozen_and_historical_tools(self):
        r = load('redis-ycsb-fixedfreq')
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            r.ROOT = base / 'new'
            r.LEGACY_ROOT = r.FREQUENCY_ROOT = base / 'old'
            r.SCRIPTS = base / 'source/scripts'
            r.HELPER = r.ROOT / 'tools/redis-ycsb-study.py'
            r.SCRIPTS.mkdir(parents=True)
            (r.FREQUENCY_ROOT / 'bin').mkdir(parents=True)
            (r.SCRIPTS / 'redis-ycsb-study.py').write_text('new helper')
            (r.SCRIPTS.parent / 'cmdp_exec').write_text('new launcher')
            for name in ('cmdp_monitor', 'frequency_collect', 'redis-ycsb-study.py'):
                (r.FREQUENCY_ROOT / 'bin' / name).write_text('historical ' + name)
            original = {str(p): p.read_bytes() for p in r.FREQUENCY_ROOT.rglob('*') if p.is_file()}
            with mock.patch.object(r.os, 'geteuid', return_value=0):
                r.bootstrap_tools()
                r.bootstrap_tools()
                self.assertEqual(r.HELPER.read_text(), 'new helper')
                self.assertEqual(r.HELPER_SHA, r.sha(r.HELPER))
                (r.SCRIPTS / 'redis-ycsb-study.py').write_text('changed after freezing')
                with self.assertRaisesRegex(AssertionError, 'Frozen tool differs'):
                    r.bootstrap_tools()
            self.assertEqual(r.HELPER.read_text(), 'new helper')
            self.assertEqual(original, {str(p): p.read_bytes()
                             for p in r.FREQUENCY_ROOT.rglob('*') if p.is_file()})

    def test_failed_inventory_blocks_all_modes_before_monitor_and_ycsb(self):
        for mode in ('off', 'sampling', 'arm'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                r = load('redis-ycsb-fixedfreq')
                r.ROOT = Path(td) / 'new'
                r.STUDY = Path(td) / 'study'
                r.FREQUENCY_ROOT = Path(td) / 'frequency'
                r.ELIGIBILITY_REQUIRED = True
                r.CONFIG['target_qps'] = 50000
                r.ROOT.mkdir()
                r.FREQUENCY_ROOT.mkdir()
                (r.ROOT / 'source-manifest.json').write_text('{"hashes": {}}')
                (r.FREQUENCY_ROOT / 'profile.json').write_text('{}')
                golden = r.STUDY / 'artifacts/golden'
                golden.mkdir(parents=True)
                (golden / 'manifest.json').write_text(json.dumps(
                    {'recordcount': 11500000, 'samples': {}, 'rdb_sha256': 'golden-sha'}))
                helper = mock.Mock()
                helper.mkdir.side_effect = lambda path: path.mkdir(parents=True, exist_ok=True)
                process, redis = mock.Mock(pid=4321), mock.Mock()
                process.poll.return_value = None
                helper.start_redis.return_value = (process, redis, 1.0)
                helper.validate_db.return_value = {'info': {'used_memory': 16 * 1024 ** 3}}
                helper.policies.return_value = {}
                r.B = helper
                r.frequency_policy = mock.Mock(return_value=2100000)
                r.frequency_reference = mock.Mock(return_value={})
                r.thermal_counts = mock.Mock(return_value={})
                r.idle_cmdp = mock.Mock(return_value=dict(entries=0, managed=0, isolated=0, waiters=0))
                r.read_stats = mock.Mock(return_value=dict(entries=0, managed=0, isolated=0, waiters=0))
                r.selected_sources = mock.Mock(return_value={})
                r.process_identity = mock.Mock(return_value={'pid': process.pid, 'start_ticks': 1, 'boot_id': 'mock'})
                r.emit = mock.Mock()
                r.eligibility_gate = mock.Mock(side_effect=RuntimeError('inventory rejected'))
                r.Control = mock.Mock(side_effect=AssertionError('must not attach real monitor'))
                original_read = Path.read_text
                def read(path, *args, **kwargs):
                    return 'mock proc snapshot' if str(path).startswith('/proc/') else original_read(path, *args, **kwargs)
                directory = r.ROOT / ('B-' + mode)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(r.os, 'geteuid', return_value=0))
                    stack.enter_context(mock.patch.object(r.os, 'sched_setaffinity'))
                    stack.enter_context(mock.patch.object(r.os, 'uname', return_value=SimpleNamespace(release=r.CONFIG['kernel'])))
                    stack.enter_context(mock.patch.object(r.shutil, 'disk_usage', return_value=SimpleNamespace(free=64 * 1024 ** 3)))
                    stack.enter_context(mock.patch.object(Path, 'read_text', new=read))
                    with self.assertRaisesRegex(RuntimeError, 'inventory rejected'):
                        r.capture(directory, 'B', mode, warm=20, duration=120)
                self.assertEqual(helper.start_redis.call_args[1]['exec_prefix'], [r.ROOT / 'bin/cmdp_exec', '--'])
                r.eligibility_gate.assert_called_once()
                r.Control.assert_not_called()
                helper.spawn.assert_not_called()
                helper.stop_redis.assert_called_once_with(process, redis)
                self.assertEqual(json.loads((directory / 'run.json').read_text())['target_qps'], 50000)
                self.assertIn('inventory rejected', (directory / 'failure.json').read_text())

    def test_startup_failure_cleans_owned_child_and_socket(self):
        h = load('redis-ycsb-study')
        with tempfile.TemporaryDirectory() as td:
            directory, database = Path(td) / 'run', Path(td) / 'db'
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired('redis', 30), 0]
            redis = mock.Mock()
            redis.info.return_value = {'loading': 0, 'process_id': 9999}
            with mock.patch.object(h.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                 mock.patch.object(h, 'spawn', return_value=process) as spawn, \
                 mock.patch.object(h, 'Redis', return_value=redis):
                with self.assertRaises(AssertionError):
                    h.start_redis(directory, database, exec_prefix=['/test/cmdp_exec', '--'])
            self.assertEqual(spawn.call_args[0][0][:2], ['/test/cmdp_exec', '--'])
            redis.close.assert_called_once()
            process.terminate.assert_called_once()
            process.kill.assert_called_once()

    def test_occupied_port_is_never_stopped_or_replaced(self):
        h = load('redis-ycsb-study')
        with tempfile.TemporaryDirectory() as td:
            sock = mock.Mock()
            with mock.patch.object(h.socket, 'create_connection', return_value=sock), \
                 mock.patch.object(h, 'spawn') as spawn:
                with self.assertRaisesRegex(RuntimeError, 'occupied'):
                    h.start_redis(Path(td) / 'run', Path(td) / 'db')
            sock.close.assert_called_once()
            spawn.assert_not_called()

    def test_shutdown_socket_failure_terminates_child_and_preserves_error(self):
        h = load('redis-ycsb-study')
        process, redis = mock.Mock(pid=1234), mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('redis', 30), 0]
        original = RuntimeError('broken owned socket')
        redis.info.side_effect = original
        with self.assertRaises(RuntimeError) as error:
            h.stop_redis(process, redis)
        self.assertIs(error.exception, original)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        redis.close.assert_called_once()
        redis.call.assert_not_called()

    def test_shutdown_never_uses_unrelated_socket_identity(self):
        h = load('redis-ycsb-study')
        process, redis = mock.Mock(pid=1234), mock.Mock()
        process.poll.return_value = None
        redis.info.return_value = {'process_id': 9999}
        with self.assertRaises(AssertionError):
            h.stop_redis(process, redis)
        redis.call.assert_not_called()
        process.terminate.assert_called_once()
        redis.close.assert_called_once()

    def test_clean_shutdown_and_exited_process_need_no_signals(self):
        for alive in (True, False):
            with self.subTest(alive=alive):
                h = load('redis-ycsb-study')
                process, redis = mock.Mock(pid=1234), mock.Mock()
                process.poll.side_effect = [None, 0] if alive else [0, 0]
                redis.info.return_value = {'process_id': 1234}
                redis.call.side_effect = EOFError
                h.stop_redis(process, redis)
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                redis.close.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
