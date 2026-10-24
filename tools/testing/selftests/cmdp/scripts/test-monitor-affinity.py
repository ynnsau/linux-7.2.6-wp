#!/usr/bin/env python3
"""Check child-only affinity before exec, with no CMD-P device access."""
import argparse
import json
import os
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('monitor', type=Path)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert set(range(5)) <= os.sched_getaffinity(0)
probe = ('import json,os,time; print(json.dumps(dict(child=sorted(os.sched_getaffinity(0)), '
         'parent=sorted(os.sched_getaffinity(os.getppid())))),flush=True); time.sleep(.05)')
results = []
for cpus in (None, [0], [1], [0,1,2,3], [1,2,3,4]):
    argv = ['taskset','-c','0',str(a.monitor),'--monitor-off']
    if cpus is not None:
        argv += ['--target-cpus', ','.join(map(str,cpus))]
    r = subprocess.run(argv+['--','/usr/bin/python3','-c',probe],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=10)
    assert r.returncode == 0, (argv,r.stdout,r.stderr)
    info = json.loads(r.stderr)
    assert info == dict(child=cpus or [0],parent=[0]), info
    records = [json.loads(x) for x in r.stdout.splitlines()]
    assert [x['event'] for x in records] == ['controller','target_exit','controller_exit']
    results.append(dict(requested=cpus,observed=info,passed=True))
for text in ('', '1,', ',1', '1,,2', '-1', '1-4', '1 2', '1024', '9999999999999999999999'):
    r = subprocess.run([str(a.monitor),'--monitor-off','--target-cpus',text,'--','/bin/true'],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    assert r.returncode == 2 and not r.stdout, (text,r.returncode)
    results.append(dict(rejected=text,passed=True))
for options in (['--target-cpus','1','--target-cpus','2','--monitor-off','--','/bin/true'],
                ['--target-cpus','1','--pid',str(os.getpid())]):
    r = subprocess.run([str(a.monitor)]+options,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    assert r.returncode == 2 and not r.stdout
    results.append(dict(rejected=options,passed=True))
# The kernel may silently intersect with online/cpuset CPUs: require exactness.
r = subprocess.run([str(a.monitor),'--monitor-off','--target-cpus','0,1023','--','/bin/true'],
    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
assert r.returncode != 0
assert [json.loads(line)['event'] for line in r.stdout.splitlines()]==['controller_exit']
results.append(dict(rejected='partial effective mask 0,1023',passed=True))
a.output.write_text(json.dumps(dict(passed=True,checks=results),indent=2)+'\n')
print('PASS:',len(results),'child-affinity checks')
