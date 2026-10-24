#!/usr/bin/env python3
"""Keep native controller counts separate from sampled CPU/trace evidence."""
import argparse,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('base',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
rows=[]
for receipt in sorted((a.base/'measured/P0').glob('*/summary.json')):
    s=json.loads(receipt.read_text())
    log=[json.loads(x) for x in (receipt.parent/'monitor.jsonl').read_text().splitlines()]
    batches=[x for x in log if x['event']=='batch_status']
    row=dict(run=str(receipt.parent),threads=s['threads'],mode=s['mode'],elapsed_s=s['elapsed_seconds'])
    row.update({key:s['target_rusage'][key] for key in ('user_us','system_us','minor_faults','major_faults','voluntary_switches','involuntary_switches')})
    if batches:
        batch=batches[-1]
        row.update({k:s['dirty'][k] for k in ('samples','resets','sample_ns','flush_ns','reset_ns')})
        row.update({'batch_'+k:batch[k] for k in ('flush_calls','flush_ns','shared_flush_calls','wp_only_flush_calls','dirty_only_flush_calls','wp_pages','dirty_pages','region_full_skips','region_skip_pages','history_allocated_bytes','history_payload_bytes','pmd_walks','pmd_cache_hits')})
        row['peak_observed_active']=max(x.get('active',0) for x in log if x['event']=='status')
        row['completed_sweeps']=s['completed_sweeps']
        row['wp_calls']=s['progress']['write_calls'];row['wp_ns']=s['progress']['write_ns']
        row['range_ns']=s['progress']['range_ns']
        for name,c in s['diagnostics']['invalidation_sources'].items():
            for key in ('calls','arm_revoked','elapsed_ns'):
                row[name+'_'+key]=c[key]
    rows.append(row)
cells={}
for t in (1,4):
    for mode in 'BC':
        group=[r for r in rows if (r['threads'],r['mode'])==(t,mode)]
        cells['t{}-{}'.format(t,mode)]={k:dict(values=[r[k] for r in group],median=statistics.median(r[k] for r in group)) for k,v in group[0].items() if isinstance(v,(int,float)) and k!='threads'}
a.output.write_text(json.dumps(dict(note='P0 native counters; timers overlap and are elapsed, not an additive CPU-time decomposition. Peak active is observed at 1s polls. History bytes exclude ownership entries, XArrays and allocator overhead.',rows=rows,cells=cells),indent=2)+'\n')
print('Summarized {} native runs'.format(len(rows)))
