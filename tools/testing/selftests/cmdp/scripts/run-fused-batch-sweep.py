#!/usr/bin/env python3
"""Two-stage BFS batch sweep using the unchanged matched timing executor.

Stage one: equal 64, 512, 4096, 16384 and 65536 page ceilings (90 runs).
Stage two: K/K, K/(K/8), (K/8)/K (54 fresh runs), with K selected from stage
one by the mean of the one/four-thread median paired full-mode overheads.
Low overhead is the primary design objective; substantial ARM coverage remains
important and is reported, without a 95% coverage-retention requirement.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import statistics

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('batch_sweep_fused', str(SCRIPTS / 'run-fused-overhead.py'))
fused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fused)
matched = fused.matched
SIZES = (64, 512, 4096, 16384, 65536)
GOAL = ('Minimize end-to-end overhead while retaining substantial ARM coverage. '
        'Coverage is a secondary design objective, not a fixed 95% retention constraint.')
SELECTION = ('Equal weight for one/four threads: arithmetic mean of the three-repeat '
             'median paired C/A overheads. Select the smallest score, then smaller K '
             'for an exact tie. This selects an exploratory follow-up, not a final default.')


def pair_key(pair):
    return 'd{}-w{}'.format(*pair)


def schedule(pairs):
    """Vary cap order; each cell uses each mode position exactly once."""
    assert len(pairs) in (3, 5) and len(set(pairs)) == len(pairs)
    assert all(1 <= x <= 65536 for pair in pairs for x in pair)
    n = len(pairs)
    orders = ([0, 1, 2, 3, 4], [4, 3, 2, 1, 0], [2, 4, 0, 3, 1]) if n == 5 else (
        [0, 1, 2], [1, 2, 0], [2, 0, 1])
    result = []
    for block, (indices, modes) in enumerate(zip(orders, ('ABC', 'BCA', 'CAB')), 1):
        cells = ('bfs-t1', 'bfs-t4') if block % 2 else ('bfs-t4', 'bfs-t1')
        for index in indices:
            dirty, wp = pairs[index]
            for cell in cells:
                for mode in modes:
                    result.append(dict(block=block, cell=cell, mode=mode,
                                       dirty_batch_pages=dirty, wp_batch_pages=wp))
    return result


def stage_two_pairs(k):
    assert k in SIZES
    return [(k, k), (k, k // 8), (k // 8, k)]


def select_stage_two(measured):
    estimates = []
    for k in SIZES:
        cells = {}
        for threads in (1, 4):
            rows = [r for r in measured if r['dirty_batch_pages'] == k and
                    r['wp_batch_pages'] == k and r['threads'] == threads]
            assert len(rows) == 9
            grouped = {}
            for row in rows:
                key = (row['block'], row['mode'])
                assert key not in grouped and row['elapsed_seconds'] > 0
                grouped[key] = row
            assert set(grouped) == {(b, m) for b in (1, 2, 3) for m in 'ABC'}
            ratios, coverage = [], []
            for block in (1, 2, 3):
                off, full = grouped[block, 'A'], grouped[block, 'C']
                ratios.append(100 * (full['elapsed_seconds'] / off['elapsed_seconds'] - 1))
                coverage.append(full['final']['protected_byte_ms'] / 1000 / 2**30 /
                                full['elapsed_seconds'])
            cells[str(threads)] = dict(full_vs_off_pct=ratios,
                median_full_vs_off_pct=statistics.median(ratios),
                mean_live_arm_gib=coverage, median_mean_live_arm_gib=statistics.median(coverage))
        estimates.append(dict(k=k, cells=cells, score_pct=statistics.mean(
            cells[t]['median_full_vs_off_pct'] for t in ('1', '4'))))
    baseline = estimates[0]
    for estimate in estimates:
        estimate['coverage_flags'] = []
        for threads in ('1', '4'):
            cell = estimate['cells'][threads]
            reference = baseline['cells'][threads]['median_mean_live_arm_gib']
            value = cell['median_mean_live_arm_gib']
            cell['coverage_vs_fresh_64_ratio'] = value / reference if reference else None
            if value < 2 or (reference and value < .5 * reference):
                estimate['coverage_flags'].append(dict(threads=int(threads), mean_live_arm_gib=value,
                    reason='Below 2 GiB or below half of fresh 64/64; review before recommending a default.'))
    chosen = min(estimates, key=lambda r: (r['score_pct'], r['k']))
    return dict(rule=SELECTION, selected_k=chosen['k'], estimates=estimates,
                pairs=stage_two_pairs(chosen['k']), selection_is_exploratory=True,
                coverage_rule='Report absolute coverage and its change; no 95% retention threshold.',
                selected_coverage_flags=chosen['coverage_flags'],
                coverage_flags_are_advisory=True)


class Campaign(fused.Campaign):
    def __init__(self, a):
        self.stage_name = 'preflight'
        self.active_pair = (64, 64)
        super().__init__(a)

    def state(self, stage, **extra):
        extra.update(sweep_stage=self.stage_name, dirty_batch_pages=self.active_pair[0],
                     wp_batch_pages=self.active_pair[1], measured_total=144)
        extra['measured_completed'] = sum(name.startswith('measured/') for name in self.summaries)
        super().state(stage, **extra)

    def set_pair(self, pair):
        assert self.active is None, 'Change caps only between completed sessions'
        self.a.dirty_batch_pages, self.a.wp_batch_pages = pair
        self.active_pair = tuple(pair)

    def finish(self, path, result):
        if 'mode' in result:
            result.update(dirty_batch_pages=self.active_pair[0], wp_batch_pages=self.active_pair[1])
        return super().finish(path, result)

    def write_once(self, name, data):
        path = self.base / name
        if path.exists():
            assert json.loads(path.read_text()) == data, 'Frozen evidence changed: ' + name
        else:
            matched.save(path, data)

    def preflight(self):
        # Keep every original kernel, graph, tool, host and timing check.
        # Avoid the single-pair wrapper's fixed 18-run provenance shape.
        matched.Campaign.preflight(self)
        ref = self.a.reference
        assert matched.sha(ref / 'frozen-work.json') == fused.FROZEN_SHA256
        previous = json.loads((ref / 'provenance.json').read_text())
        current = json.loads((self.base / 'provenance.json').read_text())
        for key in ('graph_sha256', 'gapbs_commit'):
            assert current[key] == previous[key]
        assert current['binary_sha256']['bfs'] == previous['binary_sha256']['bfs']
        baseline = json.loads((ref / 'host-initial.json').read_text())
        for key in ('numa_balancing', 'taint', 'settings', 'governors'):
            assert self.before[key] == baseline[key], key
        sources = [Path(__file__), SCRIPTS / 'run-fused-overhead.py',
                   SCRIPTS / 'prepare-fused-overhead.py', SCRIPTS / 'test-fused-batch-sweep.py']
        for source in sources:
            dest = self.base / 'source' / source.name
            if dest.exists():
                assert matched.sha(dest) == matched.sha(source)
            else:
                dest.write_bytes(source.read_bytes())
        self.write_once('sweep-provenance.json', dict(design_goal=GOAL, selection_rule=SELECTION,
            source_sha256={p.name: matched.sha(p) for p in sources}, reference=str(ref),
            repetitions=3, stage1_runs=90, stage2_runs=54, measured_runs=144,
            unchanged_timing_executor=True, kernel_changes=False, host_settings_changed=False))
        work = json.loads((ref / 'frozen-work.json').read_text())
        frozen = fused.plan(work, 64, 64)
        assert frozen['cells']['bfs-t1']['trials'] == 38
        assert frozen['cells']['bfs-t4']['trials'] == 127
        frozen.update(schedule=schedule([(k, k) for k in SIZES]), stage='equal',
            pairs=[list((k, k)) for k in SIZES], selection_rule=SELECTION, design_goal=GOAL)
        frozen.pop('dirty_batch_pages')
        frozen.pop('wp_batch_pages')
        self.write_once('stage1-plan.json', frozen)
        self.work = frozen['cells']

    def config_gates(self, pair):
        self.set_pair(pair)
        label = 'caps/' + pair_key(pair)
        for mode in 'BC':
            path = self.base / label / ('controller-' + mode)
            if self.receipt(path):
                continue
            before = self.guard(path / 'host-before.json')
            command = self.execute(path, self.controller_args(mode, True) + ['--',
                self.fixture_bin / 'cmdp_fixture', '--mode', 'hotcold', '--seconds', '4'], {})
            v, timing = self.validate_controller(path, mode, before, require_arm=mode == 'C')
            result = dict(mode=mode, elapsed_seconds=command['elapsed_seconds'], validation=v)
            result.update(timing)
            self.finish(path, result)
        for app, threads in matched.CELLS:
            for mode in 'BC':
                self.workload(label + '/generated-{}-t{}-{}'.format(app, threads, mode),
                              app, threads, mode, 8, generated=True, verify=True)
            self.workload(label + '/twitter-{}-t{}-C'.format(app, threads),
                          app, threads, 'C', 1, verify=True)
        self.workload(label + '/sustained-bfs-t4-C', 'bfs', 4, 'C',
                      self.work['bfs-t4']['trials'], sustained=True)

    def measure(self, stage, entries):
        reference = {}
        collected = []
        for entry in entries:
            pair = (entry['dirty_batch_pages'], entry['wp_batch_pages'])
            self.set_pair(pair)
            cell, mode, block = entry['cell'], entry['mode'], entry['block']
            work = self.work[cell]
            name = 'measured/{}/{}/b{:02d}-{}-{}'.format(stage, pair_key(pair), block, cell, mode)
            result = self.workload(name, work['application'], work['threads'], mode,
                                   work['trials'], sustained=True)
            assert (result['dirty_batch_pages'], result['wp_batch_pages']) == pair
            if cell not in reference:
                assert block == 1 and mode == 'A'
                reference[cell] = result['sources']
                prefix = work['pilot_sources'][:min(work['trials'], work['pilot_trials'])]
                assert result['sources'][:len(prefix)] == prefix
            assert result['sources'] == reference[cell], 'Matched source sequence changed'
            row = dict(result, block=block, cell=cell, run=name)
            collected.append(row)
            self.state('measured-progress', last_run=name, elapsed_seconds=result['elapsed_seconds'])
        self.write_once(stage + '-measured.json', collected)
        return collected

    def run(self):
        self.preflight()
        self.stage_name = 'common-gates'
        self.set_pair((64, 64))
        super().gates()
        for mode in 'ABC':
            self.workload('smoke/bfs-t1-' + mode, 'bfs', 1, mode,
                          self.work['bfs-t1']['trials'], sustained=True)
        self.stage_name = 'stage1-gates'
        for k in SIZES[1:]:
            self.config_gates((k, k))
        self.stage_name = 'stage1'
        first = self.measure('stage1', schedule([(k, k) for k in SIZES]))
        selection = select_stage_two(first)
        self.write_once('stage2-selection.json', selection)
        self.state('stage1-complete', selected_k=selection['selected_k'])
        pairs = [tuple(p) for p in selection['pairs']]
        plan = dict(pairs=pairs, schedule=schedule(pairs), cells=self.work, repetitions=3,
                    selected_from='stage1-measured.json', selection_rule=SELECTION,
                    fresh_equal_reference=True, max_address_window_pages=selection['selected_k'])
        # Normalize tuples for exact JSON comparison on resume.
        self.write_once('stage2-plan.json', json.loads(json.dumps(plan)))
        self.stage_name = 'stage2-gates'
        for pair in pairs[1:]:
            self.config_gates(pair)
        self.stage_name = 'stage2'
        second = self.measure('stage2', schedule(pairs))
        assert len(first) == 90 and len(second) == 54
        final = self.guard(self.base / 'host-final.json')
        assert matched.prepare.running_build_id() == self.audit['kernel_build_id']
        assert matched.sha(self.graph) == self.audit['graph_sha256']
        for name, digest in self.audit['installed_files'].items():
            assert matched.sha(Path(name)) == digest, name
        self.write_once('final-audit.json', dict(source_commit=self.commit, kernel_source_unchanged=True,
            boot_id=final['boot_id'], kernel=final['kernel'], kernel_build_id=self.audit['kernel_build_id'],
            graph_sha256=self.audit['graph_sha256'], installed_files=self.audit['installed_files'],
            ownership=final['stats'], passed=True))
        summary = dict(at=matched.now(), source_commit=self.commit, measured_runs=144,
            stage1_runs=90, stage2_runs=54, selected_k=selection['selected_k'], runs=self.summaries)
        summary['pass'] = True
        matched.save(self.base / 'campaign-summary.json', summary)
        self.stage_name = 'complete'
        self.state('complete', passed=True, selected_k=selection['selected_k'])
        (self.base / 'campaign.exit').write_text('0\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--prior', type=Path, required=True)
    p.add_argument('--reference', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    p.add_argument('--resume', action='store_true')
    a = p.parse_args()
    a.dirty_batch_pages = a.wp_batch_pages = 64
    record = json.loads(fused.BUILD_RECORD.read_text())
    assert record['pass'] and record['release'] == matched.prepare.RELEASE
    matched.KERNEL_COMMIT = record['source_freezes']['fixed']['commit']
    assert a.base.is_dir() and os.geteuid() == 0
    with (a.base / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(signum, frame):
            raise RuntimeError('Driver interrupted by signal ' + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        campaign = Campaign(a)
        try:
            campaign.run()
        except BaseException as exc:
            campaign.failure(exc)
            raise


if __name__ == '__main__':
    main()
