#!/usr/bin/env python3
"""Preserve weighted leaf/caller offsets for offline source-line annotation."""
import argparse,importlib.util,json
from collections import Counter
from pathlib import Path
s=importlib.util.spec_from_file_location('analysis',str(Path(__file__).with_name('analyze-bfs-overhead-profile.py')))
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
p=argparse.ArgumentParser();p.add_argument('contexts',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(exist_ok=True,parents=True)
hot={'ktime_get','_raw_spin_lock_irqsave','_raw_spin_unlock_irqrestore','_raw_spin_lock','pte_offset_map_lock','cmdp_monitor_get_page_cached','cmdp_arm_prepare','cmdp_find_range'}
for path in sorted(a.contexts.glob('P2-*.json')):
 target=a.output/path.name
 if target.exists():continue
 d=json.loads(path.read_text());assert d['sample_weight_unit']=='cycles'
 tids=set(d['target_tids']);active=set();counts=Counter()
 lo=d['trace_clock']['window_begin_s'];hi=d['trace_clock']['window_end_s']
 for e in m.events(Path(d['run'])/'perf-script.txt.gz'):
  if e['event']=='workqueue:workqueue_execute_start':active.add(e['tid'])
  elif e['event']=='workqueue:workqueue_execute_end':active.discard(e['tid'])
  elif e['event'].startswith('cycles/') and lo<=e['ts']<=hi and e['raw_frames']:
   scope='scanner' if e['tid'] in active else 'bfs-kernel' if e['tid'] in tids and e.get('kernel_frame') else None
   if not scope:continue
   name=e['frames'][0][0];raw=e['raw_frames']
   if name.startswith('cmdp_'):
    counts[(scope,'sample_ip',raw[0][1])]+=e['period']
   if name in hot and len(raw)>1 and raw[1][1].startswith('cmdp_'):
    counts[(scope,'caller_of:'+name,raw[1][1])]+=e['period']
 rows=[dict(scope=k[0],kind=k[1],symbol_offset=k[2],cycles=v) for k,v in counts.most_common()]
 target.write_text(json.dumps(dict(run=d['run'],weight_unit='cycles',note='Leaf IP and caller-return-address weights overlap; do not add them. Map callers at return address minus one.',rows=rows),indent=2)+'\n')
 print('Extracted',path.name,flush=True)
