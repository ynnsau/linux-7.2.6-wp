#!/usr/bin/env python3
"""Validate controller JSONL. Kernel counters remain the authoritative state."""
import argparse,json,pathlib,re,sys

def validate(path, minimum=1, compute_seconds=0):
    errors=[]
    try:
        records=[json.loads(x) for x in (path/'monitor.jsonl').read_text().splitlines() if x.strip()]
    except (OSError,ValueError) as e:
        return {'pass':False,'errors':[str(e)]}
    finals=[x for x in records if x.get('event')=='final']
    exits=[x for x in records if x.get('event')=='controller_exit']
    if len(finals)!=1: errors.append('Expected exactly one final kernel status')
    if len(exits)!=1 or exits[0].get('exit_code')!=0: errors.append('Controller did not exit successfully')
    for x in records:
        if x.get('event')=='target_exit' and x.get('exit_code')!=0: errors.append('Target exited unsuccessfully')
        if x.get('event')=='cohort_missing': errors.append('A requested cohort was not available')
    final=finals[-1] if finals else {}
    for key in ('active','resource_failures','reports_dropped'):
        if final.get(key,1): errors.append(f'Final {key} is nonzero or absent')
    if final.get('passes_completed',0)<minimum: errors.append(f'Fewer than {minimum} completed sweeps')
    if final.get('stop_reason') not in ('control_stop','target_exit'):
        errors.append('Unexpected stop reason: '+str(final.get('stop_reason')))
    if final.get('first_arms',0)+final.get('rearms',0) != final.get('active',0)+sum(final.get('terminals',{}).values()):
        errors.append('Session generation counts do not balance')
    cohorts={x['pass_id']:x for x in records if x.get('event')=='cohort_final'}
    for ident,c in cohorts.items():
        if c['first_arms']+c['rearms'] != c['active']+c['write_revoked']+c['other_terminal']:
            errors.append(f'Cohort {ident} transition counts do not balance')
        if c.get('active_first',-1)+c.get('active_rearm',-1) != c['active']:
            errors.append(f'Cohort {ident} first/rearm survivor counts do not balance')
        # These are Linux ABI errno values, including when reporting on macOS.
        normal_interruption=(not c.get('complete') and c.get('error') in (-108,-3)
                             and final.get('stop_reason') in ('control_stop','target_exit'))
        if c.get('error') and not normal_interruption: errors.append(f'Cohort {ident} has error {c["error"]}')
        if c.get('end_ns',0)<c.get('start_ns',0): errors.append(f'Cohort {ident} clock order invalid')
    if sum(c['first_arms']+c['rearms'] for c in cohorts.values()) != final.get('first_arms',0)+final.get('rearms',0):
        errors.append('Retained cohorts do not account for all session generations')
    log=(path/'target.log').read_text(errors='replace') if (path/'target.log').exists() else ''
    trials=[float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)',log)]
    if compute_seconds and sum(trials)<compute_seconds:
        errors.append(f'BFS measured compute duration {sum(trials):.3f}s is below {compute_seconds}s')
    return {'pass':not errors,'errors':errors,'completed_sweeps':final.get('passes_completed',0),
            'trial_count':len(trials),'total_trial_seconds':sum(trials),'final':final,'cohorts':list(cohorts.values()),
            'coverage_note':'Skipped mapping/page categories are reported; this is not a claim to support every writable mapping.'}

if __name__=='__main__':
    a=argparse.ArgumentParser(); a.add_argument('run',type=pathlib.Path); a.add_argument('--min-sweeps',type=int,default=1); a.add_argument('--min-compute-seconds',type=float,default=0)
    args=a.parse_args(); result=validate(args.run,args.min_sweeps,args.min_compute_seconds)
    (args.run/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'pass':result['pass'],'errors':result['errors'],'completed_sweeps':result.get('completed_sweeps',0)},indent=2))
    sys.exit(0 if result['pass'] else 1)
