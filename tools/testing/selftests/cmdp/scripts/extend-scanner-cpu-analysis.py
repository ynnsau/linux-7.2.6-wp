#!/usr/bin/env python3
"""Preserve diagnostic context separately from headline paired runtime."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('base',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
def stats(values):return dict(values=values,median=statistics.median(values),min=min(values),max=max(values))
def save(name,value):(a.output/name).write_text(json.dumps(value,indent=2)+'\n')
rows=[];groups=defaultdict(list);windows=[]
for receipt in sorted((a.base/'measured').glob('*/*/summary.json')):
    s=json.loads(receipt.read_text());command=json.loads((receipt.parent/'command.json').read_text())
    windows.append((command['launcher_begin_ns']/1e9,command['launcher_end_ns']/1e9,str(receipt.parent.relative_to(a.base))))
    r=dict(run=str(receipt.parent.relative_to(a.base)),stage=s['profile_stage'],threads=s['threads'],mode=s['mode'],placement=s['placement'])
    for name,v in s['diagnostics'].get('invalidation_sources',{}).items():
        r[name+'_callback_s']=v['elapsed_ns']/1e9
        r[name+'_callback_calls']=v['calls']
        r[name+'_revoked_pages']=v['arm_revoked']
    if s['profile_stage']=='P1':
        d=json.loads((a.base/'contexts'/('P1-'+receipt.parent.name+'.json')).read_text())
        r['bfs_mmap_wait_s']=sum(v.get('blocked:mmap/rwsem',0) for v in d['target_scheduling'].values())
        r['bfs_vma_wait_s']=sum(v.get('blocked:VMA reader exclusion',0) for v in d['target_scheduling'].values())
        r['bfs_runnable_s']=sum(v.get('runnable',0) for v in d['target_scheduling'].values())
        r['callback_s_by_scope']={}
        for key,v in d['invalidations'].items():
            scope=key.rsplit('-source',1)[0]
            r['callback_s_by_scope'][scope]=r['callback_s_by_scope'].get(scope,0)+v['elapsed_s']
    rows.append(r);groups['{}-t{}-{}-{}'.format(r['stage'],r['threads'],r['mode'],r['placement'])].append(r)
cells={}
for key,values in groups.items():
    cells[key]={name:stats([r[name] for r in values]) for name,value in values[0].items()
                if isinstance(value,(int,float)) and name!='threads'}
save('diagnostic-summary.json',dict(rows=rows,cells=cells,
    notes=['Callback elapsed time includes waiting/preemption and sums thread contexts; it is not tree-only CPU time or additive wall time.',
           'Batch flush_ns measures synchronous flush_tlb_mm_range calls, excluding later TLB refills and invalidation-path flushes.',
           'write_ns measures cmdp_prepare_write revocation before address-lock accounting; it excludes the rest of Linux fault handling.']))
events={}
for log in (a.base/'kernel-initial.log',a.base/'kernel-new.log'):
    for line in log.read_text().splitlines():
        m=re.search(r'\[\s*(\d+\.\d+)\] perf: interrupt took too long .*lowering kernel.perf_event_max_sample_rate to (\d+)',line)
        if not m:continue
        ts=float(m[1]);events[ts]=dict(kernel_time_s=ts,ceiling_per_s=int(m[2]),message=line,
            approximate_run=next((name for lo,hi,name in windows if lo<=ts<=hi),None))
quality=[json.loads(p.read_text()) for p in (a.base/'quality').glob('P1-b*.json')]
assert len(quality)==36 and all(q['pass_'] for q in quality)
save('perf-auto-ceiling.json',dict(events=[events[t] for t in sorted(events)],
    raw_trace_loss=sum(q['lost_samples'] for q in quality),
    raw_trace_throttle=sum(q['throttle_records']+q['unthrottle_records'] for q in quality),
    note='Automatic kernel overhead control messages are distinct from LOST/THROTTLE trace records. Headline P0 timings have no added perf collection; P1 is analyzed separately.'))
print('PASS: diagnostic context and automatic perf ceilings recorded')
