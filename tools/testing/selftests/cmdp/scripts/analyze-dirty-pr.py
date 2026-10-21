#!/usr/bin/env python3
"""Analyze saved one/four-thread dirty-qualified Twitter PageRank runs; never run tests.

Usage: python3 analyze-dirty-pr.py RUN_BASE [--output OUTPUT_DIR] [--dirty-row]
Inputs are twitter-observe-t{1,4}/run logs plus the corresponding phase summaries.
Outputs are analysis.json, analysis.md, plot-data.json and ARM/WP PNG/PDF/SVG.
The requested 120 s budget covers total application/controller elapsed time;
whole converged PageRank trials cause trial-sized duration quantization.
All timestamps use the monitor's CLOCK_MONOTONIC attachment origin. The final
cleanup sample is shown separately from the last sampled running status.
"""
import argparse
import collections
import hashlib
import json
import math
import re
import statistics
from pathlib import Path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text())


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def monotonic(rows, keys):
    for key in keys:
        require(all(a[key] <= b[key] for a, b in zip(rows, rows[1:])), key + ' decreased')


def timing(progress):
    calls = progress['write_calls']
    return {'calls': calls, 'elapsed_sum_ns': progress['write_ns'],
            'elapsed_sum_ms': progress['write_ns'] / 1e6,
            'elapsed_mean_us': progress['write_ns'] / calls / 1000 if calls else None,
            'elapsed_max_us': progress['write_max_ns'] / 1000,
            'sample_now_ns': progress['now_ns']}


def point(row, origin):
    return {'now_ns': row['now_ns'], 'seconds_since_attach': (row['now_ns'] - origin) / 1e9,
            'active_pages': row['active'], 'active_gib_at_4k': row['active'] * 4096 / 2**30,
            'ARM': row['first_arms'] + row['rearms'], 'WP': row['terminals']['write'],
            'first_arms': row['first_arms'], 'rearms': row['rearms'],
            'passes_started': row['passes_started'], 'passes_completed': row['passes_completed'],
            'overruns': row['overruns'], 'state': row['state'], 'stop_reason': row['stop_reason']}


def analyze_monitor(path):
    rows = read_jsonl(path)
    finals = [r for r in rows if r.get('event') == 'final']
    require(len(finals) == 1, 'Expected one final status')
    final = finals[0]
    statuses = [r for r in rows if r.get('event') in ('status', 'observation_end', 'final')]
    require(len(statuses) > 1, 'Need at least two status samples')
    origin = statuses[0]['attach_ns']
    require(all(a['now_ns'] < b['now_ns'] for a, b in zip(statuses, statuses[1:])),
            'Status timestamps must increase strictly')
    require(all(r['attach_ns'] == origin for r in statuses), 'Attachment origin changed')
    monotonic(statuses, ('first_arms', 'rearms', 'passes_started', 'passes_completed', 'overruns', 'protected_byte_ms'))
    for key in final['terminals']:
        require(all(a['terminals'][key] <= b['terminals'][key] for a, b in zip(statuses, statuses[1:])),
                'Terminal counter decreased: ' + key)
    for r in statuses:
        require(r['first_arms'] + r['rearms'] == r['active'] + sum(r['terminals'].values()),
                'Protection-generation conservation failed')
    require(final['active'] == 0 and final['stop_reason'] in ('target_exit', 'control_stop'), 'Target did not exit and drain normally')
    require(final['resource_failures'] == final['reports_dropped'] == 0, 'Resource/report limit encountered')
    require([r['exit_code'] for r in rows if r.get('event') == 'target_exit'] == [0], 'Target exit failed')
    require([r['exit_code'] for r in rows if r.get('event') == 'controller_exit'] == [0], 'Controller exit failed')
    stops = [r for r in rows if r.get('event') == 'stop_return']
    require(len(stops) == 1 and stops[0]['errno'] == 0, 'STOP failed')
    progress = [r for r in rows if r.get('event') == 'progress']
    dirty_rows = [r for r in rows if r.get('event') == 'dirty_status']
    require(progress and dirty_rows, 'Progress/dirty samples missing')
    monotonic(progress, ('now_ns', 'write_calls', 'write_ns', 'write_max_ns'))
    monotonic(dirty_rows, ('now_ns', 'samples', 'dirty', 'clean', 'primed', 'unknown', 'qualified',
                         'late_dirty', 'resets', 'flushes', 'reset_ns', 'flush_ns', 'sample_ns',
                         'invalidations', 'wp_resets', 'resource_skips'))
    for r in dirty_rows:
        require(r['samples'] == r['dirty'] + r['clean'], 'Dirty + clean observation counts differ from samples')
    last_progress, dirty = progress[-1], dirty_rows[-1]
    require(dirty['resource_skips'] == 0, 'Dirty metadata resource limit encountered')
    cohorts = sorted({r['pass_id']: r for r in rows if r.get('event') == 'cohort_final'}.values(),
                     key=lambda r: r['pass_id'])
    require(len(cohorts) == final['passes_started'], 'Missing final sweep reports')
    require(sum(bool(r['complete']) for r in cohorts) == final['passes_completed'], 'Completed sweep count mismatch')
    for r in rows:
        if r.get('event', '').startswith('cohort_') or r.get('event') == 'sweep':
            require(r['first_arms'] + r['rearms'] == r['active'] + r['write_revoked'] + r['other_terminal'],
                    'Cohort generation conservation failed')
            require(r['active'] == r['active_first'] + r['active_rearm'], 'Cohort active decomposition failed')
    for r in cohorts:
        require(r['end_ns'] >= r['start_ns'], 'Invalid sweep interval')
        require((r['complete'] and r['error'] == 0) or (not r['complete'] and r['error'] in (0, -108, -3)),
                'Unexpected sweep error')
    stop_ns = last_progress['stop_request_ns']
    running = [r for r in statuses if r['stop_reason'] == 'none' and (not stop_ns or r['now_ns'] < stop_ns)]
    require(running, 'No running status sample')
    peak = max(running, key=lambda r: r['active'])
    last_positive = next((r for r in reversed(running) if r['active'] > 0), None)
    trailing_zeros = [r for r in running if last_positive is not None and r['now_ns'] > last_positive['now_ns']]
    boundaries = {}
    for boundary in (60, 120):
        r = next((r for r in running if r['now_ns'] - origin >= boundary * 1e9), None)
        boundaries[str(boundary)] = point(r, origin) if r else None
    complete_durations = [(r['end_ns'] - r['start_ns']) / 1e9 for r in cohorts if r['complete']]
    page_seconds = sum((a['active'] + b['active']) * .5 * (b['now_ns'] - a['now_ns']) / 1e9
                       for a, b in zip(statuses, statuses[1:]))
    result = {
        'kernel_protection_integral': {'protected_byte_ms': final['protected_byte_ms'],
            'gib_seconds_at_4k': final['protected_byte_ms'] / (1000 * 2**30),
            'method': 'Kernel active-byte time accounting at state transitions; integer byte-millisecond rounding',
            'saturated': final['protected_byte_ms'] == 2**64 - 1},
        'sampled_protection_integral': {'page_seconds': page_seconds,
            'gib_seconds_at_4k': page_seconds * 4096 / 2**30,
            'method': 'Trapezoidal integration of sampled active-page counts over actual CLOCK_MONOTONIC timestamp gaps, through final cleanup',
            'estimate_not_exact_lifetime_integral': True,
            'first_sample_ns': statuses[0]['now_ns'], 'last_sample_ns': statuses[-1]['now_ns']},
        'time_origin': {'clock': 'CLOCK_MONOTONIC', 'attach_ns': origin,
                        'controller': next((r for r in rows if r.get('event') == 'controller'), None)},
        'sweeps': {'started': final['passes_started'], 'completed': final['passes_completed'],
                   'missed_deadlines': final['overruns'], 'partial': len(cohorts) - final['passes_completed'],
                   'completed_duration_sum_seconds': sum(complete_durations),
                   'completed_duration_min_seconds': min(complete_durations) if complete_durations else None,
                   'completed_duration_median_seconds': statistics.median(complete_durations) if complete_durations else None,
                   'completed_duration_max_seconds': max(complete_durations) if complete_durations else None},
        'counts': {'first_arms': final['first_arms'], 'rearms': final['rearms'],
                   'ARM': final['first_arms'] + final['rearms'], 'WP': final['terminals']['write'],
                   'terminals': final['terminals'], 'active_after_cleanup': final['active'],
                   'pre_stop_active': final['pre_stop_active'], 'history_entries': final['history_entries'],
                   'history_resets': final['history_resets'], 'resource_failures': final['resource_failures'],
                   'reports_dropped': final['reports_dropped']},
        'sampled_live': {'peak': point(peak, origin), 'last_running_status': point(running[-1], origin),
                         'last_positive_running_status': point(last_positive, origin) if last_positive else None,
                         'first_trailing_zero_running_status': point(trailing_zeros[0], origin) if trailing_zeros else None,
                         'trailing_sampled_zero_span_seconds': (running[-1]['now_ns'] - trailing_zeros[0]['now_ns']) / 1e9 if trailing_zeros else 0,
                         'kernel_stop_request_ns': stop_ns,
                         'kernel_stop_request_seconds_since_attach': (stop_ns - origin) / 1e9 if stop_ns else None,
                         'boundary_samples': boundaries,
                         'boundary_caveat': 'Last running status precedes kernel stop request; mapping invalidations may already have begun. Exact pre-teardown memory state is not logged.'},
        'dirty_sampling': dict(dirty),
        'write_handling': timing(last_progress),
        'handler_calls_minus_WP_generations': last_progress['write_calls'] - final['terminals']['write'],
        'final_status': final, 'final_progress': last_progress,
        'sample_offsets_from_final_status_us': {'progress': (last_progress['now_ns'] - final['now_ns']) / 1000,
                                              'dirty': (dirty['now_ns'] - final['now_ns']) / 1000},
        'stop_return': stops[0],
        'validation': {'counters_monotonic': True, 'generation_conservation': True,
                       'cohort_totals_match_sweep_counts': True, 'target_and_controller_exit_zero': True},
    }
    plot = {'status': [point(r, origin) for r in statuses],
            'dirty': [dict(r, seconds_since_attach=(r['now_ns'] - origin) / 1e9) for r in dirty_rows],
            'sweeps': [{'pass_id': r['pass_id'], 'start_seconds': (r['start_ns'] - origin) / 1e9,
                        'end_seconds': (r['end_ns'] - origin) / 1e9,
                        'complete': bool(r['complete']), 'error': r['error']} for r in cohorts]}
    return result, plot


def analyze_threads(rows, expected, origin, expected_exe=None):
    matching = [r for r in rows if len(r.get('threads', [])) == expected
                and (expected_exe is None or r.get('exe') == expected_exe)]
    require(matching, 'No expected thread count observed')
    require(all(r['thp_enabled'] == '0' for r in matching), 'THP was not disabled for target')
    def bound(r):
        return all(len(t['cpu_allowed']) == 1 for t in r['threads']) and len({t['cpu_allowed'][0] for t in r['threads']}) == expected
    first_bound = next((i for i, r in enumerate(matching) if bound(r)), None)
    require(first_bound is not None, 'Expected bound worker threads were not observed')
    startup_samples = first_bound
    matching = matching[first_bound:]
    require(all(bound(r) for r in matching), 'Worker affinity regressed after initialization')
    details = []
    for tid in sorted({t['tid'] for r in matching for t in r['threads']}):
        observations = [(r['monotonic_ns'], t) for r in matching for t in r['threads'] if t['tid'] == tid]
        start, end = observations[0], observations[-1]
        monotonic([t for _, t in observations], ('utime_ticks', 'stime_ticks'))
        details.append({'tid': tid, 'samples': len(observations),
                        'first_seconds_since_attach': (start[0] - origin) / 1e9,
                        'last_seconds_since_attach': (end[0] - origin) / 1e9,
                        'user_ticks_increase': end[1]['utime_ticks'] - start[1]['utime_ticks'],
                        'system_ticks_increase': end[1]['stime_ticks'] - start[1]['stime_ticks'],
                        'cpu_allowed_values': sorted({tuple(t['cpu_allowed']) for _, t in observations}),
                        'sampled_processors': sorted({t['processor'] for _, t in observations})})
    active = [r for r in details if r['user_ticks_increase'] + r['system_ticks_increase'] > 0]
    require(len(active) == expected, 'Expected every worker to accumulate CPU ticks')
    # An interval with every worker gaining CPU ticks substantiates activity,
    # without claiming each worker ran at the same instant.
    active_intervals = 0
    for a, b in zip(matching, matching[1:]):
        at, bt = {t['tid']: t for t in a['threads']}, {t['tid']: t for t in b['threads']}
        if set(at) == set(bt) and all(bt[k]['utime_ticks'] + bt[k]['stime_ticks'] >
                                      at[k]['utime_ticks'] + at[k]['stime_ticks'] for k in at):
            active_intervals += 1
    return {'requested': expected, 'total_samples': len(rows), 'bound_matching_samples': len(matching), 'startup_samples_before_binding': startup_samples,
            'thread_count_histogram': dict(sorted(collections.Counter(len(r.get('threads', [])) for r in rows).items())),
            'active_workers_with_positive_ticks': len(active),
            'sample_intervals_with_all_workers_gaining_ticks': active_intervals,
            'workers': details,
            'cpu_tick_note': 'Ticks are retained in native units; no assumed CLK_TCK conversion. Concurrent interval progress does not prove simultaneous execution at every instant.'}


def analyze_case(base, threads):
    phase = base / ('twitter-observe-t' + str(threads))
    run = phase / 'run'
    required = [run / name for name in ('monitor.jsonl', 'target.log', 'validation.json', 'thread-samples.jsonl', 'command.json')]
    required += [phase / 'summary.json', phase / 'phase-profile.json']
    require(all(p.exists() for p in required), 'Missing completed phase evidence: ' + str(phase))
    summary, validation = read_json(phase / 'summary.json'), read_json(run / 'validation.json')
    command, profile = read_json(run / 'command.json'), read_json(phase / 'phase-profile.json')
    require(summary['pass'] and validation['pass'], 'Phase validation failed')
    require(command['exit'] == 0 and not command.get('timed_out'), 'Command failed/timed out')
    require(command['environment']['OMP_NUM_THREADS'] == str(threads), 'Requested thread count mismatch')
    report, plot = analyze_monitor(run / 'monitor.jsonl')
    text = (run / 'target.log').read_text()
    trials = [float(s) for s in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', text)]
    expected = int(command['argv'][command['argv'].index('-n') + 1])
    require(len(trials) == expected == validation['trial_count'], 'Trial count mismatch')
    require(abs(sum(trials) - validation['total_trial_seconds']) < .001, 'Trial time mismatch')
    require(not re.search(r'Verification:', text), 'Verifier activity mixed into observation run')
    max_iterations = int(command['argv'][command['argv'].index('-i') + 1])
    tolerance = float(command['argv'][command['argv'].index('-t') + 1])
    require('-l' in command['argv'], 'PageRank iteration logging missing')
    iteration_groups, current_iterations = [], []
    for line in text.splitlines():
        step = re.match(r'^\s*(\d+)\s+([0-9.eE+-]+)\s*$', line)
        if step:
            current_iterations.append((int(step.group(1)), float(step.group(2))))
        elif 'Trial Time:' in line:
            require(current_iterations, 'Missing PageRank iteration log')
            require([i for i, _ in current_iterations] == list(range(len(current_iterations))),
                    'PageRank iterations are not contiguous')
            require(len(current_iterations) <= max_iterations, 'PageRank iteration cap exceeded')
            require(all(math.isfinite(value) for _, value in current_iterations), 'Non-finite PageRank change')
            # Stock GAPBS PrintStep rounds to five decimal places. These are
            # successive-iterate changes, not independent verifier residuals.
            require(current_iterations[-1][1] <= tolerance + 0.000005,
                    'Final logged iterate change exceeds tolerance plus rounding')
            iteration_groups.append({'iterations': len(current_iterations),
                                     'last_logged_change_rounded': current_iterations[-1][1]})
            current_iterations = []
    require(len(iteration_groups) == expected and not current_iterations,
            'PageRank iteration/trial groups differ')
    require(max_iterations == profile['max_iterations'] and tolerance == profile['tolerance'],
            'PageRank parameters differ from phase profile')
    target_elapsed = profile['target_total_elapsed_seconds']
    origin = report['time_origin']['attach_ns']
    report.update({'requested_threads': threads, 'profile': profile,
                   'pagerank': {'max_iterations': max_iterations, 'tolerance': tolerance,
                                'trials': iteration_groups,
                                'note': 'Logged changes are rounded successive-iterate L1 changes; independent verification runs provide push residuals.'},
                   'application': {'trials': len(trials), 'trial_compute_seconds': sum(trials),
                                   'trial_times_seconds': trials,
                                   'application_and_monitor_elapsed_seconds': command['elapsed_seconds'],
                                   'target_total_elapsed_seconds': target_elapsed,
                                   'target_elapsed_difference_seconds': command['elapsed_seconds'] - target_elapsed,
                                   'attach_to_final_seconds': (report['final_status']['now_ns'] - origin) / 1e9},
                   'thread_activity': analyze_threads(read_jsonl(run / 'thread-samples.jsonl'), threads, origin,
                                                      command['argv'][command['argv'].index('--') + 1]),
                   'host_after': summary.get('host_after'),
                   'source_files_sha256': {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in required}})
    require(report['dirty_sampling']['mode'] == 2, 'Expected dirty-qualified ARM policy')
    require(report['dirty_sampling']['quiet_ms'] == profile['quiet_ms'], 'Quiet interval mismatch')
    return report, plot


def render(reports, data, output, dirty_row=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator
    blue, orange, teal, gray = '#2563eb', '#d86612', '#0d9488', '#64748b'
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.titleweight': 'bold', 'svg.fonttype': 'none'})
    nrows = 3 if dirty_row else 2
    fig, axes = plt.subplots(nrows, 2, figsize=(12, 8.8 if dirty_row else 6.8), sharex=True)
    fig.subplots_adjust(left=.08, right=.91, top=.82, bottom=.17, hspace=.43, wspace=.38)
    fig.text(.08, .957, 'Twitter PageRank: dirty sampling before ARM', fontsize=20, weight='bold')
    fig.text(.08, .913, '10 s sweep period and 10 s clean qualification  |  Each column is a separate run', color=gray)
    fig.text(.08, .875, 'Shaded intervals: actual sweeps. Dashed vertical line: kernel stop request.', fontsize=9, color=gray)
    xmax = max(d['status'][-1]['seconds_since_attach'] for d in data.values())
    ymax = max(max(p['active_pages'] for p in d['status']) for d in data.values()) / 1e6
    armmax = max(r['counts']['ARM'] for r in reports.values()) / 1e6
    dirtymax = max(max(p[k] for k in ('dirty', 'clean')) for d in data.values() for p in d['dirty']) / 1e6
    wp_count = max(r['counts']['WP'] for r in reports.values())
    wp_divisor = 1e6 if wp_count >= 1000000 else 1000 if wp_count >= 1000 else 1
    wpmax = wp_count / wp_divisor
    wp_label = ('Cumulative WP (millions)' if wp_divisor == 1e6 else
                'Cumulative WP (thousands)' if wp_divisor == 1000 else 'Cumulative WP events')
    for col, key in enumerate(('1', '4')):
        r, d = reports[key], data[key]
        t = [p['seconds_since_attach'] for p in d['status']]
        a = axes[0, col]
        a.plot(t, [p['active_pages'] / 1e6 for p in d['status']], color=blue, lw=2)
        a.fill_between(t, [p['active_pages'] / 1e6 for p in d['status']], color=blue, alpha=.09)
        a.set_title('{} thread{} | {:.1f} s elapsed'.format(key, '' if key == '1' else 's', r['application']['application_and_monitor_elapsed_seconds']), loc='left')
        a.set_ylabel('Live ARM pages (millions)')
        a.set_ylim(0, max(.001, ymax * 1.15))
        peak = r['sampled_live']['peak']
        a.text(.03, .92, 'Sampled peak: {:,} pages'.format(peak['active_pages']), transform=a.transAxes, va='top', fontsize=9, color=blue)
        if r['sampled_live']['trailing_sampled_zero_span_seconds'] > r['profile']['period_ms'] / 1000:
            zero_seconds = r['sampled_live']['first_trailing_zero_running_status']['seconds_since_attach']
            a.annotate('Zero by {:.0f} s, before exit'.format(zero_seconds),
                       xy=(zero_seconds, 0), xytext=(.55, .28), textcoords='axes fraction',
                       color=gray, fontsize=9, arrowprops={'arrowstyle': '->', 'color': gray, 'lw': 1})
        a = axes[1, col]
        a.plot(t, [p['ARM'] / 1e6 for p in d['status']], color=blue, lw=2)
        a.set_ylabel('Cumulative ARM (millions)', color=blue)
        a.tick_params(axis='y', labelcolor=blue)
        a.set_ylim(0, max(.001, armmax * 1.18))
        a.set_title('ARM and WP events (separate scales)', loc='left', fontsize=10)
        twin = a.twinx()
        twin.spines['right'].set_visible(True)
        twin.plot(t, [p['WP'] / wp_divisor for p in d['status']], color=orange, lw=2)
        twin.set_ylim(0, max(1, wpmax * 1.18))
        twin.set_ylabel(wp_label, color=orange)
        if wp_divisor == 1:
            twin.yaxis.set_major_locator(MaxNLocator(integer=True))
        twin.tick_params(axis='y', labelcolor=orange)
        a.text(.03, .94, 'ARM {:,} | WP {:,}'.format(r['counts']['ARM'], r['counts']['WP']),
               transform=a.transAxes, va='top', fontsize=9)
        if dirty_row:
            a = axes[2, col]
            a.plot([p['seconds_since_attach'] for p in d['dirty']], [p['dirty'] / 1e6 for p in d['dirty']], color=orange, label='Dirty')
            a.plot([p['seconds_since_attach'] for p in d['dirty']], [p['clean'] / 1e6 for p in d['dirty']], color=teal, label='Clean')
            a.set_title('Repeated dirty-bit observations', loc='left', fontsize=10)
            a.set_ylabel('Observations (millions)')
            a.set_ylim(0, max(.001, dirtymax * 1.1))
            a.legend(frameon=False, fontsize=8)
        stop = r['sampled_live']['kernel_stop_request_seconds_since_attach']
        for a in axes[:, col]:
            for sweep in d['sweeps']:
                a.axvspan(sweep['start_seconds'], sweep['end_seconds'], color=gray, alpha=.07, zorder=0)
            if stop is not None:
                a.axvline(stop, color=gray, ls='--', lw=1)
            a.grid(axis='y', alpha=.2)
            a.set_xlim(0, xmax * 1.025)
            a.xaxis.set_major_locator(MaxNLocator(5))
        axes[-1, col].set_xlabel('Seconds since monitor attachment')
    fig.text(.08, .092, 'ARM counts protection cycles; WP counts write-triggered revocations. A page can contribute repeatedly.', fontsize=9, color=gray)
    fig.text(.08, .059, 'Monitoring includes graph loading, initialization and exit cleanup. Final samples confirm zero live ARM after cleanup.', fontsize=9, color=gray)
    fig.text(.08, .026, 'This comparison has no matched unmonitored baseline; it does not measure end-to-end overhead.', fontsize=9, color=gray)
    for ext in ('png', 'pdf', 'svg'):
        fig.savefig(output / ('arm-wp-timeline.' + ext), dpi=180, bbox_inches='tight')
    plt.close(fig)


def common_window(reports, data, requested_seconds=120):
    # Compare counters before either run begins cleanup. Status polls are not
    # synchronized, so preserve each sampled endpoint rather than interpolate
    # discrete events or claim an exact checkpoint at the requested boundary.
    endpoint = min([requested_seconds] + [
        r['sampled_live']['last_running_status']['seconds_since_attach']
        for r in reports.values()])
    samples = {}
    for key, points in data.items():
        running = [p for p in points['status']
                   if p['stop_reason'] == 'none' and p['seconds_since_attach'] <= endpoint]
        require(running, 'Common running window contains no status sample')
        samples[key] = running[-1]
    return {'requested_seconds_since_attach': requested_seconds,
            'common_boundary_seconds_since_attach': endpoint,
            'samples': samples,
            'note': 'Latest running status at or before the common boundary; actual sampled times differ. This is a monitoring prefix, not a completed PageRank result or matched overhead measurement.'}


def markdown(reports, common):
    lines = ['Dirty-qualified Twitter PageRank, one and four threads.', '']
    for key, report in reports.items():
        live = report['sampled_live']
        if live['trailing_sampled_zero_span_seconds'] > report['profile']['period_ms'] / 1000:
            lines += ['With {} thread{}, sampled live ARM had fallen to zero by {:.3f} s and remained zero at every status sample through {:.3f} s. Coverage was lost before completion, not only during final cleanup. The aggregate invalidation counters do not identify the cause.'.format(
                key, '' if key == '1' else 's',
                live['first_trailing_zero_running_status']['seconds_since_attach'],
                live['last_running_status']['seconds_since_attach']), '']
    lines += ['| Metric | 1 thread | 4 threads |', '|---|---:|---:|']
    entries = [('PageRank trials', lambda r: '{:,}'.format(r['application']['trials'])),
               ('PageRank compute (s)', lambda r: '{:.3f}'.format(r['application']['trial_compute_seconds'])),
               ('Application + monitor elapsed (s)', lambda r: '{:.3f}'.format(r['application']['application_and_monitor_elapsed_seconds'])),
               ('Target total elapsed / actual difference (s)', lambda r: '{:.1f} / {:+.3f}'.format(r['application']['target_total_elapsed_seconds'], r['application']['target_elapsed_difference_seconds'])),
               ('Iterations per PageRank trial', lambda r: ', '.join(str(p['iterations']) for p in r['pagerank']['trials'])),
               ('Requested convergence tolerance', lambda r: '{:g}'.format(r['pagerank']['tolerance'])),
               ('Sweeps started / completed / missed deadlines', lambda r: '{started} / {completed} / {missed_deadlines}'.format(**r['sweeps'])),
               ('ARM events (first + re-ARM)', lambda r: '{:,} ({:,} + {:,})'.format(r['counts']['ARM'], r['counts']['first_arms'], r['counts']['rearms'])),
               ('WP events', lambda r: '{:,}'.format(r['counts']['WP'])),
               ('Sampled peak live ARM pages', lambda r: '{:,}'.format(r['sampled_live']['peak']['active_pages'])),
               ('Last sampled running ARM pages', lambda r: '{:,}'.format(r['sampled_live']['last_running_status']['active_pages'])),
               ('Trailing sampled span with zero live ARM (s)', lambda r: '{:.3f}'.format(r['sampled_live']['trailing_sampled_zero_span_seconds'])),
               ('Kernel-accounted protected integral (GiB-s)', lambda r: '{:.3f}'.format(r['kernel_protection_integral']['gib_seconds_at_4k'])),
               ('Sampled protected integral (GiB-s)', lambda r: '{:.3f}'.format(r['sampled_protection_integral']['gib_seconds_at_4k'])),
               ('Dirty / clean observations', lambda r: '{:,} / {:,}'.format(r['dirty_sampling']['dirty'], r['dirty_sampling']['clean'])),
               ('Dirty resets / TLB flushes', lambda r: '{:,} / {:,}'.format(r['dirty_sampling']['resets'], r['dirty_sampling']['flushes'])),
               ('Dirty sampling elapsed sum (s)', lambda r: '{:.3f}'.format(r['dirty_sampling']['sample_ns'] / 1e9)),
               ('History payload before controller close (MiB)', lambda r: '{:.2f}'.format(r['dirty_sampling']['metadata_bytes'] / 2**20)),
               ('Write helper calls', lambda r: '{:,}'.format(r['write_handling']['calls'])),
               ('Write helper elapsed sum (ms)', lambda r: '{:.3f}'.format(r['write_handling']['elapsed_sum_ms'])),
               ('Write helper elapsed mean (us)', lambda r: '{:.3f}'.format(r['write_handling']['elapsed_mean_us']) if r['write_handling']['elapsed_mean_us'] is not None else 'n/a'),
               ('Workers with observed CPU tick growth', lambda r: str(r['thread_activity']['active_workers_with_positive_ticks']))]
    for label, get in entries:
        lines.append('| {} | {} | {} |'.format(label, get(reports['1']), get(reports['4'])))
    lines += ['', 'Common monitoring prefix (up to {:.3f} s since attachment):'.format(common['common_boundary_seconds_since_attach']), '',
              '| Metric | 1 thread | 4 threads |', '|---|---:|---:|']
    for label, field, fmt in [('Actual sampled endpoint (s)', 'seconds_since_attach', '{:.3f}'),
                              ('Cumulative ARM at sampled endpoint', 'ARM', '{:,}'),
                              ('Cumulative WP at sampled endpoint', 'WP', '{:,}'),
                              ('Cumulative re-ARM at sampled endpoint', 'rearms', '{:,}'),
                              ('Live ARM at sampled endpoint', 'active_pages', '{:,}')]:
        lines.append('| {} | {} | {} |'.format(label, *[
            fmt.format(common['samples'][key][field]) for key in ('1', '4')]))
    lines += ['', common['note'], '', 'The duration target covers application plus monitor elapsed time. Each run uses a whole number of converged PageRank trials, so the target is approximate. Logged iterate changes are rounded to five decimal places; they are distinct from the push residuals in separate correctness runs.', '', 'Sweeps, dirty observations and ARM counts can revisit the same page. Last sampled running status precedes the kernel stop request; it is not an exact pre-teardown checkpoint.', '',
              'Write helper timing includes waits and can overlap across threads. It is neither full architectural page-fault latency nor application overhead. Sampling helper time already includes reset and flush time; do not add these totals. The sampled protected integral uses trapezoidal interpolation of sampled live ARM counts, so it is an estimate rather than exact generation lifetime accounting.', '',
              'Observation includes graph loading and initialization. There is no matched unmonitored baseline, so this report does not estimate causal overhead or a causal one-to-four-thread speedup.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--dirty-row', action='store_true')
    args = parser.parse_args()
    output = args.output or args.base / 'analysis'
    output.mkdir(parents=True, exist_ok=True)
    reports, data = {}, {}
    for threads in (1, 4):
        reports[str(threads)], data[str(threads)] = analyze_case(args.base, threads)
    common = common_window(reports, data)
    record = {'runs': reports, 'common_monitoring_prefix': common,
              'definitions': {'ARM': 'first_arms + rearms; protection generations', 'WP': 'terminals.write; write-triggered revoked generations',
                              'live_ARM': 'sampled active protected pages', 'dirty_samples': 'repeated page observations, not unique pages',
                              'write_timing': 'elapsed time in CMD-P write preparation including waits; overlapping across threads; not full fault latency or overhead'},
              'limitations': ['No matched unmonitored baseline.', 'Thread-count runs use independently sized trial counts.', 'Whole converged trials make the total-duration target approximate.', 'Rounded successive-iterate changes are distinct from independent verifier residuals.',
                              'Monitoring includes loading, initialization and exit cleanup.', 'Status, progress and dirty counters are separately timestamped samples.',
                              'Sampled peak is a lower bound on the true instantaneous peak.', 'Exact start of target mm teardown is not logged.']}
    (output / 'analysis.json').write_text(json.dumps(record, indent=2) + '\n')
    (output / 'plot-data.json').write_text(json.dumps(data, indent=2) + '\n')
    (output / 'analysis.md').write_text(markdown(reports, common))
    render(reports, data, output, args.dirty_row)
    print(json.dumps({'pass': True, 'output': str(output), 'threads': list(reports)}, indent=2))


if __name__ == '__main__':
    main()
