#!/usr/bin/env python3
"""Verify matched campaign evidence; export repeat-level data and figures.

Read-only inputs. Requires numpy/matplotlib only for plots. Primary estimates
are medians of six within-block runtime ratios, not ratios of medians.
"""
import argparse
import csv
import hashlib
import itertools
import json
from pathlib import Path
import platform
import re
import statistics

APPS = ('bfs', 'pr', 'cc', 'bc')
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
    assert overall['pass'] and overall['measured_runs'] == 144
    assert (base / 'campaign.exit').read_text().strip() == '0'
    assert len(frozen['schedule']) == 144
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
        assert set(blocks) == set(range(1, 7))
        mode_orders = [''.join(e['mode'] for e in frozen['schedule'] if e['cell'] == cell and e['block'] == b)
                       for b in range(1, 7)]
        assert sorted(mode_orders) == sorted(''.join(p) for p in itertools.permutations('ABC'))
        reference = blocks[1]['A']['sources']
        for modes in blocks.values():
            assert set(modes) == set(MODES)
            assert all(s['sources'] == reference for s in modes.values())
        ratios = {label: [] for label in ('sampling_vs_off_pct', 'full_vs_off_pct', 'full_vs_sampling_pct')}
        secondary = {metric: {label: [] for label in ratios} for metric in ('child_lifetime_seconds', 'trial_seconds')}
        for b in range(1, 7):
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
        cells[cell] = dict(work=frozen['cells'][cell], order=mode_orders,
            overhead={k: describe(v) for k, v in ratios.items()},
            secondary_overhead={metric: {k: describe(v) for k, v in values.items()} for metric, values in secondary.items()},
            modes={mode: {metric: describe([r[metric] for r in cellruns if r['mode'] == mode]) for metric in metrics}
                   for mode in MODES})
    for filename in ('frozen-work.json', 'campaign-summary.json', 'campaign.exit', 'provenance.json', 'host-final.json'):
        manifest[filename] = sha(base / filename)
    return dict(pass_=True, measured_runs=len(runs), repeats_per_cell_mode=6, primary='whole job elapsed',
                wp_event_scope='CMD-P generations terminated by write; not every Linux page fault',
                wp_time_scope='cmdp_prepare_write elapsed around revoke; excludes fault entry/lookup and remaining Linux fault path',
                cells=cells, runs=runs, first_block_series=series), manifest


def plots(data, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold',
        'axes.labelcolor': '#334155', 'text.color': '#172033', 'axes.edgecolor': '#cbd5e1',
        'savefig.facecolor': 'white', 'figure.facecolor': 'white', 'pdf.fonttype': 42})
    def savefig(fig, name):
        for ext in ('png', 'svg', 'pdf'):
            path = out / (name + '.' + ext)
            fig.savefig(path, dpi=180, bbox_inches='tight')
            if ext == 'svg':
                path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n')
        plt.close(fig)
    def style(ax, ylabel):
        ax.set_ylabel(ylabel)
        ax.grid(axis='y', color='#e2e8f0', linewidth=.7)
        ax.set_axisbelow(True)
        ax.set_xticks(range(4))
        ax.set_xticklabels([app.upper() for app in APPS])
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3))
    for ax, threads in zip(axes, (1, 4)):
        for i, app in enumerate(APPS):
            cell = data['cells']['{}-t{}'.format(app, threads)]
            for j, mode in enumerate(MODES):
                values = cell['modes'][mode]['elapsed_s']['values']
                x = i + (j - 1) * .23
                ax.scatter(x + np.linspace(-.055, .055, len(values)), values, color=COLORS[mode], s=27, alpha=.8,
                           label=LABELS[mode] if i == 0 else None, zorder=3)
                ax.plot([x - .08, x + .08], [statistics.median(values)] * 2, color=COLORS[mode], lw=3)
        ax.set_title('{} thread{}'.format(threads, '' if threads == 1 else 's'))
        style(ax, 'End-to-end runtime (seconds)')
        ax.set_ylim(bottom=0)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .91), ncol=3, frameon=False, fontsize=10)
    fig.suptitle('Identical fixed work: total application + controller runtime', fontsize=16, fontweight='bold')
    fig.text(.5, -.015, 'Dots: all six runs per mode. Bars: medians. Whole trials; graph loading and cleanup included.\n'
             'Work counts are fixed within each workload/thread cell and may differ between thread counts.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .06, 1, .84))
    savefig(fig, 'matched-runtime')

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), sharey=True)
    for ax, threads in zip(axes, (1, 4)):
        for i, app in enumerate(APPS):
            cell = data['cells']['{}-t{}'.format(app, threads)]
            for offset, mode, metric in [(-.14, 'B', 'sampling_vs_off_pct'), (.14, 'C', 'full_vs_off_pct')]:
                d = cell['overhead'][metric]
                x = i + offset
                ax.vlines(x, d['minimum'], d['maximum'], color=COLORS[mode], lw=1, alpha=.5)
                ax.scatter(x + np.linspace(-.06, .06, 6), d['values'], color=COLORS[mode], s=30, alpha=.75,
                           label=LABELS[mode] if i == 0 else None, zorder=3)
                ax.plot([x - .09, x + .09], [d['median']] * 2, lw=3, color=COLORS[mode])
        ax.axhline(0, color='#334155', lw=1)
        ax.set_title('{} thread{}'.format(threads, '' if threads == 1 else 's'))
        style(ax, 'Runtime change relative to off (%)')
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .91), ncol=2, frameon=False, fontsize=10)
    fig.suptitle('End-to-end overhead of sampling and dirty→ARM', fontsize=16, fontweight='bold')
    fig.text(.5, -.01, 'Each dot is 100 × (mode runtime / off runtime − 1) within the same block.\n'
             'Bars: median of six ratios. Lines: observed range, not a confidence interval. Negative values retained.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .06, 1, .84))
    savefig(fig, 'matched-overhead')

    fig, axes = plt.subplots(4, 2, figsize=(13, 12), sharey=True)
    coverage_max = max(max(s['rss_gib'] + s['arm_gib'])
                       for key, s in data['first_block_series'].items() if key.endswith('-C'))
    for i, app in enumerate(APPS):
        for j, threads in enumerate((1, 4)):
            ax = axes[i, j]
            s = data['first_block_series']['{}-t{}-C'.format(app, threads)]
            ax.plot(s['rss_time_s'], s['rss_gib'], color='#64748b', lw=1.8, label='Process RSS (sampled)')
            ax.step(s['arm_time_s'], s['arm_gib'], where='post', color=COLORS['C'], lw=1.8, label='Live ARM (sampled)')
            ax.fill_between(s['arm_time_s'], s['arm_gib'], step='post', alpha=.12, color=COLORS['C'])
            ax.set_title('{} · {} thread{}'.format(app.upper(), threads, '' if threads == 1 else 's'), fontsize=12)
            ax.set_ylim(0, coverage_max * 1.06)
            ax.set_xlim(0, s['elapsed_s'])
            ax.grid(axis='y', color='#e2e8f0')
            ax.set_ylabel('GiB')
            if i == 3:
                ax.set_xlabel('Seconds since launcher start')
    axes[0, 0].legend(loc='lower right', frameon=True, fontsize=9)
    fig.suptitle('Dirty→ARM coverage alongside total resident process memory', fontsize=16, fontweight='bold')
    fig.text(.5, .003, 'Preselected first measured block; real elapsed time, including initialization and exit.\n'
             'ARM = live protected 4 KiB pages. RSS is approximate; the two streams are not atomic snapshots.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .04, 1, .96))
    savefig(fig, 'matched-arm-rss')

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)
    peak_rss_max = max(r['target_peak_rss_gib'] for r in data['runs'])
    for ax, threads in zip(axes, (1, 4)):
        for i, app in enumerate(APPS):
            cell = data['cells']['{}-t{}'.format(app, threads)]
            for j, mode in enumerate(MODES):
                values = cell['modes'][mode]['target_peak_rss_gib']['values']
                x = i + (j - 1) * .23
                ax.scatter(x + np.linspace(-.055, .055, 6), values, s=27, alpha=.8, color=COLORS[mode],
                           label=LABELS[mode] if i == 0 else None)
                ax.plot([x - .08, x + .08], [statistics.median(values)] * 2, color=COLORS[mode], lw=3)
        ax.set_title('{} thread{}'.format(threads, '' if threads == 1 else 's'))
        style(ax, 'Target peak RSS (GiB, wait4)')
        ax.set_ylim(0, peak_rss_max * 1.06)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .91), ncol=3, frameon=False, fontsize=10)
    fig.suptitle('Peak application memory in the matched runs', fontsize=16, fontweight='bold')
    fig.text(.5, .01, 'Target-only process peak, not the controller or kernel. CMD-P history allocation is reported separately.', ha='center', fontsize=10)
    fig.tight_layout(rect=(0, .06, 1, .84))
    savefig(fig, 'matched-peak-rss')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('base', type=Path)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--no-plots', action='store_true')
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    data, manifest = collect(a.base)
    data['pass'] = data.pop('pass_')
    data['analysis_source_sha256'] = sha(Path(__file__))
    data['python_version'] = platform.python_version()
    (a.out / 'analysis.json').write_text(json.dumps(data, indent=2) + '\n')
    (a.out / 'input-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (a.out / 'runs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(data['runs'][0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(data['runs'])
    paired = []
    for cell, result in sorted(data['cells'].items()):
        for block in range(6):
            row = dict(cell=cell, block=block + 1,
                off_seconds=result['modes']['A']['elapsed_s']['values'][block],
                sampling_seconds=result['modes']['B']['elapsed_s']['values'][block],
                full_seconds=result['modes']['C']['elapsed_s']['values'][block])
            row.update({key: values['values'][block] for key, values in result['overhead'].items()})
            paired.append(row)
    with (a.out / 'paired-overheads.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(paired[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(paired)
    if not a.no_plots:
        plots(data, a.out)
    for cell, d in data['cells'].items():
        print(cell, json.dumps({k: {m: v[m] for m in ('median', 'minimum', 'maximum')} for k, v in d['overhead'].items()}))


if __name__ == '__main__':
    main()
