#!/usr/bin/env python3
"""Render both successful runs and partial evidence without inventing final zeros."""
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
validation = json.loads((path / 'validation.json').read_text())
final = validation.get('final', {})
observed = final or validation.get('last_observed', {})
progress = validation.get('progress', {})
observation = validation.get('observation_end', {})
known = validation.get('final_state_known', bool(final))
scope = 'Final' if known else 'Last observed'
completed = validation.get('completed_sweeps')
active_boundary = observation.get('active')
request_active = progress.get('stop_request_active') if progress.get('stop_request_ns') else None


def value(item):
    return 'unknown' if item is None else str(item)


lines = [f"Validation: {'PASS' if validation['pass'] else 'FAIL'}",
         f"Final kernel state: {final.get('state')}" if known else 'Final kernel state: unknown (incomplete run)',
         f"Completed sweeps ({scope.lower()}): {value(completed)}",
         f"{scope} first arms: {value(observed.get('first_arms'))}; rearms: {value(observed.get('rearms'))}; active: {value(observed.get('active'))}",
         f"Active at controller observation end: {value(active_boundary)}",
         f"Active at kernel STOP request: {value(request_active)}",
         f"Active at drain start (legacy pre_stop_active): {value(final.get('pre_stop_active'))}",
         f"Final stop reason: {final.get('stop_reason', 'unknown')}",
         f"{scope} protected byte-ms: {value(observed.get('protected_byte_ms'))}"]
if progress:
    lines += [f"Last scanner phase: {progress.get('scan_phase', 'unknown')}; current mmap wait ns: {value(progress.get('scan_lock_wait_ns'))}",
              f"Range callbacks: {value(progress.get('range_calls'))}; in flight: {value(progress.get('range_inflight'))}; completed elapsed ns: {value(progress.get('range_ns'))}",
              f"Range lookup calls: {value(progress.get('lookup_calls'))}; nodes examined: {value(progress.get('lookup_nodes'))}; maximum index lock hold ns: {value(progress.get('index_lock_max_ns'))}",
              f"CMD-P write-handler calls: {value(progress.get('write_calls'))}; elapsed ns: {value(progress.get('write_ns'))}; maximum ns: {value(progress.get('write_max_ns'))}"]
stop_request = validation.get('stop_request', {})
stop_return = validation.get('stop_return', {})
if stop_request:
    lines += [f"Controller STOP requested at ns: {value(stop_request.get('now_ns'))}; returned at ns: {value(stop_return.get('now_ns'))}; elapsed ns: {value(stop_return.get('elapsed_ns'))}"]
lines += ['', f"Cohort scope: {validation.get('cohort_scope', 'final')}. Observations may have different timestamps.",
          '| Cohort | New | Rearmed | Retained during scan | Write-revoked | Other terminal | Observed active |',
          '|---|---:|---:|---:|---:|---:|---:|']
for cohort in validation.get('cohorts', []):
    lines.append('| ' + ' | '.join(str(cohort[key]) for key in ('pass_id', 'first_arms', 'rearms', 'already_armed', 'write_revoked', 'other_terminal', 'active')) + ' |')
lines += ['', f"{scope} reason counts: " + json.dumps(observed.get('terminals', {}), sort_keys=True),
          '', validation.get('coverage_note', ''),
          'Already-armed pages preserve their original arm epoch. Final cohort active counts are after cleanup; other terminal counts include cleanup. Observation-end cohorts are preserved separately in validation.json.',
          'Missing final status leaves the eventual state and cleanup outcome unknown; partial observations never make validation pass.',
          'CMD-P write-handler timing covers its instrumented handler work, not the complete architectural page-fault path or application runtime overhead.',
          'resident_pages counts resident pages inspected in supported VMA types; unsupported VMAs contribute skipped virtual extents, not a measured resident count. It is not an all-process resident denominator.',
          'The current ABI exposes cohort aggregates and address queries, not an exhaustive page-event stream. No complete per-page duration distribution is inferred.']
lines += ['ERROR: ' + error for error in validation.get('errors', [])]
(path / 'report.md').write_text('\n'.join(lines) + '\n')
print('\n'.join(lines))
