#!/usr/bin/env python3
"""Cross-check decoded attribution against independently recorded CMD-P counters."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('contexts',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
sources=['unknown','numa','protection','mprotect','mremap','unmap','clear','test'];results=[]
for path in sorted(a.contexts.glob('P*.json')):
 d=json.loads(path.read_text());r=json.loads((Path(d['run'])/'summary.json').read_text())
 if d['profile_stage']=='P0':continue
 assert d['analysis_version']==7,path
 assert d['raw_record_quality']['pass_'],path
 assert not d['unclosed_scanner_items'],path
 assert not d['anomalies'],(path,d['anomalies'])
 if d['profile_stage']=='P1':
  gap=d['scanner_work_elapsed_s']-d['scanner_on_cpu_s']-sum(d['scanner_waits_s'].get(k,0) for k in ('runnable','blocked'))
  assert abs(gap)<.001,(path,'scanner schedule balance',gap)
 if d['profile_stage']=='P3':
  assert d['pmu']['min_running_percent']>=95,path
  assert not any('|flush on task switch|pages0' in k for k in d['tlb_page_histogram']),path
 if d['mode']=='A':assert not d['invalidations'],path
 else:
  for i,name in enumerate(sources):
   entries=[v for k,v in d['invalidations'].items() if k.endswith('-source'+str(i))]
   wanted=r['diagnostics']['invalidation_sources'][name]
   assert sum(x['calls'] for x in entries)==wanted['calls'],(path,name,'call count')
   assert sum(x['revoked'] for x in entries)==wanted['arm_revoked'],(path,name,'revoked count')
   assert sum(x['histories'] for x in entries)==wanted['histories_reset'],(path,name,'history count')
   assert abs(sum(x['elapsed_s'] for x in entries)-wanted['elapsed_ns']/1e9)<1e-6,(path,name,'elapsed time')
 results.append(dict(run=d['run'],passed=True,raw_samples=d['raw_record_quality']['record_counts'].get('9',0),scanner_items=d['scanner_work_items']))
a.output.write_text(json.dumps(dict(passed=True,runs=len(results),checks='No loss/throttle, complete scanner intervals, zero parser/scheduler anomalies, callback counts/revocations/histories/timers match controller; P3 counter running >=95% and no no-op task-switch flush records',results=results),indent=2)+'\n')
print('PASS: {} profiled runs agree with independent controller counters'.format(len(results)))
