#!/usr/bin/env python3
"""Exercise frequency restoration and counter parsing without touching host policy."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('frequency', str(Path(__file__).with_name('cpu-frequency.py')))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class FrequencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sys = self.base / 'sys'
        self.sys.mkdir()
        (self.sys / 'online').write_text('0-1\n')
        (self.sys / 'cpufreq').mkdir()
        (self.sys / 'cpufreq/boost').write_text('1\n')
        for cpu in (0, 1):
            p = self.sys / 'cpufreq' / ('policy' + str(cpu))
            p.mkdir()
            values = dict(related_cpus=str(cpu), scaling_driver='acpi-cpufreq',
                          scaling_governor='ondemand', scaling_min_freq='1000000',
                          scaling_max_freq='2101000', scaling_available_frequencies='2101000 2100000 2000000 1000000',
                          scaling_available_governors='ondemand performance', boost='1')
            for name, value in values.items():
                (p / name).write_text(value + '\n')
        self.sys_patch = patch.object(f, 'SYS', self.sys)
        self.sys_patch.start()
        self.root = self.base / 'artifacts'
        self.root.mkdir()
        self.state = f.load_state(self.root)

    def tearDown(self):
        self.sys_patch.stop()
        self.tmp.cleanup()

    def test_apply_restore_preserves_original_and_reorders_bounds(self):
        before = (self.root / 'state.json').read_bytes()
        real_write = f.write
        def strict_write(path, value):
            path = Path(path)
            if path.name == 'scaling_min_freq':
                self.assertLessEqual(int(value), int(f.text(path.with_name('scaling_max_freq'))))
            if path.name == 'scaling_max_freq':
                self.assertGreaterEqual(int(value), int(f.text(path.with_name('scaling_min_freq'))))
            real_write(path, value)
        with patch.object(f, 'write', strict_write):
            f.apply(self.root, self.state, 2100000)
            self.assertEqual(f.check_locked(2100000)[0], [])
            f.apply(self.root, self.state, 1000000)
            self.assertEqual(f.check_locked(1000000)[0], [])
            f.restore_state(self.root, self.state)
        restored = f.inspect()
        self.assertEqual(restored['policies'], self.state['policies'])
        self.assertEqual(restored['boost_controls'], self.state['boost_controls'])
        self.assertEqual(before, (self.root / 'state.json').read_bytes())

    def test_partial_apply_failure_rolls_back_all_policies(self):
        real_write = f.write
        failed = [False]
        def fail_once(path, value):
            if 'policy1/scaling_min_freq' in str(path) and not failed[0]:
                failed[0] = True
                raise OSError('injected write failure')
            real_write(path, value)
        with patch.object(f, 'write', fail_once):
            with self.assertRaises(OSError):
                f.apply(self.root, self.state, 2100000)
        self.assertTrue(failed[0])
        restored = f.inspect()
        self.assertEqual(restored['policies'], self.state['policies'])
        self.assertEqual(restored['boost_controls'], self.state['boost_controls'])

    def test_global_boost_propagates_and_local_writes_are_rejected_when_off(self):
        real_write = f.write
        global_boost = self.sys / 'cpufreq/boost'
        def kernel_boost_write(path, value):
            path = Path(path)
            if path.name == 'boost' and path != global_boost and f.text(global_boost) == '0':
                raise OSError(22, 'local boost write while global boost disabled')
            real_write(path, value)
            if path == global_boost:
                for cpu in (0, 1):
                    real_write(self.sys / 'cpufreq' / ('policy' + str(cpu)) / 'boost', value)
        with patch.object(f, 'write', kernel_boost_write):
            f.apply(self.root, self.state, 2100000)
            self.assertEqual(f.check_locked(2100000)[0], [])
            f.apply(self.root, self.state, 2000000)
            f.restore_state(self.root, self.state)
        self.assertEqual(f.inspect()['boost_controls'], self.state['boost_controls'])

    def test_turbo_sentinel_rejected_before_writes(self):
        with patch.object(f, 'write') as write:
            with self.assertRaises(RuntimeError):
                f.apply(self.root, self.state, 2101000)
            write.assert_not_called()

    def test_wrong_boot_snapshot_rejected(self):
        value = dict(self.state, boot_id='different')
        f.save(self.root / 'state.json', value)
        with self.assertRaisesRegex(RuntimeError, 'boot_id'):
            f.load_state(self.root)

    def test_turbostat_missing_package_cells_and_idle_samples(self):
        path = self.base / 'turbostat.tsv'
        path.write_text('CPU Busy% Bzy_MHz CoreTmp PkgTmp\n'
                        '- 99 2100 55 56\n0 99.5 2101 55 56\n1 99.8 2099 52\n'
                        'CPU Busy% Bzy_MHz CoreTmp PkgTmp\n0 0 100 50 52\n1 99 2200 53\n')
        data = f.parse_turbostat(path, [0, 1], 2100000)
        self.assertEqual(data[0]['samples'], 2)
        self.assertEqual(data[0]['eligible'], 1)
        self.assertEqual(data[0]['in_band_fraction'], 1.0)
        self.assertEqual(data[1]['eligible'], 2)
        self.assertEqual(data[1]['in_band_fraction'], 0.5)

    def test_thermal_errors_use_deltas_and_explicit_limits(self):
        before = dict(counts={'thermal': 2}, sensors={'cpu': dict(value_millic=50000, limit_millic=85000, alarm=False)})
        self.assertEqual(f.health_errors(before, before), [])
        after = dict(counts={'thermal': 3}, sensors={'cpu': dict(value_millic=85000, limit_millic=85000, alarm=False)})
        self.assertEqual(len(f.health_errors(before, after)), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
