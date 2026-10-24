#!/usr/bin/env python3
"""Record automatic perf rate ceilings separately from raw record throttling."""
import argparse,json,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args()
first=json.loads((a.root/'run/measured/P0/b01-bfs-t1-A/command.json').read_text())['launcher_begin_ns']/1e9
observations={}
for log in a.root.glob('run*/kernel-*.log'):
 for line in log.read_text().splitlines():
  m=re.search(r'\[\s*(\d+\.\d+)\] perf: interrupt took too long .*lowering kernel.perf_event_max_sample_rate to (\d+)',line)
  if m and float(m[1])>=first:observations[float(m[1])]=dict(at_monotonic_s=float(m[1]),ceiling_per_s=int(m[2]),kernel_message=line)
runs=[json.loads(f.read_text()) for f in (a.root/'analysis/final-contexts').glob('P2-*.json')]
fixtures=[]
for path in a.root.glob('run*/readiness/*/collectors.json'):
    records=json.loads(path.read_text())
    if records:
        fixtures.append((min(x['start_ns'] for x in records)/1e9,max(x['stopped_ns'] for x in records)/1e9,str(path.parent.relative_to(a.root))))
for event in observations.values():
 event['retained_run']=None
 for run in runs:
  lo=run['trace_clock']['window_begin_s'];hi=run['trace_clock']['window_end_s']
  if lo<=event['at_monotonic_s']<=hi:
   event['retained_run']=str(Path(run['run']).name);event['seconds_into_run']=event['at_monotonic_s']-lo
 event['phase']='retained hardware profile' if event['retained_run'] else 'outside retained P2'
 if not event['retained_run']:
  for lo,hi,name in fixtures:
   if lo<=event['at_monotonic_s']<=hi:event['phase']='readiness fixture';event['fixture']=name
final=json.loads((a.root/'run-v5/final-audit.json').read_text())
out=dict(initial_ceiling_per_s=63750,final_ceiling_per_s=final['perf_auto_ceiling_final'],requested_P2_hz=99,
    observations=[observations[k] for k in sorted(observations)],
    note='Automatic kernel perf overhead control; no manual sysctl change. Every observed ceiling exceeds the requested 99 Hz. Separate raw record audits show zero LOST/THROTTLE/UNTHROTTLE records in retained recordings. No claim that instrumentation is free; P0 provides headline timings.')
assert out['observations'] and min(x['ceiling_per_s'] for x in out['observations'])>99
(a.root/'analysis/perf-auto-ceiling.json').write_text(json.dumps(out,indent=2)+'\n')
print('Recorded',len(observations),'automatic perf ceiling changes; final',out['final_ceiling_per_s'])
