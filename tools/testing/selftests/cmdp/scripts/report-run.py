#!/usr/bin/env python3
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); v=json.loads((p/'validation.json').read_text()); f=v.get('final',{})
lines=[f"Validation: {'PASS' if v['pass'] else 'FAIL'}",f"Completed sweeps: {f.get('passes_completed',0)}",
       f"First arms: {f.get('first_arms',0)}; rearms: {f.get('rearms',0)}; active at stop: {f.get('pre_stop_active',0)}",
       f"Stop reason: {f.get('stop_reason','unknown')}",f"Protected byte-ms: {f.get('protected_byte_ms',0)}",
       '', '| Cohort | New | Rearmed | Retained during scan | Write-revoked | Other terminal | Final active |',
       '|---|---:|---:|---:|---:|---:|---:|']
for c in v.get('cohorts',[]):
    lines.append('| '+' | '.join(str(c[k]) for k in ('pass_id','first_arms','rearms','already_armed','write_revoked','other_terminal','active'))+' |')
lines+=['','Reason counts: '+json.dumps(f.get('terminals',{}),sort_keys=True),'',v.get('coverage_note',''),
        'Already-armed pages preserve their original arm epoch. Final cohort active counts are after cleanup; other terminal counts include cleanup.',
        'resident_pages counts resident pages inspected in supported VMA types; unsupported VMAs contribute skipped virtual extents, not a measured resident count. It is not an all-process resident denominator.',
        'The current ABI exposes cohort aggregates and address queries, not an exhaustive page-event stream. No complete per-page duration distribution is inferred.']
lines += ['ERROR: '+x for x in v.get('errors',[])]
(p/'report.md').write_text('\n'.join(lines)+'\n'); print('\n'.join(lines))
