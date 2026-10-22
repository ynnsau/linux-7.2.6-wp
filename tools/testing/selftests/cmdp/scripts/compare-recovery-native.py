#!/usr/bin/env python3
"""Compare completed native recovery observations with historical dirty runs.

Read-only with respect to all inputs. Writes only --output, which must be absent
or empty. Requires the tracked recovery analyzer and its dependencies under
--scripts-dir; imports reuse their validation and sampled-zero methodology.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text())


def load(path):
    spec = importlib.util.spec_from_file_location('recovery_comparison_analyzer', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def work_details(app, run, previous):
    command = read(run / 'command.json')
    text = (run / 'target.log').read_text()
    trials = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', text)]
    expected = int(command['argv'][command['argv'].index('-n') + 1])
    require(len(trials) == expected and all(math.isfinite(x) and x > 0 for x in trials),
            'Invalid complete trial evidence: ' + str(run))
    require(command['exit'] == 0 and not command.get('timed_out'), 'Failed command: ' + str(run))
    require(not re.search(r'Verification:', text), 'Observation contains verifier activity')
    work = {'trials': len(trials), 'trial_compute_seconds': sum(trials),
            'trial_times_seconds': trials, 'command_argv': command['argv'],
            'application_and_monitor_elapsed_seconds': command['elapsed_seconds']}
    if app == 'pr':
        iterations, current = [], []
        for line in text.splitlines():
            match = re.fullmatch(r'\s*(\d+)\s+([0-9.eE+-]+)\s*', line)
            if match:
                current.append((int(match.group(1)), float(match.group(2))))
            elif 'Trial Time:' in line:
                require(current and [x[0] for x in current] == list(range(len(current))),
                        'Missing/nonsequential PR iteration log')
                iterations.append({'iterations': len(current),
                                   'last_logged_change_rounded': current[-1][1]})
                current = []
        require(not current and len(iterations) == len(trials), 'Incomplete PR iteration groups')
        work['pagerank_trials'] = iterations
        work['pagerank_max_iterations'] = int(command['argv'][command['argv'].index('-i') + 1])
        work['pagerank_tolerance'] = float(command['argv'][command['argv'].index('-t') + 1])
        work['pagerank_note'] = 'Rounded logged iterate changes are not independent verification residuals.'
    return work


def case(base, app, threads, kind, analyzer, saved):
    previous = analyzer.previous
    if kind == 'recovery':
        report, unused = analyzer.analyze(base, app, threads)
        phase = base / ('{}-twitter-observe-t{}'.format(app, threads))
        saved_report = saved['{}-{}'.format(app, threads)]
    else:
        report, unused = previous.analyze_case(base, threads)
        phase = base / ('twitter-observe-t' + str(threads))
        saved_report = saved['runs'][str(threads)]
    # Both existing analyses must agree with fresh raw-counter reconstruction.
    for field in ('counts', 'kernel_protection_integral', 'final_status'):
        require(report[field] == saved_report[field],
                'Saved analysis differs from raw evidence: {} {}'.format(phase, field))
    require(not report['kernel_protection_integral']['saturated'], 'Saturated protection integral')
    run = phase / 'run'
    rows = previous.read_jsonl(run / 'monitor.jsonl')
    statuses = [x for x in rows if x.get('event') in ('status', 'observation_end', 'final')]
    command = read(run / 'command.json')
    exe = command['argv'][command['argv'].index('--') + 1]
    thread_rows = [x for x in previous.read_jsonl(run / 'thread-samples.jsonl')
                   if x.get('exe') == exe and len(x.get('threads', [])) == threads]
    # Validate target affinity/activity consistently for old and new raw inputs.
    activity = previous.analyze_threads(thread_rows, threads, report['time_origin']['attach_ns'], exe)
    origin = report['time_origin']['attach_ns']
    stop_ns = report['sampled_live']['kernel_stop_request_ns']
    spans = analyzer.zero_spans(statuses, thread_rows, stop_ns, origin)
    running = [r for r in statuses if r['stop_reason'] == 'none' and (not stop_ns or r['now_ns'] < stop_ns)]
    positive = [r for r in running if r['active'] > 0]
    first_positive = positive[0]['now_ns'] if positive else None
    for span in spans:
        span['classification'] = ('startup_before_first_positive_sample'
                                  if first_positive is None or span['end_ns'] <= first_positive
                                  else 'later_after_positive_sample')
        span['cpu_progress_interval_count'] = len(span['cpu_progress_intervals'])
        span['cpu_ticks_increase_sum'] = sum(x['cpu_ticks_increase'] for x in span['cpu_progress_intervals'])
    grouped = {}
    for group in ('startup_before_first_positive_sample', 'later_after_positive_sample'):
        selected = [s for s in spans if s['classification'] == group]
        grouped[group] = {'spans': len(selected), 'sampled_zero_seconds': sum(s['seconds'] for s in selected),
                          'spans_with_cpu_progress': sum(s['target_cpu_progress_observed'] for s in selected),
                          'cpu_progress_intervals': sum(s['cpu_progress_interval_count'] for s in selected),
                          'cpu_ticks_increase_sum': sum(s['cpu_ticks_increase_sum'] for s in selected)}
    monitored_seconds = (report['final_status']['now_ns'] - origin) / 1e9
    require(monitored_seconds > 0, 'Empty monitored interval')
    integral = report['kernel_protection_integral']['gib_seconds_at_4k']
    work = work_details(app, run, previous)
    profile = read(phase / 'phase-profile.json')
    require(profile['quiet_ms'] == profile['period_ms'] == 10000 and profile['poll_ms'] == 1000,
            'Not the common Q/period/poll protocol')
    audit = read(base / 'postboot-audit.json')
    result = {'application': app, 'threads': threads, 'variant': kind,
              'kernel': audit['kernel'], 'boot_id': audit['boot_id'], 'source_commit': audit['source_commit'],
              'kernel_image_sha256': audit.get('kernel_image_sha256'),
              'graph_sha256': audit.get('graph_sha256'),
              'work': work, 'counts': report['counts'], 'sweeps': report['sweeps'],
              'protected_gib_seconds': integral, 'monitored_attach_through_final_seconds': monitored_seconds,
              'average_protected_gib_over_attach_through_final': integral / monitored_seconds,
              'average_denominator': 'final status timestamp minus monitor attachment timestamp; includes startup and final cleanup',
              'sampled_peak': report['sampled_live']['peak'],
              'last_running_sample': report['sampled_live']['last_running_status'],
              'stop_seconds_since_attach': report['sampled_live']['kernel_stop_request_seconds_since_attach'],
              'first_positive_sample_seconds': (first_positive - origin) / 1e9 if first_positive else None,
              'zero_arm_sampled_spans': spans, 'zero_arm_groups': grouped,
              'thread_activity': activity,
              'input_sha256': {str(p): sha(p) for p in (phase / 'summary.json', phase / 'phase-profile.json',
                    run / 'monitor.jsonl', run / 'target.log', run / 'thread-samples.jsonl',
                    run / 'command.json', base / 'postboot-audit.json')}}
    plotted = [{'seconds': (r['now_ns'] - origin) / 1e9, 'gib': r['active'] * 4096 / 2**30,
                'running_before_stop': r in running, 'stop_reason': r['stop_reason']} for r in statuses]
    return result, plotted


def draw(results, plots, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import MaxNLocator
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'svg.fonttype': 'none'})
    colors = {'historical': '#b65d0a', 'recovery': '#1768ac'}
    styles = {'historical': '--', 'recovery': '-'}
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.3), sharex=True, sharey=True)
    fig.subplots_adjust(left=.085, right=.98, bottom=.16, top=.81, hspace=.34, wspace=.15)
    xmax = max(p['seconds'] for values in plots.values() for p in values)
    ymax = max(p['gib'] for values in plots.values() for p in values)
    for row, app in enumerate(('bfs', 'pr')):
        for col, threads in enumerate((1, 4)):
            ax = axes[row, col]
            ax.set_title('{} | {} thread{}'.format(app.upper(), threads, '' if threads == 1 else 's'),
                         loc='left', fontsize=13, weight='bold')
            caption = []
            for kind in ('historical', 'recovery'):
                key = '{}-{}-{}'.format(app, threads, kind)
                data, result = plots[key], results[key]
                running = [p for p in data if p['running_before_stop']]
                cleanup = [p for p in data if not p['running_before_stop']]
                ax.plot([p['seconds'] for p in running], [p['gib'] for p in running],
                        color=colors[kind], linestyle=styles[kind], linewidth=2)
                if running:
                    ax.scatter([running[-1]['seconds']], [running[-1]['gib']],
                               color=colors[kind], s=22, zorder=5)
                if cleanup:
                    ax.scatter([p['seconds'] for p in cleanup], [p['gib'] for p in cleanup],
                               color=colors[kind], marker='x', s=34, linewidths=1.3, zorder=6, clip_on=False)
                stop = result['stop_seconds_since_attach']
                if stop is not None:
                    ax.axvline(stop, color=colors[kind], linestyle=':', linewidth=1, alpha=.6)
                caption.append('{}: {} trial{}, {:.1f} s'.format(kind.title(), result['work']['trials'],
                               '' if result['work']['trials'] == 1 else 's',
                               result['work']['application_and_monitor_elapsed_seconds']))
            ax.text(.97, .95, '\n'.join(caption), ha='right', va='top', transform=ax.transAxes,
                    fontsize=9, bbox={'facecolor': 'white', 'alpha': .88, 'edgecolor': 'none', 'pad': 3})
            ax.set_xlim(0, xmax * 1.025)
            ax.set_ylim(0, max(1, ymax) * 1.28)
            ax.grid(axis='y', alpha=.22)
            ax.xaxis.set_major_locator(MaxNLocator(6))
            if col == 0: ax.set_ylabel('Live ARM (GiB; 4 KiB pages)')
            if row == 1: ax.set_xlabel('Seconds since monitor attachment')
    fig.text(.085, .966, 'Twitter graph: live ARM before and after recovery', fontsize=19, weight='bold')
    fig.text(.085, .924, 'Same axes in every panel | Q = 10 s | sweep period = 10 s | poll = 1 s', color='#555555')
    handles = [Line2D([0], [0], color=colors['recovery'], lw=2, label='Recovery kernel'),
               Line2D([0], [0], color=colors['historical'], lw=2, ls='--', label='Historical dirty kernel'),
               Line2D([0], [0], color='#555555', marker='o', linestyle='None', markersize=4, label='Last running sample'),
               Line2D([0], [0], color='#555555', marker='x', linestyle='None', label='Cleanup sample'),
               Line2D([0], [0], color='#555555', linestyle=':', label='Kernel STOP')]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(.078, .895), ncol=5,
               frameon=False, fontsize=9, handlelength=2)
    fig.text(.085, .078, 'Curves end at recorded running samples; no extrapolation. Startup, loading and teardown are included.', fontsize=9, color='#555555')
    fig.text(.085, .049, 'Work and duration differ (BFS trial counts and PR 4-thread trials). This is not an overhead comparison.', fontsize=9, color='#555555')
    for ext in ('png', 'pdf', 'svg'):
        fig.savefig(str(output / ('historical-recovery-live-arm.' + ext)), dpi=180)
    plt.close(fig)


def markdown(results, output):
    lines = ['# Historical dirty kernel and recovery kernel observations', '',
             'Full-run values below include startup, loading and final cleanup. Historical runs are not matched performance baselines.', '',
             '| Case | Kernel | Elapsed s | Trials | Trial compute s | ARM generations | WP generations | Invalidations | Protected GiB-s | Mean protected GiB |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for key, r in results.items():
        lines.append('| {} {}T | {} | {:.3f} | {} | {:.3f} | {:,} | {:,} | {:,} | {:.3f} | {:.4f} |'.format(
            r['application'].upper(), r['threads'], r['variant'], r['work']['application_and_monitor_elapsed_seconds'],
            r['work']['trials'], r['work']['trial_compute_seconds'], r['counts']['ARM'], r['counts']['WP'],
            r['counts']['terminals']['invalidate'], r['protected_gib_seconds'],
            r['average_protected_gib_over_attach_through_final']))
    lines += ['', 'Mean protected GiB is the kernel protection integral divided by the actual monitor-attachment-to-final-status interval, '
              'including startup and cleanup. Its denominator differs slightly from application-plus-monitor elapsed time. '
              'ARM and WP counts refer to generations, not unique pages; no total-process coverage percentage is inferred.', '',
              '| Case | Kernel | Monitored s (mean denominator) | Peak sampled GiB | Last running GiB | Last running time s |',
              '| --- | --- | ---: | ---: | ---: | ---: |']
    for r in results.values():
        lines.append('| {} {}T | {} | {:.3f} | {:.4f} | {:.6f} | {:.3f} |'.format(
            r['application'].upper(), r['threads'], r['variant'], r['monitored_attach_through_final_seconds'],
            r['sampled_peak']['active_gib_at_4k'], r['last_running_sample']['active_gib_at_4k'],
            r['last_running_sample']['seconds_since_attach']))
    lines += ['', 'The last running sample can already include mapping teardown. It is not an exact final useful-coverage measurement. '
              'Sampled peaks are lower bounds on instantaneous peaks.', '',
              '## Workload differences', '']
    for app in ('bfs', 'pr'):
        for threads in (1, 4):
            old, new = [results['{}-{}-{}'.format(app, threads, k)] for k in ('historical', 'recovery')]
            line = '- {} {} thread{}: historical {} complete trials; recovery {} complete trials.'.format(
                app.upper(), threads, '' if threads == 1 else 's', old['work']['trials'], new['work']['trials'])
            if app == 'pr':
                line += ' Logged iterations per trial: historical {}; recovery {}.'.format(
                    [t['iterations'] for t in old['work']['pagerank_trials']],
                    [t['iterations'] for t in new['work']['pagerank_trials']])
            lines.append(line)
    lines += ['', '## Sampled zero-ARM spans', '',
              'Startup means before the first positive sampled ARM value; later means after a positive sample. These labels '
              'describe coverage samples, not application phases. The same tracked analyzer function is applied to both versions. '
              'CPU evidence counts positive tick changes in fully enclosed thread-sampling intervals with the expected executable/thread count. '
              'Consecutive zero samples do not exclude intervening ARM episodes.', '',
              '| Case | Kernel | Group | Span start–end s | Duration s | CPU progress observed | CPU intervals | CPU ticks |',
              '| --- | --- | --- | --- | ---: | --- | ---: | ---: |']
    for r in results.values():
        for s in r['zero_arm_sampled_spans']:
            lines.append('| {} {}T | {} | {} | {:.3f}–{:.3f} | {:.3f} | {} | {} | {} |'.format(
                r['application'].upper(), r['threads'], r['variant'],
                'startup' if s['classification'].startswith('startup') else 'later',
                s['start_seconds'], s['end_seconds'], s['seconds'],
                'yes' if s['target_cpu_progress_observed'] else 'no enclosed evidence',
                s['cpu_progress_interval_count'], s['cpu_ticks_increase_sum']))
        if not r['zero_arm_sampled_spans']:
            lines.append('| {} {}T | {} | — | no qualifying sampled span | — | — | — | — |'.format(
                r['application'].upper(), r['threads'], r['variant']))
    lines += ['', '## Limits', '',
              '- Whole-run totals and protected GiB-seconds depend on duration and work. Mean protected GiB adjusts duration only; it does not match graph-algorithm work or startup proportions.',
              '- The old and new measurements occur on different boots with different instrumentation and kernel behavior. No end-to-end overhead, speedup, causal performance effect or statistical confidence interval is established.',
              '- Historical logs do not contain the new invalidation-source diagnostics; their invalidation totals cannot be retrospectively assigned to NUMA or another callback path.',
              '- CPU progress inside a zero-ARM span can be loading, computation or teardown. The logs do not identify exact algorithm-phase boundaries.',
              '- Timeline lines connect sampled values. Curves are never extended beyond their recorded running observations; cleanup samples and STOP are distinguished.', '']
    (output / 'comparison.md').write_text('\n'.join(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--new', type=Path, required=True)
    p.add_argument('--old-bfs', type=Path, required=True)
    p.add_argument('--old-pr', type=Path, required=True)
    p.add_argument('--scripts-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    # Do all input validation before creating the output directory.
    require(not a.output.exists() or (a.output.is_dir() and not any(a.output.iterdir())), 'Output must be absent or empty')
    analyzer = load(a.scripts_dir / 'analyze-recovery-native.py')
    new_file = a.new / 'recovery-analysis/analysis.json'
    old_files = {app: getattr(a, 'old_' + app) / 'analysis/analysis.json' for app in ('bfs', 'pr')}
    new = read(new_file)
    olds = {app: read(path) for app, path in old_files.items()}
    results, plots = {}, {}
    for app in ('bfs', 'pr'):
        for threads in (1, 4):
            for kind in ('historical', 'recovery'):
                base = a.new if kind == 'recovery' else getattr(a, 'old_' + app)
                saved = new if kind == 'recovery' else olds[app]
                key = '{}-{}-{}'.format(app, threads, kind)
                results[key], plots[key] = case(base, app, threads, kind, analyzer, saved)
    a.output.mkdir(parents=True, exist_ok=True)
    metadata = {'pass': True, 'results': results,
                'script_sha256': sha(Path(__file__)),
                'analysis_input_sha256': {str(path): sha(path) for path in [new_file] + list(old_files.values())},
                'analyzer_sha256': {str(path): sha(path) for path in [a.scripts_dir / name for name in
                   ('analyze-recovery-native.py', 'analyze-dirty-native.py', 'validate-run.py')]},
                'method': 'Tracked raw-log validators and zero_spans function, applied identically to historical/recovery inputs; no fitted model or performance inference.'}
    draw(results, plots, a.output)
    markdown(results, a.output)
    (a.output / 'plot-data.json').write_text(json.dumps(plots, indent=2) + '\n')
    (a.output / 'comparison.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps({'pass': True, 'cells': len(results), 'output': str(a.output)}))


if __name__ == '__main__':
    main()
