#!/usr/bin/env python3
"""Build a report from complete, validated before/after campaign analyses."""
import argparse
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    args = parser.parse_args()
    root = args.bundle
    data = read(root / 'analysis/analysis.json')
    compare = read(root / 'analysis/comparison.json')
    audit = read(root / 'run/final-audit.json')
    provenance = read(root / 'run/provenance.json')
    manifest = read(root / 'run/raw-manifest.json')
    assert data['pass'] and compare['pass'] and audit['pass']
    assert data['measured_runs'] == 144
    cells = [f'{app}-t{t}' for app in ('bfs', 'pr', 'cc', 'bc') for t in (1, 4)]

    def label(cell):
        app, t = cell.split('-t')
        return f'{app.upper()} · {t}'

    def span(values, digits=1):
        return f'{min(values):.{digits}f}–{max(values):.{digits}f}'

    def dist(values, digits=1, suffix=''):
        return f"{values['median']:.{digits}f}{suffix} [{values['minimum']:.{digits}f}–{values['maximum']:.{digits}f}]"

    def overhead_range(metric, version):
        return span([compare['cells'][c]['overhead'][metric][version]['median'] for c in cells])

    def event_range(d):
        return f"{d['minimum']:,}–{d['maximum']:,}"

    logs = [json.loads(line) for line in (root / 'run/campaign.log').read_text().splitlines() if line.startswith('{')]
    start = next(x['at'] for x in logs if x.get('stage') == 'running' and x.get('run', '').startswith('measured/'))
    end = read(root / 'run/campaign-summary.json')['at']
    times = [datetime.fromisoformat(t).astimezone(ZoneInfo('America/Chicago')).strftime('%Y-%m-%d %H:%M:%S %Z') for t in (start, end)]
    sampling = 'sampling_vs_off_pct'
    full = 'full_vs_off_pct'
    incremental = 'full_vs_sampling_pct'
    drift = [compare['cells'][c]['off_median_drift_pct'] for c in cells]
    lower = sum(compare['cells'][c]['overhead'][full]['change_percentage_points'] < 0 for c in cells)
    coverage_changes = []
    for cell, name in (('pr-t4', 'PR-4'), ('cc-t4', 'CC-4')):
        modes = compare['cells'][cell]['modes']['C']
        old = modes['previous']['mean_live_arm_gib']['median']
        new = modes['batched']['mean_live_arm_gib']['median']
        coverage_changes.append(f'{name}: {old:.2f} → {new:.2f} GiB ({100 * (new / old - 1):+.1f}%)')
    coverage_text = ('Median whole-job mean ARM changed for ' + '; '.join(coverage_changes) +
        '. These measurements compare the same policy on fixed work; protected memory exposure varies. '
        'The experiment does not isolate the cause of the coverage change.')
    peak_low = min(data['cells'][c]['modes'][m]['target_peak_rss_gib']['minimum'] for c in cells for m in 'ABC')
    peak_high = max(data['cells'][c]['modes'][m]['target_peak_rss_gib']['maximum'] for c in cells for m in 'ABC')
    peak_span_mib = 1024 * max(
        max(data['cells'][c]['modes'][m]['target_peak_rss_gib']['maximum'] for m in 'ABC') -
        min(data['cells'][c]['modes'][m]['target_peak_rss_gib']['minimum'] for m in 'ABC') for c in cells)
    history_low = min(data['cells'][c]['modes'][m]['history_mib']['minimum'] for c in cells for m in 'BC')
    history_high = max(data['cells'][c]['modes'][m]['history_mib']['maximum'] for c in cells for m in 'BC')
    lines = [
        '# Batched GAPBS overhead — 2026-10-22', '',
        '**144/144 measured runs passed:** BFS, PR, CC and BC; one/four threads; off (A), dirty sampling only (B), and sampling + dirty→ARM (C); six matched blocks. No measured failures, retries or exclusions.', '',
        f"Median sampling overhead changed from **{overhead_range(sampling, 'previous')}%** to **{overhead_range(sampling, 'batched')}%** across the eight cells. Full dirty→ARM overhead changed from **{overhead_range(full, 'previous')}%** to **{overhead_range(full, 'batched')}%**. Full-mode median overhead was lower in {lower}/8 cells. These are ranges of cell medians, not pooled estimates or confidence intervals.", '',
        f'Measured campaign: {times[0]} through {times[1]}. Kernel: `7.2.6-cmdp-batch-production`. The earlier campaign used `7.2.6-cmdp-rearm-production`.', '',
        '## Before and after batching', '',
        'Each percentage is the median of six within-block runtime ratios to monitoring off on the same boot. Both campaigns use byte-identical frozen work/schedule, the same graph, GAPBS binaries, controller binary, timing executor and recorded controls. All three requested optimizations are enabled together; this comparison does not separate their individual contributions.', '',
        '| Workload · threads | Sampling, previous | Sampling, batched | Dirty→ARM, previous | Dirty→ARM, batched | Full-mode change (percentage points) |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for c in cells:
        o = compare['cells'][c]['overhead']
        values = [o[m][v]['median'] for m, v in ((sampling, 'previous'), (sampling, 'batched'), (full, 'previous'), (full, 'batched'))]
        lines.append('| ' + label(c) + ' | ' + ' | '.join(f'{v:.1f}%' for v in values) + f" | {o[full]['change_percentage_points']:+.1f} |")
    lines += ['', '![Overhead before and after batching](analysis/batching-overhead-comparison.png)', '',
        '## Repeated-run variation', '',
        'Median [minimum–maximum] of six paired percentages, `100 × (numerator / denominator − 1)`. Thin plot ranges describe observed variation, not confidence intervals. Every A/B/C permutation occurs once per cell. All successful observations are retained, including slower cases.', '',
        '| Workload · threads | Sampling / off | Dirty→ARM / off | Dirty→ARM / sampling |',
        '| --- | ---: | ---: | ---: |']
    for c in cells:
        o = data['cells'][c]['overhead']
        lines.append('| ' + label(c) + ' | ' + ' | '.join(dist(o[m], suffix='%') for m in (sampling, full, incremental)) + ' |')
    lines += ['', 'C/B compares two active policies. C can skip pages already under ARM and changes scan/admission/recovery activity. A negative C/B result is retained; it does not imply a negative fault-handler cost. Percentage changes are not additive.', '',
        '![All matched overhead ratios on the batched kernel](analysis/matched-overhead.png)', '',
        '## Fixed work, runtime and baseline drift', '',
        f'Off-mode median runtime drift across boots ranges from {min(drift):+.2f}% to {max(drift):+.2f}%. The before/after overhead comparison normalizes each mode to its own matched off run. Across-boot differences remain observational even with unchanged recorded controls.', '',
        '| Workload · threads | Trials | Off A (s) | Sampling B (s) | Dirty→ARM C (s) | Off drift vs previous |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for c in cells:
        d = data['cells'][c]
        runtime = ' | '.join(f"{d['modes'][m]['elapsed_s']['median']:.2f}" for m in 'ABC')
        lines.append(f"| {label(c)} | {d['work']['trials']} | {runtime} | {compare['cells'][c]['off_median_drift_pct']:+.2f}% |")
    lines += ['', 'Primary time spans common-launcher invocation through blocking wait completion, including graph loading/initialization, all algorithm trials, target exit and controller STOP/drain. Cache warming is outside timing. PR uses 21 fixed iterations (`-t 0`); BC uses one source per trial. PR-1 retains one complete trial even though it exceeds two minutes. Work counts differ across thread counts, so these runtimes do not measure thread speedup.', '',
        '![Off baseline across boots](analysis/batching-off-baseline.png)', '',
        '![All current runtimes](analysis/matched-runtime.png)', '',
        '## Coverage, WP events and memory', '',
        'Mean live ARM integrates protected byte-time over the entire job. WP counts are CMD-P generations terminated by a write, not unique pages or every Linux fault. The same ten-second policy can produce different sweep counts and phase alignment when runtime changes; coverage and WP changes therefore need to be read alongside runtime.', '',
        '| Workload · threads | Mean ARM previous (GiB) | Mean ARM batched (GiB) | WP previous, range | WP batched, range | Batched C sweeps, range |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for c in cells:
        m = compare['cells'][c]['modes']['C']
        old, new = m['previous'], m['batched']
        lines.append(f"| {label(c)} | {old['mean_live_arm_gib']['median']:.2f} | {new['mean_live_arm_gib']['median']:.2f} | {event_range(old['wp_events'])} | {event_range(new['wp_events'])} | {event_range(new['completed_sweeps'])} |")
    lines += ['', coverage_text, '', '![Coverage and WP events before and after batching](analysis/batching-coverage-wp.png)', '',
        '![Predeclared first-block live ARM and RSS](analysis/matched-arm-rss.png)', '',
        'The time-series figure uses the predeclared first measured block, not a case selected for favorable timing or coverage. Exit-time ARM falls are cleanup. Unprotected RSS can include recently written, not-yet-qualified, unsupported or unscanned pages.', '',
        '![Target peak RSS](analysis/matched-peak-rss.png)', '',
        f'Target peak RSS ranges from {peak_low:.2f} to {peak_high:.2f} GiB across workloads; the largest within-cell span across all modes/repeats is {peak_span_mib:.3f} MiB. Peak RSS comes from child resource usage; the one-second RSS observer remains active in all modes. Target RSS excludes CMD-P kernel allocations and the controller. Final history-object accounting in B/C ranges from {history_low:.1f} to {history_high:.1f} MiB and excludes XArray/cohort allocator overhead and other kernel allocations. Per-run bytes and diagnostic timers are retained in the data exports.', '',
        '## Verify that batching occurred', '',
        'Counters below are medians of six per-run total-page / total-call ratios, from before/after host snapshots. Calls are synchronous architecture range-flush requests, not IPI counts or individual hardware instructions. Every run satisfies the 64-page bound; off mode has zero deltas, sampling-only has no ARM flushes, and full mode exercises both paths.', '',
        '| Workload · threads | Dirty pages/call, B | Dirty pages/call, C | ARM pages/call, C |',
        '| --- | ---: | ---: | ---: |']
    for c in cells:
        b = compare['cells'][c]['batching']
        values = [b[mode][metric]['median'] for mode, metric in (('B', 'dirty_pages_per_flush'), ('C', 'dirty_pages_per_flush'), ('C', 'arm_pages_per_flush'))]
        lines.append('| ' + label(c) + ' | ' + ' | '.join(f'{v:.2f}' for v in values) + ' |')
    lines += ['', '![Observed batching](analysis/batching-flush-coverage.png)', '',
        'The new counters also record active-page skips and page lookups in `batch-counters.csv`. The older `dirty_status.flushes` field counts page events, not range-flush calls. The batched `arm_ns` sums overlapping per-entry latencies and must not be treated as CPU time or added to other timers. The WP-service timer covers revoke service only, not the whole Linux fault path. End-to-end runtime remains the overhead measure.', '',
        '## Controls and validation', '',
        '- Postboot image/build/source audit passed; eight native tools built. Gates passed 26 launcher/accounting checks, 582 native fixture checks (321 existing + 261 batching), 192 generated graph verifications and 16 full-Twitter graph verifications. The complete BFS-1 A/B/C smoke block was excluded by design.',
        '- Every measured case passed work-count/source/iteration, mode, thread/affinity/THP, exit, accounting and host-health checks. Final ownership is zero and cumulative ARM/publish/revoke totals agree. No new kernel BUG/Oops/stall was recorded and taint stayed unchanged. The kernel log contains five informational perf sampling-rate reductions. Boot, image, graph, binaries, source and recorded controls were rechecked after timing.',
        '- Quiet interval and requested period: 10 seconds; observation: 1 second; limits: 8,388,608 pages/history entries. CPU 0 or CPUs 0–3, all node 0; OpenMP dynamic teams off, fixed placement. Target THP disabled, host NUMA balancing on, governor `ondemand`. Same warm-cache procedure before every case.',
        '- A opens no CMD-P device and creates no scanner. B samples/resets dirty state without ARM. C samples, qualifies quiet pages, ARMs and recovers after invalidation. Existing supported private-anonymous resident 4 KiB CPU-writer scope and dummy backend are unchanged.',
        '- Local VPN interruptions affected monitoring only. After reconnecting, the detached driver had continued with the same PID and host boot ID; no run was restarted or excluded for an interruption.',
        '- No kernel, GRUB, reboot or host-setting changes were made during this measurement round. Measurement source stayed frozen until all runs and the final audit completed.', '',
        '## Evidence and reproduction', '',
        '- [144 per-run rows](analysis/runs.csv), [48 paired rows](analysis/paired-overheads.csv), [batch/page counters](analysis/batch-counters.csv).',
        '- [Current statistics](analysis/analysis.json), [before/after comparison](analysis/comparison.json), [analysis input hashes](analysis/input-manifest.json), [archive copy verification](archive-verification.json).',
        '- [Cross-environment verification](analysis/cross-environment-verification.json): Python 3.9.6 locally and Python 3.6.9 on BMP reproduce identical numerical results and CSV files.',
        '- [Unchanged primary analyzer](analysis/analyze-matched-gapbs.py), [comparison analyzer](analysis/compare-batched-overhead.py), [report builder](analysis/build-batched-report.py). All figures also have SVG/PDF copies.', '',
        '```sh',
        'python3 analysis/analyze-matched-gapbs.py /path/to/bundle/run --out /tmp/batched-analysis',
        'python3 analysis/compare-batched-overhead.py \\',
        '  --before-analysis /path/to/previous/analysis/analysis.json \\',
        '  --after-analysis /tmp/batched-analysis/analysis.json \\',
        '  --after-run /path/to/bundle/run --out /tmp/batched-analysis',
        '```', '',
        'The raw campaign is preserved at BMP `/home/yans3/cmdp-v2/periodic/matched-batched-gapbs-20261022-v1` and in the Obsidian results archive alongside fresh preparation and operational records. The repository report contains derived data/figures and analysis code; raw host logs stay in the experiment archive.', '',
        f"The raw campaign manifest covers {len(manifest):,} files ({sum(v['bytes'] for v in manifest.values()):,} bytes), excluding itself. Copied sizes and SHA-256 digests are verified in the archive receipt.", '',
        f"Measurement source: `{provenance['source_commit']}`. Kernel behavior: `{provenance['kernel_commit']}`. Running build ID: `{provenance['running_build_id']}`. Graph SHA-256: `{provenance['graph_sha256']}`. Frozen work SHA-256: `51409f90a551bb349b45cbe355031628976a6c95215c39fbade8e18020a32b74`.", '',
    ]
    (root / 'README.md').write_text('\n'.join(lines))
    print(root / 'README.md')


if __name__ == '__main__':
    main()
