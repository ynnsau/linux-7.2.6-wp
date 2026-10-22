#!/usr/bin/env python3
"""Validate controller JSONL without discarding evidence from incomplete runs."""
import argparse
import json
import pathlib
import re
import sys

STATUS_EVENTS = {'status', 'stopping_status', 'observation_end', 'final'}
COHORT_EVENTS = {'sweep', 'cohort_snapshot', 'cohort_observation_end', 'cohort_final'}


def validate(path, minimum=1, compute_seconds=0, require_admissions=True):
    errors = []
    records = []
    try:
        raw = (path / 'monitor.jsonl').read_text()
    except OSError as exc:
        raw = ''
        errors.append(str(exc))
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError('record is not an object')
            records.append(record)
        except ValueError as exc:
            errors.append(f'Invalid JSON record at line {line_no}: {exc}')
    finals = [x for x in records if x.get('event') == 'final']
    exits = [x for x in records if x.get('event') == 'controller_exit']
    statuses = [x for x in records if x.get('event') in STATUS_EVENTS]
    observations = [x for x in records if x.get('event') == 'observation_end']
    progress = [x for x in records if x.get('event') == 'progress']
    stop_requests = [x for x in records if x.get('event') == 'stop_request']
    stop_returns = [x for x in records if x.get('event') == 'stop_return']
    final = finals[-1] if finals else {}
    observed = statuses[-1] if statuses else {}
    # Missing final status means unknown final state, never zero completed work.
    completed = final.get('passes_completed') if final else observed.get('passes_completed')
    if completed is None:
        completed_ids = {x['pass_id'] for x in records
                         if x.get('event') in COHORT_EVENTS and x.get('complete')}
        completed = len(completed_ids) if completed_ids else None
    if len(finals) != 1:
        errors.append('Expected exactly one final kernel status; final state is unknown or ambiguous')
    if len(exits) != 1 or exits[0].get('exit_code') != 0:
        errors.append('Controller did not exit successfully')
    for record in records:
        if record.get('event') == 'target_exit' and record.get('exit_code') != 0:
            errors.append('Target exited unsuccessfully')
        if record.get('event') == 'cohort_missing':
            errors.append('A requested cohort was not available')
        if record.get('event') == 'stop_return' and record.get('errno'):
            errors.append(f'STOP failed with errno {record["errno"]}')
    if completed is None or completed < minimum:
        errors.append(f'Fewer than {minimum} observed completed sweeps')
    if final:
        if final.get('state') != 4:
            errors.append('Final monitor state is not STOPPED')
        for key in ('active', 'resource_failures', 'reports_dropped'):
            if final.get(key, 1):
                errors.append(f'Final {key} is nonzero or absent')
        if final.get('stop_reason') not in ('control_stop', 'target_exit'):
            errors.append('Unexpected stop reason: ' + str(final.get('stop_reason')))
        if final.get('first_arms', 0) + final.get('rearms', 0) != final.get('active', 0) + sum(final.get('terminals', {}).values()):
            errors.append('Session generation counts do not balance')
        if final.get('flags', 0) & 1 and final.get('first_arms', 0) + final.get('rearms', 0):
            errors.append('Inventory-only session unexpectedly armed pages')
    final_cohorts = {x['pass_id']: x for x in records if x.get('event') == 'cohort_final'}
    observed_cohorts = {x['pass_id']: x for x in records if x.get('event') in COHORT_EVENTS}
    observation_cohorts = {x['pass_id']: x for x in records if x.get('event') == 'cohort_observation_end'}
    cohorts = final_cohorts if final else observed_cohorts
    for ident, cohort in cohorts.items():
        if cohort['first_arms'] + cohort['rearms'] != cohort['active'] + cohort['write_revoked'] + cohort['other_terminal']:
            errors.append(f'Cohort {ident} transition counts do not balance')
        if cohort.get('active_first', -1) + cohort.get('active_rearm', -1) != cohort['active']:
            errors.append(f'Cohort {ident} first/rearm survivor counts do not balance')
        # Linux ABI errno values, including when reporting on macOS.
        normal_interruption = (not cohort.get('complete') and cohort.get('error') in (-108, -3)
                               and final.get('stop_reason') in ('control_stop', 'target_exit'))
        if cohort.get('error') and not normal_interruption:
            errors.append(f'Cohort {ident} has error {cohort["error"]}')
        # An in-flight cohort legitimately has no end timestamp yet.
        if cohort.get('end_ns') and cohort['end_ns'] < cohort.get('start_ns', 0):
            errors.append(f'Cohort {ident} clock order invalid')
    if final and sum(c['first_arms'] + c['rearms'] for c in final_cohorts.values()) != final.get('first_arms', 0) + final.get('rearms', 0):
        errors.append('Retained cohorts do not account for all session generations')
    dirty_records = sorted((x for x in records if x.get('event') == 'dirty_status'),
                           key=lambda x: x.get('now_ns', 0))
    dirty_final = dirty_records[-1] if dirty_records else {}
    dirty_mode = dirty_final.get('mode', 0)
    dirty_cumulative = ('passes_started', 'passes_completed', 'samples', 'dirty', 'clean',
                        'primed', 'unknown', 'qualified', 'late_dirty', 'resets', 'flushes',
                        'reset_ns', 'flush_ns', 'sample_ns', 'invalidations', 'wp_resets',
                        'resource_skips', 'metadata_bytes')
    previous_dirty = None
    for record in dirty_records:
        if record.get('mode') not in (0, 1, 2):
            errors.append('Unknown dirty sampling mode')
        if record.get('mode') != dirty_mode:
            errors.append('Dirty policy changed during a reported session')
        if record.get('samples', 0) != record.get('dirty', 0) + record.get('clean', 0):
            errors.append('Dirty samples do not equal dirty plus clean observations')
        if not (record.get('primed', 0) <= record.get('resets', 0) <= record.get('samples', 0)):
            errors.append('Dirty prime/reset/sample counts are inconsistent')
        if record.get('resets', 0) != record.get('flushes', 0):
            errors.append('A completed dirty reset lacks its synchronous flush')
        if record.get('passes_completed', 0) > record.get('passes_started', 0):
            errors.append('Dirty completed passes exceed issued passes')
        if record.get('reset_ns', 0) + record.get('flush_ns', 0) > record.get('sample_ns', 0):
            errors.append('Dirty sub-operation times exceed total sampling time')
        if dirty_mode and record.get('quiet_ms', 0) < 10:
            errors.append('Invalid reported quiet threshold')
        if previous_dirty:
            for key in dirty_cumulative:
                if record.get(key, 0) < previous_dirty.get(key, 0):
                    errors.append(f'Dirty cumulative {key} decreased')
        previous_dirty = record
    if dirty_mode:
        if not dirty_final.get('samples'):
            errors.append('Dirty policy performed no valid samples')
        if final and dirty_final.get('valid_observations'):
            errors.append('Stopped dirty session still reports valid observations')
        if dirty_mode == 1 and final and (final.get('first_arms', 0) + final.get('rearms', 0)
                                         or final.get('terminals', {}).get('write', 0)):
            errors.append('Sampling-only policy unexpectedly armed or write-revoked pages')
        if dirty_mode == 2 and final:
            admissions = final.get('first_arms', 0) + final.get('rearms', 0)
            if require_admissions and not admissions:
                errors.append('Qualification run admitted no pages; admission test is vacuous')
            if admissions > dirty_final.get('qualified', 0):
                errors.append('Admissions exceed qualified observation epochs')
    diagnostic_records = sorted((x for x in records if x.get('event') == 'diagnostics'),
                                key=lambda x: x.get('now_ns', 0))
    previous_diagnostic = None
    for record in diagnostic_records:
        rejected = record.get('rejected', {})
        sources = record.get('invalidation_sources', {})
        if not rejected or sum(rejected.values()) != record.get('unknown'):
            errors.append('Exclusive sampling rejection reasons do not sum to unknown')
        cumulative = {'unknown': record.get('unknown', 0),
                      'trace_records': record.get('trace_records', 0),
                      'trace_records_dropped': record.get('trace_records_dropped', 0)}
        cumulative.update(('rejected.' + key, value) for key, value in rejected.items())
        for name, source in sources.items():
            cumulative.update(('source.' + name + '.' + key, value) for key, value in source.items())
            if source.get('max_ns', 0) > source.get('elapsed_ns', 0):
                errors.append('Invalidation maximum exceeds total duration: ' + name)
        if previous_diagnostic:
            for key, value in cumulative.items():
                if value < previous_diagnostic.get(key, 0):
                    errors.append('Diagnostic cumulative counter decreased: ' + key)
        previous_diagnostic = cumulative
    diagnostic_final = diagnostic_records[-1] if diagnostic_records else {}
    if diagnostic_final and final:
        sources = diagnostic_final.get('invalidation_sources', {})
        if sum(x.get('arm_revoked', 0) for x in sources.values()) != final.get('terminals', {}).get('invalidate', 0):
            errors.append('Final invalidation sources do not account for every invalidated ARM generation')
        if progress and sum(x.get('calls', 0) for x in sources.values()) != progress[-1].get('range_calls', 0):
            errors.append('Final invalidation source calls do not match completed range callbacks')
        if dirty_final and diagnostic_final.get('unknown') != dirty_final.get('unknown'):
            errors.append('Final diagnostic unknown differs from stopped dirty sampling total')
    log = (path / 'target.log').read_text(errors='replace') if (path / 'target.log').exists() else ''
    trials = [float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)', log)]
    if compute_seconds and sum(trials) < compute_seconds:
        errors.append(f'BFS measured compute duration {sum(trials):.3f}s is below {compute_seconds}s')
    return {
        'pass': not errors, 'errors': errors, 'completed_sweeps': completed,
        'requirements': {'minimum_completed_sweeps': minimum, 'require_admissions': require_admissions},
        'completed_sweeps_source': ('final_status' if final else
                                   'last_status' if observed.get('passes_completed') is not None else
                                   'observed_complete_cohorts' if completed is not None else 'unknown'),
        'final_state_known': len(finals) == 1, 'last_observed': observed,
        'observation_end': observations[-1] if observations else {},
        'progress': progress[-1] if progress else {},
        'dirty': dirty_final, 'diagnostics': diagnostic_final,
        'stop_request': stop_requests[-1] if stop_requests else {},
        'stop_return': stop_returns[-1] if stop_returns else {},
        'trial_count': len(trials), 'total_trial_seconds': sum(trials), 'final': final,
        'cohort_scope': 'final' if final else 'last_observed', 'cohorts': list(cohorts.values()),
        'observation_cohorts': list(observation_cohorts.values()),
        'coverage_note': 'Skipped mapping/page categories are reported; this is not a claim to support every writable mapping.',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=pathlib.Path)
    parser.add_argument('--min-sweeps', type=int, default=1)
    parser.add_argument('--min-compute-seconds', type=float, default=0)
    parser.add_argument('--allow-zero-admissions', action='store_true',
                        help='For correctness/timing pilots only; still check all safety and accounting invariants')
    args = parser.parse_args()
    result = validate(args.run, args.min_sweeps, args.min_compute_seconds,
                      require_admissions=not args.allow_zero_admissions)
    (args.run / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('pass', 'errors', 'completed_sweeps', 'final_state_known')}, indent=2))
    sys.exit(0 if result['pass'] else 1)
