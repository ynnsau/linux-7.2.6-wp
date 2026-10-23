#!/usr/bin/env python3
"""Verify matched campaign evidence; export repeat-level data and figures.

Read-only inputs. Requires numpy/matplotlib only for plots. Primary estimates
are medians of three within-block runtime ratios, not ratios of medians.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics

APPS = ('bfs',)
MODES = ('A', 'B', 'C')
LABELS = {'A': 'Off', 'B': 'Sampling only', 'C': 'Sampling + dirty→ARM'}
COLORS = {'A': '#64748b', 'B': '#d18b17', 'C': '#7357bd'}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def describe(values):
    return dict(median=statistics.median(values), minimum=min(values), maximum=max(values), values=values)


def collect(base):
    frozen = read(base / 'frozen-work.json')
    overall = read(base / 'campaign-summary.json')
    assert overall['pass'] and overall['measured_runs'] == 18
    assert (base / 'campaign.exit').read_text().strip() == '0'
    assert len(frozen['schedule']) == 18
    assert frozen['repetitions'] == 3
    assert 1 <= frozen['dirty_batch_pages'] <= 65536
    assert 1 <= frozen['wp_batch_pages'] <= 65536
    assert set(frozen['cells']) == {'bfs-t1', 'bfs-t4'}
    manifest, runs, series, grouped = {}, [], {}, {}
    for entry in frozen['schedule']:
        block, cell, mode = entry['block'], entry['cell'], entry['mode']
        name = 'measured/b{:02d}-{}-{}'.format(block, cell, mode)
        path = base / name
        summary = read(path / 'summary.json')
        assert summary['pass'] and summary['source_commit'] == overall['source_commit']
        assert summary['trials'] == frozen['cells'][cell]['trials']
        assert summary['mode'] == mode and not summary['generated'] and not summary['verify']
        assert summary['elapsed_seconds'] > 0
        command = read(path / 'command.json')
        assert command['exit'] == 0 and not command.get('timed_out')
        elapsed = (command['launcher_end_ns'] - command['launcher_begin_ns']) / 1e9
        assert abs(elapsed - summary['elapsed_seconds']) < 1e-8
        log = (path / 'target.log').read_text()
        times = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)]
        assert times == summary['trial_times_s'] and len(times) == summary['trials']
        assert not re.search(r'Verification:', log)
        if summary['application'] == 'pr':
            indices = [int(x) for x in re.findall(r'^\s+(\d+)\s+[0-9.eE+-]+\s*$', log, re.M)]
            assert indices == list(range(21)) * summary['trials']
        elif summary['application'] in ('bfs', 'bc'):
            assert [int(x) for x in re.findall(r'^Source:\s*(\d+)', log, re.M)] == summary['sources']
        for filename, digest in summary['evidence_sha256'].items():
            assert sha(path / filename) == digest, str(path / filename)
            manifest[name + '/' + filename] = digest
        manifest[name + '/summary.json'] = sha(path / 'summary.json')
        grouped.setdefault(cell, {}).setdefault(block, {})[mode] = summary
        proc = summary['target_rusage']
        final, dirty, progress = summary['final'], summary['dirty'], summary['progress']
        run = dict(run=name, cell=cell, block=block, application=summary['application'], threads=summary['threads'],
            mode=mode, trials=summary['trials'], elapsed_s=summary['elapsed_seconds'],
            child_lifetime_s=summary['child_lifetime_seconds'], trial_s=summary['trial_seconds'],
            controller_tail_s=summary['controller_tail_seconds'], target_peak_rss_gib=proc['maxrss_kib'] / 2**20,
            target_user_s=proc['user_us'] / 1e6, target_system_s=proc['system_us'] / 1e6,
            minor_faults=proc['minor_faults'], major_faults=proc['major_faults'],
            completed_sweeps=summary['completed_sweeps'], sweeps_started=final.get('passes_started', 0),
            first_arms=final.get('first_arms', 0), rearms=final.get('rearms', 0),
            wp_events=final.get('terminals', {}).get('write', 0), wp_service_s=progress.get('write_ns', 0) / 1e9,
            wp_max_us=progress.get('write_max_ns', 0) / 1000,
            lock_wait_s=progress.get('scan_lock_wait_total_ns', 0) / 1e9,
            lock_retries=progress.get('scan_lock_retries', 0), dirty_samples=dirty.get('samples', 0),
            dirty_sample_s=dirty.get('sample_ns', 0) / 1e9, dirty_flush_s=dirty.get('flush_ns', 0) / 1e9,
            history_mib=dirty.get('metadata_bytes', 0) / 2**20,
            mean_live_arm_gib=final.get('protected_byte_ms', 0) / 1000 / 2**30 / summary['elapsed_seconds'],
            source_sequence=summary['sources'])
        batch = {} if mode == 'A' else read(path / 'validation.json')['batch']
        if batch:
            snapshots = [r for r in rows(path / 'monitor.jsonl') if r['event'] == 'batch_status']
            assert snapshots and snapshots[-1] == batch
            assert batch['dirty_batch_pages'] == frozen['dirty_batch_pages']
            assert batch['wp_batch_pages'] == frozen['wp_batch_pages']
            assert batch['flush_calls'] == sum(batch[k] for k in
                ('shared_flush_calls', 'dirty_only_flush_calls', 'wp_only_flush_calls'))
            assert batch['max_dirty_pages'] <= frozen['dirty_batch_pages']
            assert batch['max_wp_pages'] <= frozen['wp_batch_pages']
            assert batch['max_span_pages'] <= max(frozen['dirty_batch_pages'], frozen['wp_batch_pages'])
            assert batch['history_payload_bytes'] == dirty['metadata_bytes']
            assert batch['history_allocated_bytes'] >= batch['history_payload_bytes']
            if mode == 'B':
                assert batch['wp_pages'] == batch['shared_flush_calls'] == batch['wp_only_flush_calls'] == 0
        for key in ('flush_calls', 'shared_flush_calls', 'dirty_only_flush_calls', 'wp_only_flush_calls',
                    'dirty_pages', 'wp_pages', 'max_dirty_pages', 'max_wp_pages', 'max_span_pages',
                    'chunks', 'limit_chunks', 'time_chunks', 'region_lookups', 'region_full_skips',
                    'region_skip_pages', 'active_run_skips', 'pmd_walks', 'pmd_cache_hits'):
            run[key] = batch.get(key, 0)
        run['actual_flush_s'] = batch.get('flush_ns', 0) / 1e9
        run['max_scan_hold_ms'] = batch.get('max_hold_ns', 0) / 1e6
        run['history_allocated_mib'] = batch.get('history_allocated_bytes', 0) / 2**20
        run['shared_flush_pct'] = 100 * batch.get('shared_flush_calls', 0) / max(1, batch.get('flush_calls', 0))
        for role, pages, only in (('dirty', 'dirty_pages', 'dirty_only_flush_calls'),
                                  ('wp', 'wp_pages', 'wp_only_flush_calls')):
            denominator = batch.get(only, 0) + batch.get('shared_flush_calls', 0)
            run[role + '_pages_per_flush'] = batch.get(pages, 0) / denominator if denominator else None
        host = {side: read(path / ('host-' + side + '.json')) for side in ('before', 'after')}
        for side in host.values():
            assert all(side['stats'][key] == 0 for key in ('entries', 'managed', 'isolated'))
        for key in ('kernel', 'boot_id', 'settings', 'governors', 'taint', 'numa_balancing'):
            assert host['before'][key] == host['after'][key]
        run['scan_page_lookups'] = host['after']['stats']['scan_page_lookups'] - host['before']['stats']['scan_page_lookups']
        run['scan_active_skips'] = host['after']['stats']['scan_active_skips'] - host['before']['stats']['scan_active_skips']
        runs.append(run)
        # Predeclared illustration: first block, all modes. No run selected by
        # coverage or favorable performance. Timestamps share CLOCK_MONOTONIC.
        if block == 1:
            origin = command['launcher_begin_ns']
            monitor = rows(path / 'monitor.jsonl')
            status = [r for r in monitor if r['event'] in ('status', 'stopping_status', 'observation_end', 'final')]
            memory = [r for r in rows(path / 'memory-samples.jsonl') if not r.get('error') and r.get('memory_kib')]
            series[cell + '-' + mode] = dict(
                elapsed_s=run['elapsed_s'],
                rss_time_s=[(r['read_begin_ns'] - origin) / 1e9 for r in memory],
                rss_gib=[r['memory_kib'].get('VmRSS', 0) / 2**20 for r in memory],
                arm_time_s=[(r['now_ns'] - origin) / 1e9 for r in status],
                arm_gib=[r['active'] * 4096 / 2**30 for r in status],
                wp_events=[r['terminals']['write'] for r in status])
    cells = {}
    for cell, blocks in grouped.items():
        assert set(blocks) == set(range(1, 4))
        mode_orders = [''.join(e['mode'] for e in frozen['schedule'] if e['cell'] == cell and e['block'] == b)
                       for b in range(1, 4)]
        assert mode_orders == ['ABC', 'BCA', 'CAB']
        reference = blocks[1]['A']['sources']
        for modes in blocks.values():
            assert set(modes) == set(MODES)
            assert all(s['sources'] == reference for s in modes.values())
        ratios = {label: [] for label in ('sampling_vs_off_pct', 'full_vs_off_pct', 'full_vs_sampling_pct')}
        secondary = {metric: {label: [] for label in ratios} for metric in ('child_lifetime_seconds', 'trial_seconds')}
        for b in range(1, 4):
            s = blocks[b]
            for label, numerator, denominator in [('sampling_vs_off_pct', 'B', 'A'),
                    ('full_vs_off_pct', 'C', 'A'), ('full_vs_sampling_pct', 'C', 'B')]:
                ratios[label].append(100 * (s[numerator]['elapsed_seconds'] / s[denominator]['elapsed_seconds'] - 1))
                for metric in secondary:
                    secondary[metric][label].append(100 * (s[numerator][metric] / s[denominator][metric] - 1))
        cellruns = [r for r in runs if r['cell'] == cell]
        metrics = ('elapsed_s', 'child_lifetime_s', 'trial_s', 'target_peak_rss_gib', 'target_user_s', 'target_system_s',
                   'mean_live_arm_gib', 'wp_events', 'wp_service_s', 'lock_wait_s', 'lock_retries',
                   'completed_sweeps', 'history_mib', 'dirty_samples', 'dirty_sample_s', 'dirty_flush_s')
        metrics += ('flush_calls', 'shared_flush_calls', 'shared_flush_pct', 'actual_flush_s',
                    'history_allocated_mib', 'max_scan_hold_ms', 'region_full_skips', 'region_skip_pages',
                    'active_run_skips', 'pmd_walks', 'pmd_cache_hits', 'scan_page_lookups', 'scan_active_skips')
        cells[cell] = dict(work=frozen['cells'][cell], order=mode_orders,
            overhead={k: describe(v) for k, v in ratios.items()},
            secondary_overhead={metric: {k: describe(v) for k, v in values.items()} for metric, values in secondary.items()},
            modes={mode: {metric: describe([r[metric] for r in cellruns if r['mode'] == mode]) for metric in metrics}
                   for mode in MODES})
    for filename in ('frozen-work.json', 'campaign-summary.json', 'campaign.exit', 'provenance.json', 'host-final.json'):
        manifest[filename] = sha(base / filename)
    return dict(pass_=True, measured_runs=len(runs), repeats_per_cell_mode=3, primary='whole job elapsed',
                dirty_batch_pages=frozen['dirty_batch_pages'], wp_batch_pages=frozen['wp_batch_pages'],
                wp_event_scope='CMD-P generations terminated by write; not every Linux page fault',
                wp_time_scope='cmdp_prepare_write elapsed around revoke; excludes fault entry/lookup and remaining Linux fault path',
                cells=cells, runs=runs, first_block_series=series), manifest


def compare(data, current, previous_root):
    previous = read(previous_root / 'analysis/analysis.json')
    assert previous['pass'] and previous['measured_runs'] == 144
    assert previous['repeats_per_cell_mode'] == 6
    oldbase = previous_root / 'run'
    old_provenance, new_provenance = read(oldbase / 'provenance.json'), read(current / 'provenance.json')
    for key in ('graph_sha256', 'gapbs_commit'):
        assert old_provenance[key] == new_provenance[key]
    assert old_provenance['binary_sha256']['bfs'] == new_provenance['binary_sha256']['bfs']
    oldhost, newhost = read(oldbase / 'host-initial.json'), read(current / 'host-initial.json')
    for key in ('settings', 'governors', 'numa_balancing', 'taint'):
        assert oldhost[key] == newhost[key]
    manifest = {'analysis/analysis.json': sha(previous_root / 'analysis/analysis.json')}
    cells = {}
    for cell, latest in data['cells'].items():
        prior = previous['cells'][cell]
        assert latest['work'] == prior['work']
        oldruns = {}
        for block in range(1, 7):
            oldruns[block] = {}
            for mode in MODES:
                name = 'measured/b{:02d}-{}-{}'.format(block, cell, mode)
                path = oldbase / name
                receipt = read(path / 'summary.json')
                assert receipt['pass'] and receipt['trials'] == latest['work']['trials']
                for filename, digest in receipt['evidence_sha256'].items():
                    assert sha(path / filename) == digest
                    manifest['run/' + name + '/' + filename] = digest
                manifest['run/' + name + '/summary.json'] = sha(path / 'summary.json')
                oldruns[block][mode] = receipt
        reference = oldruns[1]['A']['sources']
        assert all(r['source_sequence'] == reference for r in data['runs'] if r['cell'] == cell)
        for mode in MODES:
            assert [oldruns[b][mode]['elapsed_seconds'] for b in range(1, 7)] == prior['modes'][mode]['elapsed_s']['values']
            assert all(oldruns[b][mode]['sources'] == reference for b in range(1, 7))
        comparisons = {}
        for key, num, den in (('sampling_vs_off_pct', 'B', 'A'),
                             ('full_vs_off_pct', 'C', 'A'), ('full_vs_sampling_pct', 'C', 'B')):
            expected = [100 * (oldruns[b][num]['elapsed_seconds'] / oldruns[b][den]['elapsed_seconds'] - 1)
                        for b in range(1, 7)]
            assert expected == prior['overhead'][key]['values']
            assert describe(expected) == prior['overhead'][key]
            comparisons[key] = dict(previous=prior['overhead'][key], current=latest['overhead'][key],
                change_percentage_points=latest['overhead'][key]['median'] - prior['overhead'][key]['median'])
        counters = {}
        for mode in ('B', 'C'):
            calls = []
            for b in range(1, 7):
                p = oldbase / 'measured' / ('b{:02d}-{}-{}'.format(b, cell, mode))
                before, after = read(p / 'host-before.json')['stats'], read(p / 'host-after.json')['stats']
                calls.append(sum(after[k] - before[k] for k in ('dirty_flush_batches', 'arm_flush_batches')))
            counters[mode] = dict(previous=describe(calls), current=latest['modes'][mode]['flush_calls'])
        cells[cell] = dict(overhead=comparisons,
            modes={mode: dict(previous=prior['modes'][mode], current=latest['modes'][mode]) for mode in MODES},
            flush_calls=counters,
            off_median_drift_pct=100 * (latest['modes']['A']['elapsed_s']['median'] / prior['modes']['A']['elapsed_s']['median'] - 1))
    return dict(cells=cells, previous_repetitions=6, current_repetitions=3,
                previous_kernel=oldhost['kernel'], current_kernel=newhost['kernel'],
                first_block_previous=previous['first_block_series'],
                interpretation='Normalize to each boot and matched off run; compare the combined changes, with different repeat counts and mode orders.'), manifest


def plots(data, comparison, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.ticker import MaxNLocator
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold',
        'axes.labelcolor': '#334155', 'text.color': '#172033', 'axes.edgecolor': '#cbd5e1',
        'savefig.facecolor': 'white', 'figure.facecolor': 'white', 'pdf.fonttype': 42})
    versions = (('previous', 'Previous batching · 6 repeats', '#8793a4', -.14),
                ('current', 'Shared flush + scanner changes · 3 repeats', '#087f8c', .14))

    def save(fig, name):
        for ext in ('png', 'svg', 'pdf'):
            fig.savefig(out / (name + '.' + ext), dpi=180, bbox_inches='tight')
        plt.close(fig)

    def grid(ax, ylabel):
        ax.set_ylabel(ylabel)
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#e2e8f0', linewidth=.7)

    def dots(ax, x, values, color, label=None):
        ax.vlines(x, values['minimum'], values['maximum'], color=color, alpha=.5)
        ax.scatter(x + np.linspace(-.045, .045, len(values['values'])), values['values'],
                   color=color, label=label, s=36, alpha=.85, zorder=3)
        ax.plot([x-.075, x+.075], [values['median']]*2, color=color, lw=3, zorder=4)

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2), sharey=True)
    for ax, threads in zip(axes, (1, 4)):
        cell = comparison['cells']['bfs-t' + str(threads)]
        for i, metric in enumerate(('sampling_vs_off_pct', 'full_vs_off_pct')):
            for version, label, color, offset in versions:
                d = cell['overhead'][metric][version]
                dots(ax, i+offset, d, color, label if i == 0 else None)
                ax.text(i+offset, d['maximum']+.4, '{:.2f}%'.format(d['median']),
                        ha='center', color=color, fontsize=10)
        ax.axhline(0, color='#64748b', lw=1)
        ax.set_title('BFS · {} thread{}'.format(threads, '' if threads == 1 else 's'))
        ax.set_xticks([0, 1]); ax.set_xticklabels(['Sampling only', 'Sampling + dirty→ARM'])
        ax.set_xlim(-.45, 1.45)
        grid(ax, 'End-to-end overhead relative to off (%)')
    top = max(comparison['cells'][cell]['overhead'][metric][version]['maximum']
              for cell in data['cells'] for metric in ('sampling_vs_off_pct', 'full_vs_off_pct')
              for version in ('previous', 'current'))
    for ax in axes:
        ax.set_ylim(top=top+2)
    fig.legend(*axes[0].get_legend_handles_labels(), loc='upper center', bbox_to_anchor=(.5, .91), ncol=2, frameon=False)
    fig.suptitle('BFS overhead with the same graph and fixed work', fontsize=16, fontweight='bold')
    fig.text(.5, .015, 'Dots: within-block ratios to off on each boot. Bars: medians; lines: observed ranges, not confidence intervals.\n'
             'Previous caps: 64/64; current: {}/{}. Comparison measures the combined changes.'.format(
                 data['dirty_batch_pages'], data['wp_batch_pages']), ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .10, 1, .84)); save(fig, 'fused-overhead-comparison')

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.1))
    for col, threads in enumerate((1, 4)):
        cell = data['cells']['bfs-t' + str(threads)]
        for row, metric, ylabel in ((0, 'elapsed_s', 'End-to-end runtime (seconds)'),
                                    (1, 'target_peak_rss_gib', 'Target peak RSS (GiB)')):
            ax = axes[row, col]
            for i, mode in enumerate(MODES):
                dots(ax, i, cell['modes'][mode][metric], COLORS[mode])
                ax.annotate('{:.2f}'.format(cell['modes'][mode][metric]['median']),
                    (i, cell['modes'][mode][metric]['median']), xytext=(0, 11),
                    textcoords='offset points', ha='center', fontsize=10)
            ax.set_title('{} thread{} · {} trials'.format(threads, '' if threads == 1 else 's', cell['work']['trials']))
            ax.set_xticks(range(3)); ax.set_xticklabels(['Off', 'Sampling', 'Dirty→ARM'])
            ax.set_xlim(-.5, 2.5)
            ax.set_ylim(0, max(cell['modes'][m][metric]['maximum'] for m in MODES)*1.17)
            grid(ax, ylabel)
    fig.suptitle('Current kernel: runtime and application memory', fontsize=16, fontweight='bold')
    fig.text(.5, .01, 'Three complete runs per mode; dots show every run and bars show medians.\n'
             'Runtime includes loading, initialization and cleanup. Target RSS excludes kernel tracking metadata.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .075, 1, .95)); save(fig, 'fused-runtime-rss')

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.2))
    for col, threads in enumerate((1, 4)):
        key = 'bfs-t{}-C'.format(threads)
        series, old = data['first_block_series'][key], comparison['first_block_previous'][key]
        ax = axes[0, col]
        ax.plot(series['rss_time_s'], series['rss_gib'], color='#64748b', label='Current process RSS', lw=1.6)
        ax.step(old['arm_time_s'], old['arm_gib'], where='post', color='#94a3b8', ls='--', label='Previous live ARM', lw=1.5)
        ax.step(series['arm_time_s'], series['arm_gib'], where='post', color='#087f8c', label='Current live ARM', lw=1.8)
        ax.fill_between(series['arm_time_s'], series['arm_gib'], step='post', color='#087f8c', alpha=.10)
        ax.set_title('BFS · {} thread{}'.format(threads, '' if threads == 1 else 's'))
        grid(ax, 'Memory (GiB)'); ax.set_ylim(bottom=0)
        ax = axes[1, col]
        ax.step(old['arm_time_s'], old['wp_events'], where='post', color='#94a3b8', ls='--', label='Previous', lw=1.5)
        ax.step(series['arm_time_s'], series['wp_events'], where='post', color='#087f8c', label='Current', lw=1.8)
        maximum = max(old['wp_events'] + series['wp_events'] + [1])
        ax.set_ylim(0, maximum*1.15)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_xlabel('Seconds since launcher start'); grid(ax, 'Cumulative WP events')
        axes[0, col].set_xlabel('Seconds since launcher start')
    fig.legend(*axes[0, 0].get_legend_handles_labels(), loc='upper center', bbox_to_anchor=(.5, .94), ncol=3, frameon=False)
    fig.suptitle('Protected coverage and writes over time', fontsize=16, fontweight='bold')
    fig.text(.5, .01, 'Predeclared illustration: first dirty→ARM block from each campaign. Cleanup returns live ARM to zero.\n'
             'WP counts generation revocations caused by writes, not unique pages or every Linux page fault.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .075, 1, .89)); save(fig, 'fused-coverage-wp')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--previous-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    data, manifest = collect(args.base)
    comparison, prior_manifest = compare(data, args.base, args.previous_root)
    data['pass'] = data.pop('pass_')
    data['analysis_source_sha256'] = sha(Path(__file__))
    data['python_version'] = platform.python_version()
    for name, obj in (('analysis.json', data), ('comparison.json', comparison),
                      ('input-manifest.json', manifest), ('previous-input-manifest.json', prior_manifest)):
        (args.out / name).write_text(json.dumps(obj, indent=2) + '\n')
    with (args.out / 'runs.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data['runs'][0]))
        writer.writeheader(); writer.writerows(data['runs'])
    plots(data, comparison, args.out)
    import matplotlib, numpy
    (args.out / 'analysis-environment.json').write_text(json.dumps(dict(
        python=platform.python_version(), matplotlib=matplotlib.__version__, numpy=numpy.__version__,
        source_sha256=sha(Path(__file__))), indent=2) + '\n')
    print(json.dumps(dict(pass_=True, measured_runs=len(data['runs']), repeats=3,
        cells={c: x['overhead'] for c, x in data['cells'].items()})))


if __name__ == '__main__':
    main()
