#!/usr/bin/env python3
"""Verify all sweep receipts and export paired comparisons, counters and plots.

Inputs remain read-only. Three measured repetitions per stage/cap/thread/mode.
The selected equal cap is measured afresh in stage two; stages are not pooled.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics

MODES = ('A', 'B', 'C')
SIZES = (64, 512, 4096, 16384, 65536)
COLORS = {'A': '#64748b', 'B': '#c67c0d', 'C': '#7651b5'}


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def describe(values):
    return dict(median=statistics.median(values), minimum=min(values), maximum=max(values), values=values)


def collect(base):
    campaign = read(base / 'campaign-summary.json')
    assert campaign['pass'] and campaign['measured_runs'] == 144
    assert campaign['stage1_runs'] == 90 and campaign['stage2_runs'] == 54
    assert (base / 'campaign.exit').read_text().strip() == (base / 'driver.exit').read_text().strip() == '0'
    assert not list(base.glob('failure*.json'))
    audit, host_initial = read(base / 'final-audit.json'), read(base / 'host-initial.json')
    assert audit['passed'] and audit['kernel_source_unchanged']
    assert audit['source_commit'] == campaign['source_commit']
    assert audit['boot_id'] == host_initial['boot_id']
    assert audit['kernel'] == '7.2.6-cmdp-fused-production'
    manifest = {}
    counts = dict(phases=len(campaign['runs']), fixture_checks=0, generated_verifications=0,
                  full_twitter_verifications=0, sustained_smokes=0)
    for name, receipt in campaign['runs'].items():
        path = base / name
        assert receipt == read(path / 'summary.json')
        assert receipt['pass'] and receipt['source_commit'] == campaign['source_commit']
        assert receipt['boot_id'] == audit['boot_id']
        for filename, digest in receipt['evidence_sha256'].items():
            assert sha(path / filename) == digest, str(path / filename)
            manifest[name + '/' + filename] = digest
        manifest[name + '/summary.json'] = sha(path / 'summary.json')
        counts['fixture_checks'] += receipt.get('checks', 0)
        if receipt.get('verify'):
            key = 'generated_verifications' if receipt['generated'] else 'full_twitter_verifications'
            counts[key] += receipt['trials']
        if name.startswith('smoke/') or '/sustained-' in name:
            counts['sustained_smokes'] += 1
    assert counts == dict(phases=220, fixture_checks=582, generated_verifications=240,
                          full_twitter_verifications=16, sustained_smokes=9), counts
    controller_tests = read(base / 'tools/monitor-measurement-tests.json')
    assert controller_tests['pass'] and controller_tests['checks'] == 26
    counts['controller_checks'] = controller_tests['checks']
    assert 'Ran 9 tests' in (base / 'schedule-tests.log').read_text()
    assert (base / 'schedule-tests.log').read_text().rstrip().endswith('OK')
    counts['schedule_tests'] = 9
    collected, groups, references = [], {}, {}
    for stage, expected_count in (('stage1', 90), ('stage2', 54)):
        plan = read(base / (stage + '-plan.json'))
        measured = read(base / (stage + '-measured.json'))
        assert len(plan['schedule']) == len(measured) == expected_count and plan['repetitions'] == 3
        assert plan['cells']['bfs-t1']['trials'] == 38 and plan['cells']['bfs-t4']['trials'] == 127
        pairs = {tuple(pair) for pair in plan['pairs']}
        expected_pairs = {(k, k) for k in SIZES} if stage == 'stage1' else {
            (campaign['selected_k'], campaign['selected_k']),
            (campaign['selected_k'], campaign['selected_k'] // 8),
            (campaign['selected_k'] // 8, campaign['selected_k'])}
        assert pairs == expected_pairs
        for entry, recorded in zip(plan['schedule'], measured):
            d, w = entry['dirty_batch_pages'], entry['wp_batch_pages']
            block, cell, mode = entry['block'], entry['cell'], entry['mode']
            name = 'measured/{}/d{}-w{}/b{:02d}-{}-{}'.format(stage, d, w, block, cell, mode)
            path = base / name
            receipt = campaign['runs'][name]
            assert recorded == dict(receipt, block=block, cell=cell, run=name)
            assert (receipt['dirty_batch_pages'], receipt['wp_batch_pages']) == (d, w)
            assert receipt['mode'] == mode and receipt['application'] == 'bfs'
            assert not receipt['verify'] and not receipt['generated']
            assert receipt['threads'] == plan['cells'][cell]['threads']
            assert receipt['trials'] == plan['cells'][cell]['trials']
            command = read(path / 'command.json')
            assert command['exit'] == 0 and not command.get('timed_out')
            elapsed = (command['launcher_end_ns'] - command['launcher_begin_ns']) / 1e9
            assert abs(elapsed - receipt['elapsed_seconds']) < 1e-8
            log = (path / 'target.log').read_text()
            assert [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)] == receipt['trial_times_s']
            assert len(receipt['trial_times_s']) == receipt['trials'] and 'Verification:' not in log
            assert [int(x) for x in re.findall(r'^Source:\s*(\d+)', log, re.M)] == receipt['sources']
            if cell not in references:
                assert mode == 'A' and block == 1
                references[cell] = receipt['sources']
                work = plan['cells'][cell]
                prefix = work['pilot_sources'][:min(work['trials'], work['pilot_trials'])]
                assert references[cell][:len(prefix)] == prefix
            assert receipt['sources'] == references[cell]
            for side in ('before', 'after'):
                host = read(path / ('host-' + side + '.json'))
                for key in ('kernel', 'boot_id', 'settings', 'governors', 'taint', 'numa_balancing'):
                    assert host[key] == host_initial[key], (name, key)
                assert all(host['stats'][k] == 0 for k in ('entries', 'managed', 'isolated'))
                assert host['stats']['arms'] == host['stats']['publishes'] == host['stats']['revokes']
            final, dirty, progress = receipt['final'], receipt['dirty'], receipt['progress']
            peak = receipt['target_rusage']['maxrss_kib'] / 2**20
            arm = final.get('protected_byte_ms', 0) / 1000 / 2**30 / elapsed
            row = dict(run=name, stage=stage, dirty_batch_pages=d, wp_batch_pages=w,
                pair='{}/{}'.format(d, w), threads=receipt['threads'], block=block, mode=mode,
                trials=receipt['trials'], elapsed_s=elapsed, trial_s=receipt['trial_seconds'],
                child_lifetime_s=receipt['child_lifetime_seconds'], target_peak_rss_gib=peak,
                mean_live_arm_gib=arm, mean_arm_over_peak_rss_pct=100 * arm / peak,
                wp_events=final.get('terminals', {}).get('write', 0),
                wp_service_s=progress.get('write_ns', 0) / 1e9,
                lock_wait_s=progress.get('scan_lock_wait_total_ns', 0) / 1e9,
                lock_retries=progress.get('scan_lock_retries', 0),
                completed_sweeps=receipt['completed_sweeps'],
                dirty_samples=dirty.get('samples', 0), dirty_sample_s=dirty.get('sample_ns', 0) / 1e9,
                target_user_s=receipt['target_rusage']['user_us'] / 1e6,
                target_system_s=receipt['target_rusage']['system_us'] / 1e6)
            monitor = rows(path / 'monitor.jsonl')
            batch = {} if mode == 'A' else read(path / 'validation.json')['batch']
            snapshots = [r for r in monitor if r['event'] == 'batch_status']
            if mode == 'A':
                assert not snapshots and not final
            else:
                assert snapshots and snapshots[-1] == batch
                assert batch['dirty_batch_pages'] == d and batch['wp_batch_pages'] == w
                assert batch['flush_calls'] == sum(batch[k] for k in (
                    'shared_flush_calls', 'dirty_only_flush_calls', 'wp_only_flush_calls'))
                assert batch['max_dirty_pages'] <= d and batch['max_wp_pages'] <= w
                assert batch['max_span_pages'] <= max(d, w)
                assert batch['history_payload_bytes'] == dirty['metadata_bytes']
                assert batch['history_allocated_bytes'] >= batch['history_payload_bytes']
                assert dirty['resource_skips'] == dirty['valid_observations'] == 0
                assert dirty['metadata_bytes'] == final['history_entries'] * 160
                assert dirty['quiet_ms'] == 10000
                if mode == 'B':
                    assert batch['wp_pages'] == batch['shared_flush_calls'] == batch['wp_only_flush_calls'] == 0
            for key in ('flush_calls', 'shared_flush_calls', 'dirty_only_flush_calls', 'wp_only_flush_calls',
                        'dirty_pages', 'wp_pages', 'max_dirty_pages', 'max_wp_pages', 'max_span_pages',
                        'chunks', 'limit_chunks', 'time_chunks', 'region_lookups', 'region_full_skips',
                        'region_skip_pages', 'active_run_skips', 'pmd_walks', 'pmd_cache_hits'):
                row[key] = batch.get(key, 0)
            row['actual_flush_s'] = batch.get('flush_ns', 0) / 1e9
            row['max_scan_hold_ms'] = batch.get('max_hold_ns', 0) / 1e6
            row['history_payload_mib'] = batch.get('history_payload_bytes', 0) / 2**20
            row['history_allocated_mib'] = batch.get('history_allocated_bytes', 0) / 2**20
            row['shared_flush_pct'] = 100 * batch.get('shared_flush_calls', 0) / max(1, batch.get('flush_calls', 0))
            for role in ('dirty', 'wp'):
                calls = batch.get(role + '_only_flush_calls', 0) + batch.get('shared_flush_calls', 0)
                row[role + '_pages_per_flush'] = batch.get(role + '_pages', 0) / calls if calls else 0
            key = (stage, d, w, receipt['threads'])
            groups.setdefault(key, []).append(row)
            collected.append(row)
        for d, w in pairs:
            for cell in ('bfs-t1', 'bfs-t4'):
                order = [''.join(e['mode'] for e in plan['schedule'] if e['block'] == b and
                         e['cell'] == cell and e['dirty_batch_pages'] == d and e['wp_batch_pages'] == w)
                         for b in (1, 2, 3)]
                assert order == ['ABC', 'BCA', 'CAB']
    estimates = []
    for (stage, d, w, threads), group in sorted(groups.items()):
        assert len(group) == 9
        blocks = {(r['block'], r['mode']): r for r in group}
        assert set(blocks) == {(b, m) for b in (1, 2, 3) for m in MODES}
        paired = {}
        for label, num, den in (('sampling_vs_off_pct', 'B', 'A'), ('full_vs_off_pct', 'C', 'A'),
                                ('full_vs_sampling_pct', 'C', 'B')):
            paired[label] = describe([100 * (blocks[b, num]['elapsed_s'] / blocks[b, den]['elapsed_s'] - 1)
                                      for b in (1, 2, 3)])
        ignored = ('run', 'stage', 'dirty_batch_pages', 'wp_batch_pages', 'pair', 'threads', 'block', 'mode', 'trials')
        metrics = [key for key in group[0] if key not in ignored]
        estimates.append(dict(stage=stage, dirty=d, wp=w, threads=threads, overhead=paired,
            modes={mode: {metric: describe([r[metric] for r in group if r['mode'] == mode])
                          for metric in metrics} for mode in MODES}))
    selection = read(base / 'stage2-selection.json')
    scores = {}
    for k in SIZES:
        subset = [e for e in estimates if e['stage'] == 'stage1' and e['dirty'] == e['wp'] == k]
        scores[k] = statistics.mean(e['overhead']['full_vs_off_pct']['median'] for e in subset)
        recorded = next(e for e in selection['estimates'] if e['k'] == k)
        assert abs(scores[k] - recorded['score_pct']) < 1e-10
    assert min(scores, key=lambda k: (scores[k], k)) == selection['selected_k'] == campaign['selected_k']
    for p in sorted(base.glob('*.json')):
        manifest[p.name] = sha(p)
    manifest['campaign.exit'] = sha(base / 'campaign.exit')
    return dict(passed=True, measured_runs=len(collected), repetitions=3, counts=counts,
        source_commit=campaign['source_commit'], kernel=audit['kernel'], selected_k=campaign['selected_k'],
        estimates=estimates, runs=collected, stage1_scores_pct=scores, selection=selection,
        primary='Median of three same-block whole-job runtime ratios to off; stages not pooled.',
        coverage='Median of whole-job time-averaged live ARM; fraction uses each run target peak RSS.',
        wp_scope='Write-triggered CMD-P generation revocations, including cleanup; not every Linux fault.',
        wp_time_scope='CMD-P write-revocation path; excludes the remainder of Linux fault handling.'), manifest


def plots(data, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold',
        'text.color': '#172033', 'axes.edgecolor': '#cbd5e1', 'axes.labelcolor': '#334155',
        'figure.facecolor': 'white', 'savefig.facecolor': 'white', 'pdf.fonttype': 42})
    lookup = {(e['stage'], e['dirty'], e['wp'], e['threads']): e for e in data['estimates']}
    k = data['selected_k']
    second = [(k, k), (k, k // 8), (k // 8, k)]

    def save(fig, name, title, footer):
        fig.suptitle(title, fontsize=17, fontweight='bold', y=.985)
        fig.text(.5, .012, footer, ha='center', fontsize=9, color='#475569')
        fig.tight_layout(rect=(0, .045, 1, .91))
        for extension in ('png', 'svg', 'pdf'):
            fig.savefig(out / (name + '.' + extension), dpi=180, bbox_inches='tight')
        plt.close(fig)

    def range_points(ax, x, stats, color, marker='o', label=None, connect=True):
        centers = [v['median'] for v in stats]
        ax.plot(x, centers, color=color, marker=marker, lw=2 if connect else 0, label=label, zorder=3)
        for position, value in zip(x, stats):
            ax.vlines(position, value['minimum'], value['maximum'], color=color, alpha=.6, lw=1.3)
            ax.scatter([position] * len(value['values']), value['values'], s=19, color=color, alpha=.55, zorder=2)

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.7), sharey=True)
    for ax, threads in zip(axes, (1, 4)):
        for mode, label in (('B', 'Sampling only'), ('C', 'Sampling + dirty→ARM')):
            key = 'sampling_vs_off_pct' if mode == 'B' else 'full_vs_off_pct'
            values = [lookup['stage1', n, n, threads]['overhead'][key] for n in SIZES]
            range_points(ax, SIZES, values, COLORS[mode], label=label)
        ax.set_xscale('log', base=2)
        ax.set_xticks(SIZES)
        ax.set_xticklabels(['64', '512', '4,096', '16,384', '65,536'])
        ax.set_title('{} thread{}'.format(threads, 's' if threads != 1 else ''))
        ax.set_xlabel('Dirty limit = ARM limit (pages)')
        ax.grid(axis='y', alpha=.18); ax.set_ylim(bottom=0)
    axes[0].set_ylabel('End-to-end overhead versus off (%)')
    axes[1].legend(loc='upper left', frameon=False)
    save(fig, 'equal-batch-overhead', 'Equal batch sizes: {} is the best combined candidate'.format(k),
         'Dots: three paired repeats. Connected markers: medians. Lines span observed repeats, not confidence intervals.')

    fig, axes = plt.subplots(1, 2, figsize=(12.8, 4.8), sharey=True)
    highest = 0
    for ax, threads in zip(axes, (1, 4)):
        for mode, offset, label in (('B', -.13, 'Sampling only'), ('C', .13, 'Sampling + dirty→ARM')):
            key = 'sampling_vs_off_pct' if mode == 'B' else 'full_vs_off_pct'
            stats = [lookup['stage2', d, w, threads]['overhead'][key] for d, w in second]
            xs = np.arange(3) + offset
            range_points(ax, xs, stats, COLORS[mode], label=label, connect=False)
            for x, value in zip(xs, stats):
                ax.annotate('{:.2f}%'.format(value['median']), (x, value['median']),
                            xytext=(7, 7), textcoords='offset points',
                            ha='left', fontsize=9, color=COLORS[mode])
                highest = max(highest, value['maximum'])
        ax.set_xticks(range(3)); ax.set_xticklabels(['{}/{}'.format(*pair) for pair in second])
        ax.set_xlim(-.5, 2.5); ax.grid(axis='y', alpha=.18)
        ax.set_xlabel('Dirty / ARM limit (pages)')
        ax.set_title('{} thread{}'.format(threads, 's' if threads != 1 else ''))
    axes[0].set_ylim(0, highest + 1.6)
    axes[0].set_ylabel('End-to-end overhead versus off (%)')
    axes[1].legend(loc='upper right', frameon=False, fontsize=9)
    save(fig, 'asymmetric-batch-overhead', 'Unequal limits did not improve dirty→ARM medians',
         'Fresh {0}/{0} reference; every pair has a {0}-page maximum address window. Labels are paired medians; n = 3.'.format(k))

    fig, axes = plt.subplots(2, 2, figsize=(12.8, 8.1), sharey=True)
    for row, stage in enumerate(('stage1', 'stage2')):
        pairs = [(n, n) for n in SIZES] if stage == 'stage1' else second
        for col, threads in enumerate((1, 4)):
            ax = axes[row, col]
            xs = list(SIZES) if stage == 'stage1' else list(range(3))
            estimates = [lookup[stage, d, w, threads] for d, w in pairs]
            range_points(ax, xs, [e['modes']['C']['mean_live_arm_gib'] for e in estimates],
                         COLORS['C'], label='Mean live ARM')
            ax.plot(xs, [e['modes']['C']['target_peak_rss_gib']['median'] for e in estimates],
                    color=COLORS['A'], linestyle='--', label='Target peak RSS')
            if stage == 'stage1':
                ax.set_xscale('log', base=2); ax.set_xticks(SIZES)
                ax.set_xticklabels(['64', '512', '4,096', '16,384', '65,536'])
                ax.set_xlabel('Equal dirty / ARM limit (pages)')
            else:
                ax.set_xticks(xs); ax.set_xticklabels(['{}/{}'.format(*p) for p in pairs])
                ax.set_xlabel('Dirty / ARM limit (pages)'); ax.set_xlim(-.3, 2.3)
            ax.set_title('{} · {} thread{}'.format('Equal sizes' if stage == 'stage1' else 'Unequal comparison',
                                                 threads, 's' if threads != 1 else ''))
            ax.set_ylim(0, 13.8); ax.grid(axis='y', alpha=.18)
            if col == 0: ax.set_ylabel('Memory (GiB)')
    axes[0, 0].legend(loc='lower left', frameon=False)
    save(fig, 'batch-arm-coverage', 'ARM coverage remains substantial across the tested settings',
         'Mean ARM: whole-job time average, then median of three runs. RSS: median target peak; excludes kernel tracking memory.')

    fig, axes = plt.subplots(2, 2, figsize=(12.8, 8.1), sharey='row')
    for col, threads in enumerate((1, 4)):
        estimates = [lookup['stage1', n, n, threads] for n in SIZES]
        for row, metric, label in ((0, 'flush_calls', 'Unique range-flush calls'),
                                   (1, 'max_scan_hold_ms', 'Maximum scanner hold per run (ms)')):
            ax = axes[row, col]
            range_points(ax, SIZES, [e['modes']['C'][metric] for e in estimates], COLORS['C'])
            ax.set_xscale('log', base=2); ax.set_xticks(SIZES)
            ax.set_xticklabels(['64', '512', '4,096', '16,384', '65,536'])
            if row == 0: ax.set_yscale('log')
            else: ax.set_ylim(bottom=0)
            ax.set_xlabel('Equal dirty / ARM limit (pages)'); ax.set_ylabel(label)
            ax.set_title('{} thread{}'.format(threads, 's' if threads != 1 else ''))
            ax.grid(axis='y', alpha=.18)
    full_equal = [e['modes']['C'] for e in data['estimates'] if e['stage'] == 'stage1']
    axes[0, 0].set_ylim(min(e['flush_calls']['minimum'] for e in full_equal) * .65,
                        max(e['flush_calls']['maximum'] for e in full_equal) * 1.3)
    axes[1, 0].set_ylim(0, max(e['max_scan_hold_ms']['maximum'] for e in full_equal) * 1.12)
    save(fig, 'batch-flush-and-hold', 'Fewer flush calls do not guarantee lower application overhead',
         'Dirty→ARM mode, three repeats. Flush counts are architecture range-flush requests, not IPI counts; holds include completion.')
    (out / 'plot-environment.json').write_text(json.dumps(dict(python=platform.python_version(),
        numpy=np.__version__, matplotlib=matplotlib.__version__), indent=2) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--no-plots', action='store_true')
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    data, manifest = collect(a.base)
    data.update(analysis_source_sha256=sha(Path(__file__)), python_version=platform.python_version())
    (a.out / 'analysis.json').write_text(json.dumps(data, indent=2) + '\n')
    (a.out / 'input-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (a.out / 'runs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(data['runs'][0]))
        writer.writeheader(); writer.writerows(data['runs'])
    flat = []
    for e in data['estimates']:
        row = dict(stage=e['stage'], dirty=e['dirty'], wp=e['wp'], threads=e['threads'])
        for name, stats in e['overhead'].items():
            for field in ('median', 'minimum', 'maximum'):
                row[name + '_' + field] = stats[field]
        for metric, stats in e['modes']['C'].items():
            row['full_' + metric + '_median'] = stats['median']
        flat.append(row)
    with (a.out / 'comparisons.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(flat[0])); writer.writeheader(); writer.writerows(flat)
    if not a.no_plots:
        plots(data, a.out)
    print(json.dumps(dict(passed=True, measured_runs=data['measured_runs'], counts=data['counts'],
        selected_k=data['selected_k'], max_hold_ms=max(r['max_scan_hold_ms'] for r in data['runs']),
        estimates=[dict(stage=e['stage'], dirty=e['dirty'], wp=e['wp'], threads=e['threads'],
            sampling_pct=e['overhead']['sampling_vs_off_pct']['median'],
            full_pct=e['overhead']['full_vs_off_pct']['median'],
            arm_gib=e['modes']['C']['mean_live_arm_gib']['median']) for e in data['estimates']]), indent=2))


if __name__ == '__main__':
    main()
