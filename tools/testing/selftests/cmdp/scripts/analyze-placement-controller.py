#!/usr/bin/env python3
"""Measure controller scheduled time, a co-placement effect distinct from scanner work."""
import argparse
from collections import Counter
import gzip
import importlib.util
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('base',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
spec=importlib.util.spec_from_file_location('trace',str(a.base/'source/analyze-bfs-overhead-profile.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
results=[]
for receipt in sorted((a.base/'measured/P1').glob('*/summary.json')):
    run=receipt.parent;s=json.loads(receipt.read_text());command=json.loads((run/'command.json').read_text())
    pid=command['controller_pid'];q=json.loads((a.base/'quality'/('P1-'+run.name+'.json')).read_text())
    life=[r for r in q['lifecycle'] if r['pid']==pid]
    begin=next(r['time_ns']/1e9 for r in life if r['tid']==pid and r['event']=='fork')
    end=next(r['time_ns']/1e9 for r in life if r['tid']==pid and r['event']=='exit')
    tids={pid}|{r['tid'] for r in life};on={};cpu=Counter();segments=0
    with gzip.open(str(run/'perf-script.txt.gz'),'rt') as f:
        for line in f:
            if 'sched:sched_switch' not in line:continue
            header=m.HEADER.match(line)
            if not header:continue
            ts=float(header.group(4));core=int(header.group(3));sw=m.SWITCH.search(header.group(7))
            assert sw,line
            prev,nxt=int(sw.group(2)),int(sw.group(5))
            if prev in on:
                start,where=on.pop(prev);lo,hi=max(begin,start),min(end,ts)
                if hi>lo:
                    if where!=0:assert sw.group(1)!='cmdp_monitor',('Controller ran outside CPU 0',run,line)
                    cpu[where]+=hi-lo;segments+=1
            if nxt in tids:on[nxt]=(ts,core)
    for start,core in on.values():
        if end>max(begin,start):cpu[core]+=end-max(begin,start)
    results.append(dict(run=run.name,threads=s['threads'],mode=s['mode'],placement=s['placement'],
        controller_pid=pid,controller_tids=sorted(tids),scheduled_seconds=sum(cpu.values()),
        cpu0_seconds=cpu.get(0,0),launcher_outside_cpu0_seconds=sum(v for k,v in cpu.items() if k!=0),
        per_cpu_seconds=cpu,observed_segments=segments,window_seconds=end-begin))
assert len(results)==36
a.output.write_text(json.dumps(dict(passed=True,runs=results,
    scope='Controller main/measurement/reporter threads in native fork-exit window, including taskset startup before binding. Non-CPU0 launcher time is separate. Scheduled intervals include interrupt time. Separate from BFS and exact scanner work.'),indent=2)+'\n')
print('PASS: controller scheduling for 36 traces')
