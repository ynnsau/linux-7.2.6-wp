#!/usr/bin/env python3
"""Explain inlined CMD-P leaf samples with the matching kernel's DWARF."""
import argparse,json,re,statistics
from collections import Counter
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args()
root=a.root;inspection=root/'run-v5/readiness/source-inspection'
mapped=json.loads((inspection/'mapped-callsites.json').read_text())
lookup={(x['kind'],x['symbol_offset']):x for x in mapped}
results={}
for t in (1,4):
 for mode in 'BC':
  for scope in ('scanner','bfs-kernel'):
   names=Counter();lines=Counter();denominator=0;nr=0
   for path in sorted((root/'analysis/callsites').glob('P2-*t{}-{}.json'.format(t,mode))):
    data=json.loads(path.read_text());d=json.loads((root/'analysis/final-contexts'/path.name).read_text())
    denominator+=d['sampled_context_cycles'].get(scope,0);nr+=1
    for row in data['rows']:
     if row['scope']!=scope:continue
     match=lookup[row['kind'],row['symbol_offset']]
     chain=match['source_inline_chain']
     frames=list(zip(chain[::2],chain[1::2]))
     if row['kind']=='sample_ip':
      function=next((name for name,source in frames if name.startswith('cmdp_')),'unknown')
      names[function]+=row['cycles']
     if row['kind'].startswith('caller_of:'):
      site=next(((name,source) for name,source in frames if name.startswith('cmdp_')),('unknown','unknown'))
      lines[row['kind'],site[0],site[1]]+=row['cycles']
   results['t{}-{}-{}'.format(t,mode,scope)]=dict(n=nr,context_cycles=denominator,
     inlined_cmdp_leaf_shares=[dict(function=k,cycles=v,context_percent=100*v/denominator) for k,v in names.most_common()],
     hot_leaf_call_sites=[dict(kind=k[0],function=k[1],source=k[2],cycles=v,context_percent=100*v/denominator) for k,v in lines.most_common()])
memory=[]
for receipt in sorted((root/'run/measured/P0').glob('*-C/summary.json')):
 s=json.loads(receipt.read_text());records=[json.loads(x) for x in (receipt.parent/'monitor.jsonl').read_text().splitlines()]
 active=max(r['active'] for r in records if r['event']=='status')
 history=max(r['history_allocated_bytes'] for r in records if r['event']=='batch_status')
 memory.append(dict(run=str(receipt.parent),threads=s['threads'],peak_observed_active=active,
     estimated_active_entry_slots_gib=active*1024/2**30,max_reported_history_gib=history/2**30,
     entry_slot_plus_history_estimate_gib=(active*1024+history)/2**30,
     mean_active_entry_slots_gib=s['final']['protected_byte_ms']/1000/s['elapsed_seconds']/4096*1024/2**30))
output=dict(note='Cycle shares refer to leaf samples in the named context. Inline and caller views overlap; never add them. Caller PCs adjusted by minus one. Hardware sampling still has skid; source-line attribution is approximate.',
    functions=results,memory=memory,
    memory_note='Ownership structure 960B; observed kmalloc-1k object size 1024B. Estimates use active pages at 1s polls; exclude pending/retired entries, XArray nodes, cohort data, slab fragmentation and other allocator overhead. RSS excludes kernel allocations. Sum uses separate observed maxima; not a measured simultaneous total.')
(root/'analysis/source-attribution.json').write_text(json.dumps(output,indent=2)+'\n')
for key in ('t1-B-scanner','t4-B-scanner','t1-C-scanner','t4-C-scanner'):
 print(key,[(r['function'],round(r['context_percent'],2)) for r in results[key]['inlined_cmdp_leaf_shares'][:6]])
for t in (1,4):
 rows=[x for x in memory if x['threads']==t]
 print('Memory t'+str(t),{k:statistics.median(x[k] for x in rows) for k in ('estimated_active_entry_slots_gib','max_reported_history_gib','entry_slot_plus_history_estimate_gib','mean_active_entry_slots_gib')})
