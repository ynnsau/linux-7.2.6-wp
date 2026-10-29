#!/usr/bin/env python3
"""Verify Redis/YCSB receipts, summarize the sweep and optionally make figures."""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import statistics

P = pathlib.Path
GIB = 1024 ** 3


def sha(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n')


def gc_window(path, start, end):
    """Sum only stop-the-world pause overlap with the selected UTC window."""
    pauses = []
    pattern = re.compile(r'^\[([^]]+)\].*\[gc\s*\].*GC\(\d+\) Pause .* ([0-9.]+)ms$')
    for line in path.read_text().splitlines():
        match = pattern.search(line)
        if not match:
            continue
        finish = datetime.datetime.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S.%f%z').timestamp()
        duration = float(match.group(2))
        overlap = max(0, min(end, finish) - max(start, finish - duration / 1000)) * 1000
        if overlap:
            pauses.append((duration, overlap))
    return {'gc_pause_count': len(pauses), 'gc_pause_ms': sum(p[1] for p in pauses),
            'gc_max_pause_ms': max((p[0] for p in pauses), default=0),
            'gc_pause_pct_of_window': sum(p[1] for p in pauses) / ((end - start) * 10)}


def summarize(root, output):
    state = json.loads((root / 'campaign-state.json').read_text())
    assert state['status'] == 'complete', 'Campaign has not finished'
    config = json.loads((root / 'campaign-config.json').read_text())
    rows, file_count = [], 0
    for run in sorted((root / 'runs').iterdir()):
        assert (run / 'receipt.json').exists(), 'Incomplete run: ' + str(run)
        receipt = json.loads((run / 'receipt.json').read_text())
        assert receipt['passed']
        for name, expected in receipt['files_sha256'].items():
            assert sha(run / name) == expected, str(run / name)
            file_count += 1
        row = json.loads((run / 'summary.json').read_text())
        assert row['admitted'] and row['cmdp_before'] == row['cmdp_after']
        assert row['policy'] == config['kernel_policy']
        assert row['golden_rdb_sha256'] == config['rdb_sha256']
        assert abs(row['measured_seconds'] - 120) <= 0.25
        ops = row['window_end']['operations'] - row['window_start']['operations']
        assert ops == row['successful_operations']
        assert abs(row['qps'] - ops / row['measured_seconds']) < 1e-8
        status = []
        for line in (run / 'ycsb.log').read_text().splitlines():
            m = re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d:\d{3}) (\d+) sec: (\d+) operations;', line)
            if m:
                stamp = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S:%f').replace(tzinfo=datetime.timezone.utc)
                status.append({'epoch': stamp.timestamp(), 'elapsed_s': int(m.group(2)), 'operations': int(m.group(3))})
        start = next(x for x in status if x['elapsed_s'] >= 30)
        end = next(x for x in status if x['elapsed_s'] >= start['elapsed_s'] + 120)
        assert start == row['window_start'] and end == row['window_end']
        middle = min(status, key=lambda x: abs(x['epoch'] - (start['epoch'] + 60)))
        first_qps = (middle['operations'] - start['operations']) / (middle['epoch'] - start['epoch'])
        second_qps = (end['operations'] - middle['operations']) / (end['epoch'] - middle['epoch'])
        row['first_half_qps'] = first_qps
        row['second_half_qps'] = second_qps
        row['second_vs_first_half_pct'] = 100 * (second_qps / first_qps - 1)
        row.update(gc_window(run / 'gc.log', start['epoch'], end['epoch']))
        obs = [json.loads(line) for line in (run / 'observations.jsonl').read_text().splitlines()]
        for x in obs:
            assert x['cmdp'] == row['cmdp_before']
            assert x['redis']['vm_swap_kib'] == x['client']['vm_swap_kib'] == 0
            assert x['info']['evicted_keys'] == x['info']['expired_keys'] == 0
            assert x['info']['aof_enabled'] == x['info']['rdb_bgsave_in_progress'] == 0
            assert x['redis']['allowed'] == '1'
            assert x['client']['allowed'] == '26-%d' % (25 + row['client_cores'])
            assert x['redis_threads_checked'] > 0 and x['client_threads_checked'] > 0
        window = [x for x in obs if start['epoch'] <= x['epoch'] <= end['epoch']]
        assert len(window) >= 118
        first, last = window[0], window[-1]
        span = last['monotonic'] - first['monotonic']
        for process in ('redis', 'client'):
            cpu = 100 * (last[process]['cpu_s'] - first[process]['cpu_s']) / span
            assert abs(cpu - row[process + '_cpu_pct']) < 1e-8
        row['redis_alloc_min'] = min(x['info']['used_memory'] for x in window)
        row['redis_alloc_max'] = max(x['info']['used_memory'] for x in window)
        hist_ops = sum(v['count'] for v in row['latency'].values())
        assert abs(hist_ops - ops) <= max(2 * row['threads'], ops * 0.001)
        for op in ('READ', 'UPDATE'):
            d = row['exported'].get(op)
            if d:
                assert d['Operations'] == d['Return=OK']
                assert all(v == 0 for k, v in d.items() if k.startswith('Return=') and k != 'Return=OK')
        row['run'] = run.name
        row['read_p99_us'] = row['latency']['READ']['p99_us']
        row['update_p99_us'] = row['latency'].get('UPDATE', {}).get('p99_us')
        # NUMA maps reports page counts, not bytes. Keep the raw per-node totals.
        for phase in ('before', 'after'):
            text = (run / ('redis-numa-' + phase + '.txt')).read_text()
            counts = {}
            for node, n in re.findall(r'\bN(\d+)=(\d+)', text):
                counts[node] = counts.get(node, 0) + int(n)
            row['redis_numa_' + phase] = counts
            smaps = (run / ('redis-smaps-' + phase + '.txt')).read_text()
            row['redis_anon_huge_kib_' + phase] = int(re.search(r'^AnonHugePages:\s+(\d+)', smaps, re.M).group(1))
        rows.append(row)
    assert len(rows) == state['completed']
    assert all(r['cmdp_before'] == rows[0]['cmdp_before'] for r in rows), 'CMD-P activity between runs'
    groups = {}
    for row in rows:
        groups.setdefault((row['workload'], row['client_cores'], row['threads']), []).append(row)
    stats = []
    for (w, cores, threads), members in sorted(groups.items()):
        assert len(members) == 3, (w, cores, threads, len(members))
        d = {'workload': w, 'client_cores': cores, 'threads': threads, 'runs': [r['run'] for r in members],
             'qps': [r['qps'] for r in members], 'qps_median': statistics.median(r['qps'] for r in members),
             'qps_min': min(r['qps'] for r in members), 'qps_max': max(r['qps'] for r in members)}
        for field in ('redis_cpu_pct', 'client_pool_util_pct', 'read_p99_us', 'redis_used_memory_mean',
                      'redis_rss_peak', 'client_rss_peak', 'restore_seconds', 'redis_freq_khz_median',
                      'gc_pause_pct_of_window', 'gc_max_pause_ms', 'second_vs_first_half_pct'):
            d[field + '_median'] = statistics.median(r[field] for r in members)
        updates = [r['update_p99_us'] for r in members if r['update_p99_us'] is not None]
        d['update_p99_us_median'] = statistics.median(updates) if updates else None
        stats.append(d)
    for w in 'ABC':
        assert all((w, config['client_cores_initial'], t) in groups for t in config['concurrency'])
    peaks = []
    for w in 'ABC':
        choices = [g for g in stats if g['workload'] == w]
        best = max(choices, key=lambda x: x['qps_median'])
        knee = min((g for g in choices if g['qps_median'] >= 0.97 * best['qps_median']),
                   key=lambda x: (x['threads'], x['client_cores']))
        matching = [r for r in rows if r['run'] in best['runs']]
        representative = min(matching, key=lambda x: abs(x['qps'] - best['qps_median']))['run']
        peaks.append(dict(best, representative_run=representative, near_peak_threads=knee['threads'],
                          near_peak_client_cores=knee['client_cores'],
                          server_saturated=best['redis_cpu_pct_median'] >= 95))
    before = (root / 'dmesg-before.txt').read_text().splitlines()
    after = (root / 'dmesg-after.txt').read_text().splitlines()
    old = set(before)
    new = [l for l in after if l not in old]
    alerts = [l for l in new if re.search(r'BUG:|Oops:|soft lockup|hard LOCKUP|Out of memory|oom-kill|Kernel panic', l, re.I)]
    assert not alerts, alerts
    result = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'complete': True,
              'verified_runs': len(rows), 'verified_evidence_files': file_count,
              'config': config, 'groups': stats, 'peaks': peaks, 'runs': rows,
              'new_kernel_messages': new, 'kernel_alerts': alerts}
    output.mkdir(parents=True, exist_ok=True)
    save(output / 'redis-ycsb-summary.json', result)
    return result


def report(out, data):
    """Write a compact measured report alongside the machine-readable evidence."""
    rows, groups, peaks = data['runs'], data['groups'], data['peaks']
    def value(x, digits=1):
        return '—' if x is None else ('{:,.' + str(digits) + 'f}').format(x)
    lines = [
        '# Redis–YCSB baseline results', '',
        'Maximum observed sustained successful logical YCSB operations/s for this setup. '
        'Each configuration has three repetitions, each with 30 seconds of warm-up and '
        '120 measured seconds. No rate cap or latency SLA was imposed.', '',
        'Redis uses CPU1 and memory node0. The YCSB JVM uses a separate CPU pool on node1. '
        'Every run reloads the same saved database. CMD-P sampling and ARM remain inactive.', '',
        '## Peak throughput', '',
        '| Workload | Workers / client cores | Median ops/s | Three-run range | Redis CPU | Read / update p99 (µs) | Fewest workers within 3% |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |'
    ]
    for p in peaks:
        lines.append('| %s | %d / %d | %s | %s–%s | %.1f%% | %s / %s | %d (%d cores) |' % (
            p['workload'], p['threads'], p['client_cores'], value(p['qps_median']),
            value(p['qps_min']), value(p['qps_max']), p['redis_cpu_pct_median'],
            value(p['read_p99_us_median'], 0), value(p['update_p99_us_median'], 0),
            p['near_peak_threads'], p['near_peak_client_cores']))
    lines += ['', 'The latency entries are medians of the three per-run p99 values; they are not '
              'percentiles pooled across repetitions. Per-run percentiles merge histograms only '
              'inside the selected measurement window.', '', '## Memory and validation', '',
              '- Records: **%s**, with ten 100-byte fields, default hashed keys and the Redis binding’s index.' % value(rows[0]['recordcount'], 0),
              '- Measured Redis allocation across all observations: **%.3f–%.3f GiB**; largest Redis RSS: **%.3f GiB**.' % (
                  min(r['redis_alloc_min'] for r in rows)/GIB, max(r['redis_alloc_max'] for r in rows)/GIB,
                  max(r['redis_rss_peak'] for r in rows)/GIB),
              '- Snapshot restoration: median **%.2f s**, range **%.2f–%.2f s**, excluded from throughput. File cache was not dropped.' % (
                  statistics.median(r['restore_seconds'] for r in rows), min(r['restore_seconds'] for r in rows), max(r['restore_seconds'] for r in rows)),
              '- Verified **%d runs** and **%d evidence-file hashes**.' % (data['verified_runs'], data['verified_evidence_files']),
              '- Zero non-OK operations, swaps, evictions, expirations or persistence activity; CMD-P counters unchanged.',
              '- Host policy and thread affinity checks passed. No new kernel crash/OOM/lockup messages were found.',
              '- Golden RDB SHA-256: `%s`.' % data['config']['rdb_sha256'], '',
              '## Stability diagnostics at each peak', '',
              '| Workload | Individual ops/s | Second-half change, each run | Median GC pause fraction | Median longest GC pause |',
              '| --- | --- | --- | ---: | ---: |']
    for p in peaks:
        members = [r for r in rows if r['run'] in p['runs']]
        lines.append('| %s | %s | %s | %.3f%% | %.2f ms |' % (
            p['workload'], ', '.join(value(r['qps']) for r in members),
            ', '.join('%+.1f%%' % r['second_vs_first_half_pct'] for r in members),
            p['gc_pause_pct_of_window_median'], p['gc_max_pause_ms_median']))
    initial_cores = data['config']['client_cores_initial']
    base = [g for g in groups if g['client_cores'] == initial_cores]
    decisions = []
    for w in 'ABC':
        choices = sorted((g for g in base if g['workload'] == w), key=lambda g: g['threads'])
        best, gains = 0, []
        for g in choices:
            gains.append(max(0, 100 * (g['qps_median'] / best - 1)) if best else 100)
            best = max(best, g['qps_median'])
        decisions.append('- %s: largest tested concurrency %d; the last two doublings improved '
                         'the running best median by %.3f%% and %.3f%% (%s the 3%% threshold).' % (
                             w, choices[-1]['threads'], gains[-2], gains[-1],
                             'both below' if all(x < 3 for x in gains[-2:]) else 'not both below'))
    pools = sorted({g['client_cores'] for g in groups})
    decisions += ['', 'Client CPU pools tested: %s cores. Peak Redis CPU and client-pool '
                  'utilization are recorded above and in the full sweep below.' % ', '.join(map(str, pools)), '']
    common = []
    for t in sorted({g['threads'] for g in base}):
        candidates = [g for g in base if g['threads'] == t]
        if len(candidates) == 3 and all(g['qps_median'] >= .97 * next(p['qps_median'] for p in peaks if p['workload'] == g['workload']) for g in candidates):
            common.append((t, candidates))
    if common:
        threads, candidates = common[0]
        loss = max(100 * (1 - g['qps_median'] / next(p['qps_median'] for p in peaks if p['workload'] == g['workload'])) for g in candidates)
        decisions += ['**%d workers on %d client cores** are a useful common baseline: every '
                      'workload is within %.3f%% of its observed peak.' % (threads, initial_cores, loss), '']
        for g in candidates:
            p = next(p for p in peaks if p['workload'] == g['workload'])
            if (p['threads'], p['client_cores']) != (threads, initial_cores):
                decisions.append('%s: the common setting has read p99 %.0f µs versus %.0f µs '
                                 'at the observed peak setting (%d workers / %d cores).' % (
                                     p['workload'], g['read_p99_us_median'], p['read_p99_us_median'], p['threads'], p['client_cores']))
        decisions.append('')
    lines += ['', 'Second-half change compares the final 60 seconds against the first 60 seconds '
              'of the measured window. It is a diagnostic, not a post-hoc exclusion rule. '
              'GC values cover stop-the-world pause overlap with that same window.', '',
              '## Sweep stopping decision', ''] + decisions + [
              '## Full concurrency sweep', '',
              '| Workload | Client cores | Workers | Median ops/s | Min–max ops/s | Redis CPU | Client pool CPU | Read p99 (µs) | Update p99 (µs) |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for g in groups:
        lines.append('| %s | %d | %d | %s | %s–%s | %.1f%% | %.1f%% | %s | %s |' % (
            g['workload'], g['client_cores'], g['threads'], value(g['qps_median']),
            value(g['qps_min']), value(g['qps_max']), g['redis_cpu_pct_median'],
            g['client_pool_util_pct_median'], value(g['read_p99_us_median'], 0), value(g['update_p99_us_median'], 0)))
    lines += ['', '## Interpretation limits', '',
              'These are observed rates for the pinned versions, data, CPU/NUMA placement and '
              'existing host frequency policy. Three repetitions show run-to-run variation but '
              'do not establish a universal Redis limit. Logical YCSB operations and Redis '
              'command counts differ; the observer also issues a small number of INFO commands. '
              'No sampled/ARM comparison was included in this baseline.', '']
    if data['new_kernel_messages']:
        lines += ['New kernel messages are preserved below. The campaign did not start perf '
                  'profiling. No crash/OOM/lockup diagnostics were found.', '', '```text']
        lines += data['new_kernel_messages'] + ['```', '']
    (out / 'redis-ycsb-report.md').write_text('\n'.join(lines))


def plots(root, out, data):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    colors = {'A': '#c45f23', 'B': '#197d88', 'C': '#5854a4'}
    labels = {'A': 'A · 50% reads / 50% updates', 'B': 'B · 95% reads / 5% updates', 'C': 'C · reads only'}
    plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False,
                         'figure.facecolor': 'white', 'axes.titleweight': 'bold'})

    def finish(fig, name):
        fig.savefig(out / (name + '.png'), dpi=170, bbox_inches='tight')
        fig.savefig(out / (name + '.svg'), bbox_inches='tight')
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), gridspec_kw={'width_ratios': [1.6, 1]})
    for w in 'ABC':
        g = sorted([g for g in data['groups'] if g['workload'] == w and g['client_cores'] == 8], key=lambda x: x['threads'])
        x, y = [r['threads'] for r in g], [r['qps_median']/1000 for r in g]
        err = [[(r['qps_median']-r['qps_min'])/1000 for r in g], [(r['qps_max']-r['qps_median'])/1000 for r in g]]
        axes[0].errorbar(x, y, yerr=err, fmt='o-', capsize=3, color=colors[w], label=labels[w])
    axes[0].set_xscale('log', base=2)
    ticks = sorted({g['threads'] for g in data['groups'] if g['client_cores'] == 8})
    axes[0].set_xticks(ticks)
    axes[0].xaxis.set_major_formatter(ScalarFormatter())
    axes[0].set(xlabel='YCSB workers · JVM on CPUs 26–33', ylabel='Thousand successful operations/s', ylim=(0, None))
    axes[0].legend(fontsize=9)
    axes[0].grid(axis='y', alpha=.2)
    for i, p in enumerate(data['peaks']):
        axes[1].bar(i, p['qps_median']/1000, color=colors[p['workload']], width=.6)
        axes[1].scatter([i-.08, i, i+.08], [v/1000 for v in p['qps']], color='#222222', s=20, zorder=3)
        axes[1].text(i, p['qps_max']/1000 + 1.5, '%.1fk\n%d workers' % (p['qps_median']/1000, p['threads']), ha='center', fontsize=10)
    axes[1].set_xticks(range(3), ['A', 'B', 'C'])
    axes[1].set(ylabel='Thousand successful operations/s', title='Highest median; dots = 3 runs')
    axes[1].set_ylim(0, max(p['qps_max'] for p in data['peaks'])/1000*1.25)
    axes[1].grid(axis='y', alpha=.2)
    fig.suptitle('Redis on one core: YCSB throughput without sampling or ARM', fontsize=14, weight='bold')
    fig.text(.5, -.015, '16 GiB Redis allocation · client on the other socket · 30 s warm-up + 120 s measurement · 3 repetitions', ha='center', fontsize=9)
    fig.tight_layout()
    finish(fig, 'redis-ycsb-throughput')

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for w in 'ABC':
        g = sorted([g for g in data['groups'] if g['workload'] == w and g['client_cores'] == 8], key=lambda x: x['threads'])
        axes[0].plot([r['threads'] for r in g], [r['redis_cpu_pct_median'] for r in g], 'o-', color=colors[w], label='Redis '+w)
        axes[0].plot([r['threads'] for r in g], [r['client_pool_util_pct_median'] for r in g], 'o--', color=colors[w], alpha=.65, label='Client pool '+w)
        axes[1].plot([r['threads'] for r in g], [r['read_p99_us_median']/1000 for r in g], 'o-', color=colors[w], label=w+' read')
        if w != 'C':
            axes[1].plot([r['threads'] for r in g], [r['update_p99_us_median']/1000 for r in g], 'x--', color=colors[w], label=w+' update')
    for ax in axes:
        ax.set_xscale('log', base=2)
        ax.set_xticks(ticks)
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.set_xlabel('YCSB workers')
        ax.grid(axis='y', alpha=.2)
        ax.legend(fontsize=9, ncol=2)
    axes[0].set(ylabel='CPU utilization (%)', ylim=(0, 105), title='Redis core versus 8-core client pool')
    axes[1].set(ylabel='p99 latency (ms)', ylim=(0, None), title='Median of three per-run p99 values')
    fig.suptitle('Concurrency, saturation and latency', fontsize=14, weight='bold')
    fig.tight_layout()
    finish(fig, 'redis-ycsb-cpu-latency')

    fig, axes = plt.subplots(3, 2, figsize=(12, 9), sharex=True)
    qps_upper = max(p['qps_max'] for p in data['peaks']) / 1000 * 1.15
    memory_upper = max(max(r['redis_alloc_max'], r['redis_rss_peak']) for r in data['runs']) / GIB * 1.1
    for i, peak in enumerate(data['peaks']):
        run = root / 'runs' / peak['representative_run']
        row = next(r for r in data['runs'] if r['run'] == run.name)
        obs = [json.loads(l) for l in (run/'observations.jsonl').read_text().splitlines()]
        t0 = row['window_start']['epoch'] - 30
        pattern = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d:\d{3}) (\d+) sec: (\d+) operations;')
        stat = []
        for line in (run/'ycsb.log').read_text().splitlines():
            m = pattern.search(line)
            if m:
                dt = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S:%f').replace(tzinfo=datetime.timezone.utc).timestamp()
                stat.append((dt, int(m.group(3))))
        ts, qs = [], []
        for a, b in zip(stat, stat[1:]):
            # The final JVM status can cover a partial shutdown interval.
            # Show warm-up and the full measured window; keep shutdown in raw logs.
            if b[0] > a[0] and b[0] <= row['window_end']['epoch']:
                ts.append(b[0]-t0);qs.append((b[1]-a[1])/(b[0]-a[0])/1000)
        axes[i,0].plot(ts, qs, color=colors[peak['workload']], lw=1.2)
        axes[i,0].axhline(peak['qps_median']/1000, color='#333333', ls=':', lw=1)
        axes[i,0].set(ylabel='Thousand ops/s', title='%s · %d workers' % (labels[peak['workload']], peak['threads']))
        axes[i,0].set_ylim(0,qps_upper)
        axes[i,1].plot([x['epoch']-t0 for x in obs], [x['info']['used_memory']/GIB for x in obs], label='Redis allocation', color=colors[peak['workload']])
        axes[i,1].plot([x['epoch']-t0 for x in obs], [x['redis']['rss_bytes']/GIB for x in obs], label='Redis RSS', color='#333333', ls='--')
        axes[i,1].set(ylabel='GiB', title='Redis memory', ylim=(0, memory_upper))
        for ax in axes[i]:
            ax.axvspan(0,30,color='#dddddd',alpha=.5)
            ax.axvline(150,color='#999999',ls=':',lw=1)
            ax.set_xlim(0,150)
            ax.set_xticks([0,30,60,90,120,150])
            ax.grid(axis='y',alpha=.2)
    axes[0,1].legend(fontsize=9)
    axes[-1,0].set_xlabel('Seconds · shaded first 30 s excluded')
    axes[-1,1].set_xlabel('Seconds · measurement ends at 150 s')
    fig.suptitle('Representative peak runs: throughput and memory over time', fontsize=14, weight='bold')
    fig.tight_layout()
    finish(fig,'redis-ycsb-timelines')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('campaign', type=P)
    ap.add_argument('output', type=P)
    ap.add_argument('--plots', action='store_true')
    args = ap.parse_args()
    data = summarize(args.campaign, args.output)
    report(args.output, data)
    if args.plots:
        plots(args.campaign, args.output, data)
    print(json.dumps({'runs': data['verified_runs'], 'files': data['verified_evidence_files'], 'peaks': data['peaks']}, indent=2))


if __name__ == '__main__':
    main()
