import datetime
import hashlib
import json
import os
import pathlib
import subprocess

P = pathlib.Path
root = P('/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1')
os.sched_setaffinity(0, {24})
def read(path):
    return P(path).read_text().strip()
def obj(path):
    return json.loads(read(path))
def command(args):
    return subprocess.check_output(args, universal_newlines=True).strip()
manifest = obj(root / 'source-manifest.json')
hashes_ok = all(hashlib.sha256(P(p).read_bytes()).hexdigest() == value
                for p, value in manifest['hashes'].items())
policies = []
for p in P('/sys/devices/system/cpu/cpufreq').glob('policy*'):
    policies.append(dict(name=p.name, governor=read(p/'scaling_governor'),
                         minimum_khz=int(read(p/'scaling_min_freq')),
                         maximum_khz=int(read(p/'scaling_max_freq')), boost=int(read(p/'boost'))))
state = obj(root / 'tail-state.json')
run = root / state['attempt'] / state['run'] if 'attempt' in state else None
payload = dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               kernel=os.uname().release, boot_id=read('/proc/sys/kernel/random/boot_id'),
               service=command(['systemctl','show','cmdp-redis-tail-v1.service','-p','MainPID',
                                '-p','ActiveState','-p','SubState','-p','Restart','-p','KillMode',
                                '-p','TimeoutStopUSec','-p','UnitFileState','-p','ActiveEnterTimestamp']),
               frozen_source_hashes_match=hashes_ok, source_commit=manifest['commit'],
               policies=policies, fixed_2100000_policy=all(
                   p['governor']=='performance' and p['minimum_khz']==p['maximum_khz']==2100000 and p['boost']==0 for p in policies),
               boost=int(read('/sys/devices/system/cpu/cpufreq/boost')),
               cmdp_stats=read('/sys/kernel/debug/cmdp/stats'), tail_state=state,
               dmesg_last_lines=command(['dmesg','--ctime']).splitlines()[-15:])
if run:
    payload['run_files']={name:obj(run/name) for name in
                         ('run.json','restore.json','eligibility.json','processes.json','timeline.json','boundaries.json','readiness.json') if (run/name).exists()}
    for name in ('redis','ycsb'):
        identity=payload['run_files'].get('processes.json',{}).get(name)
        if identity:
            payload[name+'_status']=read('/proc/%s/status'%identity['pid'])
(root/'launch-state.json').write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
print(json.dumps({k:payload[k] for k in ('at','service','fixed_2100000_policy','frozen_source_hashes_match','source_commit')},indent=2))
