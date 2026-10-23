#!/usr/bin/env python3
"""Read-only schedule, comparison and executor-contract checks; no CMD-P access."""
import copy
import importlib.util
import json
from pathlib import Path
import statistics
import types
import unittest

PATH = Path(__file__).resolve().with_name('run-fused-batch-sweep.py')
spec = importlib.util.spec_from_file_location('sweep_under_test', str(PATH))
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)


def synthetic():
    rows = []
    for i, k in enumerate(sweep.SIZES):
        for threads in (1, 4):
            for block, off in enumerate((80., 120., 200.), 1):
                ratio = 1 + (7 - i + threads / 10 + block / 100) / 100
                for mode in 'ABC':
                    elapsed = off * (ratio if mode == 'C' else 1)
                    rows.append(dict(dirty_batch_pages=k, wp_batch_pages=k, threads=threads,
                        block=block, mode=mode, elapsed_seconds=elapsed,
                        final={'protected_byte_ms': elapsed * 1000 * 2**30 * 5}))
    return rows


class SweepTests(unittest.TestCase):
    def check_schedule(self, pairs):
        entries = sweep.schedule(pairs)
        self.assertEqual(len(entries), len(pairs) * 18)
        identities = [(e['dirty_batch_pages'], e['wp_batch_pages'], e['block'], e['cell'], e['mode'])
                      for e in entries]
        self.assertEqual(len(identities), len(set(identities)))
        for pair in pairs:
            for cell in ('bfs-t1', 'bfs-t4'):
                rows = [e for e in entries if (e['dirty_batch_pages'], e['wp_batch_pages']) == pair
                        and e['cell'] == cell]
                self.assertEqual(len(rows), 9)
                for block, order in enumerate(('ABC', 'BCA', 'CAB'), 1):
                    self.assertEqual(''.join(e['mode'] for e in rows if e['block'] == block), order)
        return entries

    def test_equal_matrix_has_three_matched_blocks(self):
        self.check_schedule([(k, k) for k in sweep.SIZES])

    def test_asymmetric_matrices_keep_window_and_balance_positions(self):
        for k in sweep.SIZES:
            pairs = sweep.stage_two_pairs(k)
            self.assertEqual({max(pair) for pair in pairs}, {k})
            entries = self.check_schedule(pairs)
            for pair in pairs:
                positions = []
                for block in (1, 2, 3):
                    order = []
                    for e in entries:
                        key = (e['dirty_batch_pages'], e['wp_batch_pages'])
                        if e['block'] == block and key not in order:
                            order.append(key)
                    positions.append(order.index(pair))
                self.assertEqual(sorted(positions), [0, 1, 2])

    def test_invalid_configurations_fail(self):
        for pairs in [[(0, 64)] * 3, [(65537, 64), (64, 64), (64, 8)], [(64, 64)] * 5]:
            with self.assertRaises(AssertionError):
                sweep.schedule(pairs)

    def test_selection_uses_paired_ratios_and_both_thread_cells(self):
        data = synthetic()
        selected = sweep.select_stage_two(data)
        self.assertEqual(selected['selected_k'], 65536)
        for e in selected['estimates']:
            expected = []
            for t in (1, 4):
                subset = [r for r in data if r['dirty_batch_pages'] == e['k'] and r['threads'] == t]
                ratios = []
                for block in (1, 2, 3):
                    a = next(r for r in subset if r['block'] == block and r['mode'] == 'A')
                    c = next(r for r in subset if r['block'] == block and r['mode'] == 'C')
                    ratios.append(100 * (c['elapsed_seconds'] / a['elapsed_seconds'] - 1))
                expected.append(statistics.median(ratios))
                self.assertAlmostEqual(e['cells'][str(t)]['median_mean_live_arm_gib'], 5.)
            self.assertAlmostEqual(e['score_pct'], statistics.mean(expected))

    def test_missing_duplicate_or_invalid_receipts_fail(self):
        data = synthetic()
        for bad in [data[:-1], data[:-1] + [data[0]]]:
            with self.assertRaises(AssertionError):
                sweep.select_stage_two(bad)
        bad = copy.deepcopy(data)
        bad[0]['elapsed_seconds'] = 0
        with self.assertRaises(AssertionError):
            sweep.select_stage_two(bad)

    def test_exact_tie_prefers_smaller_ceiling(self):
        data = synthetic()
        for r in data:
            r['elapsed_seconds'] = 100 if r['mode'] != 'C' else 110
        self.assertEqual(sweep.select_stage_two(data)['selected_k'], 64)

    def test_existing_executor_and_measurement_boundaries_are_inherited(self):
        for name in ('execute', 'workload', 'observe', 'sample', 'warm', 'check_workers', 'guard'):
            self.assertIs(getattr(sweep.Campaign, name), getattr(sweep.matched.Campaign, name))

    def test_caps_only_reach_monitor_enabled_sessions(self):
        c = object.__new__(sweep.Campaign)
        c.a = types.SimpleNamespace(dirty_batch_pages=65536, wp_batch_pages=8192)
        c.monitor = Path('/unused/monitor')
        self.assertEqual(c.controller_args('A'), [c.monitor, '--monitor-off'])
        for mode in 'BC':
            args = c.controller_args(mode)
            self.assertEqual(args[args.index('--dirty-batch-pages') + 1], '65536')
            self.assertEqual(args[args.index('--wp-batch-pages') + 1], '8192')
            self.assertEqual(args[args.index('--period-ms') + 1], '10000')
            self.assertEqual(args[args.index('--quiet-ms') + 1], '10000')

    def test_selection_survives_json_roundtrip(self):
        a = sweep.select_stage_two(synthetic())
        b = sweep.select_stage_two(json.loads(json.dumps(synthetic())))
        self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main(verbosity=2)
