#!/usr/bin/env python3
"""Compare complete matched campaigns and expose batching counters.

Inputs are validated outputs of analyze-matched-gapbs.py plus new raw receipts.
Overhead is normalized to each boot's off mode; cross-boot off drift is separate.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

APPS = ('bfs', 'pr', 'cc', 'bc')
COUNTERS = ('dirty_flush_batches', 'dirty_flush_pages', 'arm_flush_batches',
            'arm_flush_pages', 'scan_active_skips', 'scan_page_lookups')
COLORS = {'previous': '#8793a4', 'batched': '#087f8c'}


def read(p):
    return json.loads(p.read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def describe(values):
    return dict(median=statistics.median(values), minimum=min(values), maximum=max(values), values=values)


def compare(before, after, run):
    assert before['pass'] and after['pass']
    assert before['measured_runs'] == after['measured_runs'] == 144
    assert before['repeats_per_cell_mode'] == after['repeats_per_cell_mode'] == 6
    cells, counter_rows, inputs = {}, [], {}
    for cell, b in sorted(before['cells'].items()):
        a = after['cells'][cell]
        assert a['work'] == b['work'] and a['order'] == b['order']
        overhead = {}
        for metric, old in b['overhead'].items():
            new = a['overhead'][metric]
            overhead[metric] = dict(previous=old, batched=new,
                                    change_percentage_points=new['median'] - old['median'])
        modes = {}
        for mode in 'ABC':
            old, new = b['modes'][mode], a['modes'][mode]
            modes[mode] = dict(previous=old, batched=new,
                median_runtime_change_pct=100 * (new['elapsed_s']['median'] / old['elapsed_s']['median'] - 1))
        cells[cell] = dict(overhead=overhead, modes=modes,
                           off_median_drift_pct=modes['A']['median_runtime_change_pct'])
    for item in after['runs']:
        path = run / item['run']
        receipt = read(path / 'summary.json')
        host = {}
        for side in ('before', 'after'):
            name = 'host-' + side + '.json'
            p = path / name
            digest = sha(p)
            assert digest == receipt['evidence_sha256'][name]
            inputs[item['run'] + '/' + name] = digest
            host[side] = read(p)['stats']
        d = {k: host['after'][k] - host['before'][k] for k in COUNTERS}
        assert all(v >= 0 for v in d.values())
        for prefix in ('dirty', 'arm'):
            calls, pages = d[prefix + '_flush_batches'], d[prefix + '_flush_pages']
            assert calls <= pages <= calls * 64
            d[prefix + '_pages_per_flush'] = pages / calls if calls else None
        if item['mode'] == 'A':
            assert all(d[k] == 0 for k in COUNTERS)
        else:
            assert d['dirty_flush_batches'] > 0
            assert bool(d['arm_flush_batches']) == (item['mode'] == 'C')
        d.update(run=item['run'], cell=item['cell'], mode=item['mode'], block=item['block'],
                 elapsed_s=item['elapsed_s'], dirty_flush_s=item['dirty_flush_s'])
        counter_rows.append(d)
    for cell, data in cells.items():
        data['batching'] = {}
        for mode in 'ABC':
            matching = [r for r in counter_rows if r['cell'] == cell and r['mode'] == mode]
            assert len(matching) == 6
            data['batching'][mode] = {key: describe([r[key] for r in matching])
                for key in COUNTERS + ('dirty_pages_per_flush', 'arm_pages_per_flush')
                if all(r[key] is not None for r in matching)}
    return dict(pass_=True, cells=cells, counter_rows=counter_rows,
                 interpretation='Each overhead uses its own boot and matched off run; between-boot differences remain observational.'), inputs


def plots(data, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold',
        'axes.labelcolor': '#334155', 'text.color': '#172033', 'axes.edgecolor': '#cbd5e1',
        'savefig.facecolor': 'white', 'figure.facecolor': 'white', 'pdf.fonttype': 42})

    def save(fig, name):
        for ext in ('png', 'svg', 'pdf'):
            path = out / (name + '.' + ext)
            fig.savefig(path, dpi=180, bbox_inches='tight')
            if ext == 'svg':
                path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n')
        plt.close(fig)

    def style(ax, ylabel):
        ax.set_ylabel(ylabel)
        ax.set_xticks(range(4))
        ax.set_xticklabels([x.upper() for x in APPS])
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#e2e8f0', linewidth=.7)

    def dots(ax, x, d, label, color, legend=False):
        ax.vlines(x, d['minimum'], d['maximum'], color=color, alpha=.5, lw=1)
        ax.scatter(x + np.linspace(-.035, .035, len(d['values'])), d['values'],
                   color=color, s=20, alpha=.7, label=label if legend else None, zorder=3)
        ax.plot([x-.075, x+.075], [d['median']]*2, lw=3, color=color, zorder=4)

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.3), sharey=True)
    for row, (metric, title) in enumerate((('sampling_vs_off_pct', 'Sampling only'),
                                         ('full_vs_off_pct', 'Sampling + dirty→ARM'))):
        for col, threads in enumerate((1, 4)):
            ax = axes[row, col]
            for i, app in enumerate(APPS):
                cell = data['cells']['{}-t{}'.format(app, threads)]['overhead'][metric]
                for offset, key, label in ((-.13, 'previous', 'Previous kernel'), (.13, 'batched', 'Batched kernel')):
                    dots(ax, i+offset, cell[key], label, COLORS[key], legend=i == 0)
            ax.axhline(0, color='#334155', lw=1)
            ax.set_title('{} · {} thread{}'.format(title, threads, '' if threads == 1 else 's'))
            style(ax, 'End-to-end overhead relative to off (%)')
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .945), ncol=2, frameon=False)
    fig.suptitle('End-to-end overhead before and after batching', fontsize=16, fontweight='bold')
    fig.text(.5, .004, 'Each dot: one within-block ratio to off on the same boot. Thick bars: medians of six ratios.\n'
             'Identical fixed work and schedule across boots; thin lines show observed ranges, not confidence intervals.',
             ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .06, 1, .91))
    save(fig, 'batching-overhead-comparison')

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.0), sharey=True)
    for ax, threads in zip(axes, (1, 4)):
        for i, app in enumerate(APPS):
            cell = data['cells']['{}-t{}'.format(app, threads)]
            for offset, key, label in ((-.13, 'previous', 'Previous kernel'), (.13, 'batched', 'Batched kernel')):
                dots(ax, i+offset, cell['modes']['A'][key]['elapsed_s'], label, COLORS[key], legend=i == 0)
        ax.set_title('{} thread{}'.format(threads, '' if threads == 1 else 's'))
        style(ax, 'Monitoring-off runtime (seconds)')
        ax.set_ylim(bottom=0)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .91), ncol=2, frameon=False)
    fig.suptitle('Check baseline drift across the two boots', fontsize=16, fontweight='bold')
    fig.text(.5, .005, 'Same application work with CMD-P monitoring off. Dots: all six repeats; bars: medians.\n'
             'Overhead comparisons normalize to each boot rather than attributing every raw runtime change to batching.',
             ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .08, 1, .84))
    save(fig, 'batching-off-baseline')

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.3))
    coverage_max = max(d['modes']['C'][version]['mean_live_arm_gib']['maximum']
                       for d in data['cells'].values() for version in COLORS)
    wp_max = max(d['modes']['C'][version]['wp_events']['maximum']
                for d in data['cells'].values() for version in COLORS)
    for row, (metric, title, ylabel) in enumerate((
            ('mean_live_arm_gib', 'Average live ARM', 'Protected memory (GiB)'),
            ('wp_events', 'WP events', 'Write-triggered generation revocations'))):
        for col, threads in enumerate((1, 4)):
            ax = axes[row, col]
            for i, app in enumerate(APPS):
                cell = data['cells']['{}-t{}'.format(app, threads)]['modes']['C']
                for offset, key, label in ((-.13, 'previous', 'Previous kernel'), (.13, 'batched', 'Batched kernel')):
                    dots(ax, i+offset, cell[key][metric], label, COLORS[key], legend=i == 0)
            ax.set_title('{} · {} thread{}'.format(title, threads, '' if threads == 1 else 's'))
            style(ax, ylabel)
            if row == 0:
                ax.set_ylim(0, coverage_max * 1.12)
            else:
                ax.set_yscale('symlog', linthresh=1)
                ax.set_ylim(-.2, max(10, wp_max * 1.6))
                ax.set_yticks([0, 1, 10, 100, 1000, 10000, 100000])
                ax.set_yticklabels(['0', '1', '10', '100', '1k', '10k', '100k'])
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .945), ncol=2, frameon=False)
    fig.suptitle('Protection coverage and WP events with the same policy', fontsize=16, fontweight='bold')
    fig.text(.5, .004, 'Dirty→ARM mode; six complete runs and their medians. ARM is time-averaged over the whole job.\n'
             'WP counts are generation events, not unique pages. The lower row uses a logarithmic scale above 1 and retains zero.',
             ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .06, 1, .91))
    save(fig, 'batching-coverage-wp')

    names = ['{}-t{}'.format(app, t) for app in APPS for t in (1, 4)]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.0), sharey=True)
    for i, cell in enumerate(names):
        metrics = data['cells'][cell]['batching']
        for offset, mode, label, color in ((-.12, 'B', 'Sampling only', '#d18b17'),
                                         (.12, 'C', 'Dirty→ARM', '#7357bd')):
            dots(axes[0], i+offset, metrics[mode]['dirty_pages_per_flush'], label, color, legend=i == 0)
        dots(axes[1], i, metrics['C']['arm_pages_per_flush'], 'Dirty→ARM', '#7357bd', legend=i == 0)
    for ax, title in zip(axes, ('Dirty-bit resets', 'ARM protection')):
        ax.axhline(64, color='#64748b', ls='--', lw=1)
        ax.set_title(title)
        ax.set_ylim(0, 69)
        ax.set_ylabel('Pages covered per range-flush call')
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels([x.replace('-t', '\n').upper() for x in names])
        ax.grid(axis='y', color='#e2e8f0', linewidth=.7)
        ax.set_axisbelow(True)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .91), ncol=2, frameon=False)
    fig.suptitle('Batching observed during the native workloads', fontsize=16, fontweight='bold')
    fig.text(.5, .005, 'Per-run total pages / total calls; six repeats and their medians. Dashed line: 64-page bound.\n'
             'Calls are synchronous architecture range-flush requests, not IPI counts. X labels show workload and threads.',
             ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .08, 1, .84))
    save(fig, 'batching-flush-coverage')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before-analysis', type=Path, required=True)
    p.add_argument('--after-analysis', type=Path, required=True)
    p.add_argument('--after-run', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--no-plots', action='store_true')
    a = p.parse_args()
    result, inputs = compare(read(a.before_analysis), read(a.after_analysis), a.after_run)
    result['pass'] = result.pop('pass_')
    result['inputs'] = dict(before_analysis_sha256=sha(a.before_analysis), after_analysis_sha256=sha(a.after_analysis),
                            comparison_source_sha256=sha(Path(__file__)), host_snapshot_sha256=inputs)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    with (a.out / 'batch-counters.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result['counter_rows'][0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(result['counter_rows'])
    if not a.no_plots:
        plots(result, a.out)
    for cell, d in sorted(result['cells'].items()):
        print(cell, json.dumps({k: dict(previous=v['previous']['median'], batched=v['batched']['median'],
                  change_pp=v['change_percentage_points']) for k, v in d['overhead'].items()}))


if __name__ == '__main__':
    main()
