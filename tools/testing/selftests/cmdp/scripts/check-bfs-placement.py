#!/usr/bin/env python3
"""Short native launcher/collector fixtures; no kernel or host-policy change."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time

scripts=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('placement',str(scripts/'run-bfs-placement.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--label',default='v2');a=p.parse_args()
c=m.profile.Campaign.__new__(m.profile.Campaign)
c.profile_stage='P1'
c.scanner='0x'+next(x.split()[0] for x in Path('/proc/kallsyms').read_text().splitlines() if x.endswith(' cmdp_monitor_work'))
before=m.matched.old.snapshot()
kernel_before=subprocess.check_output(['dmesg'],universal_newlines=True).splitlines()
results=[]
for cpus in ([0],[1],[0,1,2,3],[1,2,3,4]):
    path=a.base/'readiness'/(a.label+'-cpus-'+','.join(map(str,cpus)))
    path.mkdir(parents=True)
    try:
        c.start_collectors(path)
        argv=['taskset','-c','0',str(a.base/'tools/cmdp_monitor'),'--target-cpus',','.join(map(str,cpus)),
              '--dirty-qualify','--quiet-ms','10','--period-ms','100','--poll-ms','1000',
              '--max-pages','262144','--max-history','524288','--dirty-batch-pages','512','--wp-batch-pages','512',
              '--',str(a.base/'tools/bin/cmdp_fixture'),'--mode','hotcold','--seconds','8']
        with (path/'monitor.jsonl').open('w') as out,(path/'target.log').open('w') as err:
            start=time.monotonic();child=subprocess.Popen(argv,stdout=out,stderr=err)
            deadline=time.monotonic()+5
            while True:
                lines=(path/'monitor.jsonl').read_text().splitlines()
                if lines: break
                assert time.monotonic()<deadline
                time.sleep(.01)
            record=json.loads(lines[0]);assert record['event']=='controller'
            assert os.sched_getaffinity(child.pid)=={0}
            assert os.sched_getaffinity(record['pid'])==set(cpus)
            assert child.wait(timeout=20)==0
            end=time.monotonic()
    finally:c.stop_collectors(path)
    with (path/'decoded.txt').open('w') as out,(path/'decode.log').open('w') as err:
        subprocess.run([m.profile.PERF,'script','--ns','-i',str(path/'perf.data'),'-k',m.profile.VMLINUX],stdout=out,stderr=err,check=True)
    active={};starts=ends=0;work_cpus=set()
    for e in m.analysis.events(path/'decoded.txt'):
        if e['event']=='workqueue:workqueue_execute_start':
            assert e['tid'] not in active;active[e['tid']]=e['ts'];starts+=1;work_cpus.add(e['cpu'])
        elif e['event']=='workqueue:workqueue_execute_end':
            assert e['tid'] in active;del active[e['tid']];ends+=1;work_cpus.add(e['cpu'])
    assert starts==ends and starts>0 and not active
    q=m.audit.audit(path/'perf.data');assert q['pass_'] and not q['unthrottle_records']
    lifecycle=[r for r in q['lifecycle'] if r['pid']==r['tid']==child.pid]
    first=next(r['time_ns']/1e9 for r in lifecycle if r['event']=='fork')
    last=next(r['time_ns']/1e9 for r in lifecycle if r['event']=='exit')
    assert abs((end-start)-(last-first))<.1
    result=dict(passed=True,target_cpus=cpus,controller_cpus=[0],scanner_work_cpus=sorted(work_cpus),
                scanner_only_cpu0=work_cpus=={0},work_starts=starts,work_ends=ends,argv=argv,quality=q)
    m.matched.save(path/'validation.json',result);results.append(result)
    print('PASS collector/launcher fixture',cpus,starts,'complete work items; scanner CPUs',sorted(work_cpus),flush=True)
after=m.matched.old.snapshot()
kernel_after=subprocess.check_output(['dmesg'],universal_newlines=True).splitlines()
assert kernel_after[:len(kernel_before)]==kernel_before
delta=kernel_after[len(kernel_before):]
(a.base/'readiness/kernel-new.log').write_text('\n'.join(delta)+'\n')
assert not any(re.search(r'BUG:|WARNING:|Oops:|KASAN:|Kernel panic|general protection fault|lockup|rcu.*stall',x,re.I) for x in delta),delta
for key in ('kernel','boot_id','numa_balancing','taint','settings','governors'):assert before[key]==after[key],key
assert all(after['stats'][key]==0 for key in ('entries','managed','isolated'))
assert after['stats']['arms']==after['stats']['publishes']==after['stats']['revokes']
m.matched.save(a.base/'readiness/validation.json',dict(passed=True,before=before,after=after,fixtures=results))
