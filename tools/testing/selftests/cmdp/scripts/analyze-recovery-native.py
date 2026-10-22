#!/usr/bin/env python3
"""Analyze the completed four-cell native recovery matrix; never run workloads.

Rates use actual monotonic sample intervals. Zero-ARM spans are sampled estimates,
with separately observed target CPU progress; they are not exact algorithm phase
boundaries or proof of zero coverage between samples. No overhead is inferred.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


previous = load('previous_analysis', SCRIPTS / 'analyze-dirty-native.py')
validator = load('recovery_validation', SCRIPTS / 'validate-run.py')


def rates(rows, origin, counters):
    out = []
    for a, b in zip(rows, rows[1:]):
        dt = (b['now_ns'] - a['now_ns']) / 1e9
        if dt <= 0: raise ValueError('Non-increasing rate timestamps')
        values = {name: (function(b) - function(a)) / dt for name, function in counters.items()}
        if any(x < 0 for x in values.values()): raise ValueError('Decreasing cumulative count')
        out.append(dict(values, seconds=(b['now_ns'] - origin) / 1e9, interval_seconds=dt))
    return out


def zero_spans(statuses, thread_rows, stop_ns, origin):
    running = [r for r in statuses if r['stop_reason'] == 'none' and (not stop_ns or r['now_ns'] < stop_ns)]
    spans = []
    for a, b in zip(running, running[1:]):
        if a['active'] or b['active']: continue
        if spans and spans[-1]['end_ns'] == a['now_ns']:
            spans[-1]['end_ns'] = b['now_ns']
        else:
            spans.append({'start_ns': a['now_ns'], 'end_ns': b['now_ns']})
    for span in spans:
        evidence = []
        for a, b in zip(thread_rows, thread_rows[1:]):
            if a['monotonic_ns'] < span['start_ns'] or b['monotonic_ns'] > span['end_ns']: continue
            at = {t['tid']: t['utime_ticks'] + t['stime_ticks'] for t in a['threads']}
            bt = {t['tid']: t['utime_ticks'] + t['stime_ticks'] for t in b['threads']}
            if set(at) != set(bt): continue
            growth = {tid: bt[tid] - at[tid] for tid in at}
            if any(x > 0 for x in growth.values()):
                evidence.append({'start_ns': a['monotonic_ns'], 'end_ns': b['monotonic_ns'],
                                 'cpu_ticks_increase': sum(growth.values())})
        span.update({'start_seconds': (span['start_ns'] - origin) / 1e9,
                     'end_seconds': (span['end_ns'] - origin) / 1e9,
                     'seconds': (span['end_ns'] - span['start_ns']) / 1e9,
                     'target_cpu_progress_observed': bool(evidence),
                     'cpu_progress_intervals': evidence})
    return spans


def analyze(base, app, threads):
    phase = base / ('{}-twitter-observe-t{}'.format(app, threads)); run = phase / 'run'
    summary = previous.read_json(phase / 'summary.json')
    if not summary['pass']: raise ValueError('Failed phase: ' + str(phase))
    audit_path = base / 'postboot-audit.json'
    audit = previous.read_json(audit_path)
    source = previous.read_json(phase / 'source-provenance.json')
    if not (audit['recovery_ready'] and summary['host_after']['boot_id'] == audit['boot_id'] and
            summary['host_after']['kernel'] == audit['kernel'] and
            summary['source_commit'] == source['commit'] == audit['source_commit'] and
            summary['kernel_commit'] == source['kernel_reference'] == audit['kernel_commit'] and
            source['kernel_image_sha256'] == audit['kernel_image_sha256'] and
            source['running_build_id'] == audit['kernel_build_id'] and
            source['graph_sha256'] == audit['graph_sha256'] and
            source['postboot_audit_sha256'] == hashlib.sha256(audit_path.read_bytes()).hexdigest()):
        raise ValueError('Matrix phase is not bound to the same boot/source/image/build-ID/graph audit')

    validation = validator.validate(run)
    if not validation['pass'] or not validation['diagnostics']:
        raise ValueError('Incomplete recovery diagnostics: ' + str(validation['errors']))
    result, original_plot = previous.analyze_monitor(run / 'monitor.jsonl')
    rows = previous.read_jsonl(run / 'monitor.jsonl')
    statuses = [r for r in rows if r.get('event') in ('status', 'observation_end', 'final')]
    diagnostics = [r for r in rows if r.get('event') == 'diagnostics']
    origin = result['time_origin']['attach_ns']
    thread_rows = previous.read_jsonl(run / 'thread-samples.jsonl')
    command = previous.read_json(run / 'command.json')
    expected_exe = command['argv'][command['argv'].index('--') + 1]
    if expected_exe != audit[app + '_path'] or source[app + '_sha256'] != audit[app + '_sha256']:
        raise ValueError('Application executable is not bound to the audited BFS/PR binary')
    result['thread_activity'] = previous.analyze_threads(thread_rows, threads, origin, expected_exe)
    target_rows = [r for r in thread_rows if r.get('exe') == expected_exe and len(r.get('threads', [])) == threads]
    result['diagnostics'] = validation['diagnostics']
    result['provenance'] = {'audit_sha256': source['postboot_audit_sha256'], 'boot_id': audit['boot_id'],
                            'source_commit': audit['source_commit'], 'kernel_build_id': audit['kernel_build_id'],
                            'kernel_image_sha256': audit['kernel_image_sha256'], 'graph_sha256': audit['graph_sha256']}
    result['zero_arm_sampled_spans'] = zero_spans(statuses, target_rows,
        result['sampled_live']['kernel_stop_request_ns'], origin)
    result['zero_arm_cpu_progress_note'] = ('Consecutive zero ARM samples before STOP, with CPU ticks increasing '
        'in fully enclosed thread-sampling intervals. Sampling can miss intermediate ARM episodes; CPU work '
        'can include loading, computation or teardown, not an identified algorithm phase.')
    result['application'] = app; result['threads'] = threads
    result['application_and_monitor_elapsed_seconds'] = command['elapsed_seconds']
    result['workload'] = summary['runs']['run']
    result['kernel_commit'] = summary['kernel_commit']
    result['source_files_sha256'] = {str(path.relative_to(phase)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (phase / 'summary.json', phase / 'source-provenance.json', run / 'monitor.jsonl', run / 'target.log',
                     run / 'thread-samples.jsonl', run / 'command.json')}
    sources = sorted(diagnostics[-1]['invalidation_sources'])
    rejected = sorted(diagnostics[-1]['rejected'])
    plot = {'status': original_plot['status'], 'stop_seconds': result['sampled_live']['kernel_stop_request_seconds_since_attach'],
            'event_rates': rates(statuses, origin, {
                'ARM': lambda r: r['first_arms'] + r['rearms'],
                'WP': lambda r: r['terminals']['write'],
                'Invalidation': lambda r: r['terminals']['invalidate']}),
            'source_revocation_rates': rates(diagnostics, origin, {
                name: (lambda r, key=name: r['invalidation_sources'][key]['arm_revoked']) for name in sources}),
            'rejection_rates': rates(diagnostics, origin, {
                name: (lambda r, key=name: r['rejected'][key]) for name in rejected})}
    return result, plot


def draw(data, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(4, 4, figsize=(18, 12), sharex='col', sharey='row', constrained_layout=True)
    colors = {1: {'ARM': '#1565c0', 'WP': '#c62828', 'Invalidation': '#ef6c00'}}
    for row, key in ((2, 'source_revocation_rates'), (3, 'rejection_rates')):
        categories = sorted({name for plot in data.values() for sample in plot[key]
                             for name in sample if name not in ('seconds', 'interval_seconds')})
        colors[row] = {name: plt.get_cmap('tab20')(i % 20) for i, name in enumerate(categories)}
    for column, (name, plot) in enumerate(data.items()):
        ax = axes[0, column]
        active = [r for r in plot['status'] if r['stop_reason'] == 'none']
        cleanup = [r for r in plot['status'] if r['stop_reason'] != 'none']
        ax.plot([r['seconds_since_attach'] for r in active], [r['active_gib_at_4k'] for r in active], color='#1565c0')
        if cleanup:
            ax.scatter([r['seconds_since_attach'] for r in cleanup], [r['active_gib_at_4k'] for r in cleanup],
                       color='#777777', s=18, marker='x', label='cleanup')
        ax.set_title(name.upper()); ax.set_ylabel('Live ARM (GiB)'); ax.set_ylim(bottom=0)
        for row, key, label in ((1, 'event_rates', 'Events / second'),
                                (2, 'source_revocation_rates', 'ARM lost / second'),
                                (3, 'rejection_rates', 'Rejected samples / second')):
            ax = axes[row, column]; series = plot[key]
            keys = sorted({k for r in series for k in r if k not in ('seconds', 'interval_seconds')})
            for name in keys:
                values = [r[name] for r in series]
                if any(values) or key == 'event_rates':
                    ax.step([r['seconds'] for r in series], values, where='pre', label=name, color=colors[row][name])
            ax.set_yscale('symlog', linthresh=1); ax.set_ylim(bottom=0); ax.set_ylabel(label + '\n(log above 1)')
            if ax.lines: ax.legend(fontsize=7, loc='upper right')
        axes[3, column].set_xlabel('Seconds since monitor attachment')
        for ax in axes[:, column]:
            ax.grid(True, alpha=.2)
            if plot['stop_seconds'] is not None: ax.axvline(plot['stop_seconds'], color='#888888', linestyle=':', linewidth=.8)
    fig.suptitle('ARM recovery: coverage, events, invalidation sources and sampling exclusions\n'
                 'Actual sample intervals; vertical dotted line = STOP; end-to-end overhead is not measured', fontsize=14)
    for suffix in ('png', 'pdf', 'svg'): fig.savefig(str(output / ('recovery-timeline.' + suffix)), dpi=160)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('base', type=Path)
    p.add_argument('--output', type=Path)
    a = p.parse_args(); out = a.output or a.base / 'recovery-analysis'
    out.mkdir(parents=True, exist_ok=False)
    results, plots = {}, {}
    for app in ('bfs', 'pr'):
        for threads in (1, 4):
            key = '{}-{}'.format(app, threads)
            results[key], plots[key] = analyze(a.base, app, threads)
    if len({r['kernel_commit'] for r in results.values()}) != 1:
        raise ValueError('Matrix used different kernel implementations')
    (out / 'analysis.json').write_text(json.dumps(results, indent=2) + '\n')
    (out / 'plot-data.json').write_text(json.dumps(plots, indent=2) + '\n')
    draw(plots, out)
    lines = ['# Native ARM recovery observations', '',
             '| Workload | Elapsed s | ARM admissions | WP | Invalidations | Protected GiB-s | CPU-active sampled-zero spans |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, r in results.items():
        lines.append('| {} | {:.2f} | {} | {} | {} | {:.2f} | {} |'.format(name,
            r['application_and_monitor_elapsed_seconds'], r['counts']['ARM'], r['counts']['WP'],
            r['counts']['terminals']['invalidate'], r['kernel_protection_integral']['gib_seconds_at_4k'],
            sum(s['target_cpu_progress_observed'] for s in r['zero_arm_sampled_spans'])))
    lines += ['', 'ARM admissions count generations, not unique pages. Invalidation-source rates attribute the '
              'callback path, not necessarily the ultimate cause. Rejection reasons describe failed sampling attempts.', '',
              'Sampled zero spans require consecutive zero observations before STOP. CPU progress inside a span '
              'shows the target was active, but does not identify graph load, algorithm or teardown phases. '
              'Intermediate ARM episodes may occur between samples. Final cleanup is plotted separately.', '',
              'No matched unmonitored baseline was collected by this protocol; these results do not measure '
              'end-to-end instrumentation overhead. Write-helper time is only one component.']
    (out / 'analysis.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'pass': True, 'cells': list(results), 'output': str(out)}))


if __name__ == '__main__':
    main()
