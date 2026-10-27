#!/usr/bin/env python3
"""Verify all paired explicit-scanner CPU receipts, evidence hashes and host guards."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('base',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
def read(path):return json.loads(path.read_text())
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()
provenance=read(a.base/'provenance.json');host=read(a.base/'host-initial.json');plan=read(a.base/'scanner-cpu-plan.json')
assert plan['measured_runs']==72 and plan['repetitions']==3 and len(plan['schedule'])==36
assert sha(a.base/'tools/cmdp_monitor')==provenance['monitor_sha256']
for name,digest in provenance['source_sha256'].items():assert sha(a.base/'source'/name)==digest,name
assert read(a.base/'campaign-summary.json')['passed']
assert read(a.base/'campaign-state.json')['stage']=='complete'
assert (a.base/'campaign.exit').read_text().strip()=='0'
counts=Counter();sources={};runs=[];nfiles=nbytes=0
for stage in ('P0','P1'):
    for entry in plan['schedule']:
        name='b{:02d}-{}-{}-{}'.format(entry['block'],entry['cell'],entry['mode'],entry['placement'])
        run=a.base/'measured'/stage/name;s=read(run/'summary.json')
        assert s['pass'] and s['source_commit']==provenance['source_commit'] and s['boot_id']==host['boot_id']
        assert s['profile_stage']==stage and s['mode']==entry['mode'] and s['placement']==entry['placement']
        assert s['threads']==plan['cells'][entry['cell']]['threads'] and s['trials']=={1:38,4:127}[s['threads']]
        assert s['dirty_batch_pages']==s['wp_batch_pages']==512
        assert s['controller_cpus']==[5]
        assert s['application_cpus']==plan['application_cpus'][str(s['threads'])]
        assert s['workers']['expected_cpus']==s['application_cpus'] and s['workers']['bound_samples']>0
        if s['threads'] not in sources:sources[s['threads']]=s['sources']
        assert s['sources']==sources[s['threads']]
        assert s['scanner_cpu']==(None if s['mode']=='A' else plan['placements'][s['placement']]['scanner_cpu'])
        if s['mode']!='A':
            assert s['cpu']['work_items']>0 and s['cpu']['cpu']==s['scanner_cpu']
            assert s['cpu']['error']==s['cpu']['offline_events']==s['cpu']['wrong_cpu_work_items']==0
        if s['mode']=='C':assert s['final']['protected_byte_ms']>0
        files={}
        for name,digest in s['evidence_sha256'].items():
            f=run/name;assert sha(f)==digest,(run,name)
            files[name]=dict(sha256=digest,bytes=f.stat().st_size);nfiles+=1;nbytes+=f.stat().st_size
        for name in ('host-before.json','host-after.json'):
            observed=read(run/name)
            for key in ('kernel','boot_id','numa_balancing','taint','settings','governors'):assert observed[key]==host[key],(run,key)
            for key in ('entries','managed','isolated','test_batch_waiting'):assert observed['stats'][key]==0,(run,key)
            assert observed['stats']['arms']==observed['stats']['publishes']==observed['stats']['revokes']
        if stage=='P1':
            q=read(a.base/'quality'/('P1-'+run.name+'.json'))
            assert q['pass_'] and q['lost_samples']==q['throttle_records']==q['unthrottle_records']==0
            assert q['bytes']==files['perf.data']['bytes']
            d=read(a.base/'contexts'/('P1-'+run.name+'.json'))
            assert not d['anomalies'] and not d['unclosed_scanner_items']
            assert d['explicit_placement_verified']
            assert d['scanner_work_items']==s['cpu'].get('work_items',0)
            cpus={int(k) for k,v in d['scanner_cpu_distribution_s'].items() if v>0}
            assert cpus==(set() if s['mode']=='A' else {s['scanner_cpu']})
            for i,source in enumerate(('unknown','numa','protection','mprotect','mremap','unmap','clear','test')):
                if s['mode']=='A':assert not d['invalidations'];break
                wanted=s['diagnostics']['invalidation_sources'][source]
                got=[v for k,v in d['invalidations'].items() if k.endswith('-source'+str(i))]
                for field,expected in [('calls','calls'),('revoked','arm_revoked'),('histories','histories_reset')]:assert sum(x[field] for x in got)==wanted[expected],(run,source,field)
                assert abs(sum(x['elapsed_s'] for x in got)-wanted['elapsed_ns']/1e9)<1e-6
        counts[stage,s['threads'],s['mode'],s['placement']]+=1
        runs.append(dict(run=str(run.relative_to(a.base)),summary_sha256=sha(run/'summary.json'),files=files,passed=True))
    print(stage,'verified 36 paired receipts and evidence',flush=True)
assert len(runs)==72 and len(counts)==24 and set(counts.values())=={3}
pilot=read(a.base/'pilot-summary.json');assert pilot['passed'] and len(pilot['runs'])==14
pilot_files=0
for name,stored in pilot['runs'].items():
    run=a.base/name;s=read(run/'summary.json')
    assert s==stored and s['pass'] and s['source_commit']==provenance['source_commit']
    assert s['boot_id']==host['boot_id']
    for filename,digest in s['evidence_sha256'].items():
        assert sha(run/filename)==digest,(name,filename)
        pilot_files+=1
    if s['profile_stage']=='P1':
        q=read(a.base/'quality'/('P1-'+run.name+'.json'))
        d=read(a.base/'contexts'/('P1-'+run.name+'.json'))
        assert q['pass_'] and q['lost_samples']==q['throttle_records']==q['unthrottle_records']==0
        assert d['explicit_placement_verified'] and not d['anomalies'] and not d['unclosed_scanner_items']
        assert {int(k) for k,v in d['scanner_cpu_distribution_s'].items() if v>0}=={s['scanner_cpu']}
for key in ('kernel','boot_id','numa_balancing','taint','settings','governors'):assert read(a.base/'host-final.json')[key]==host[key]
result=dict(passed=True,runs=72,three_repetitions_per_case=True,files=nfiles,bytes=nbytes,
    pilot_cases=14,pilot_files=pilot_files,
    source_commit=provenance['source_commit'],kernel_commit=provenance['kernel_commit'],
    running_build_id=provenance['running_build_id'],boot_id=host['boot_id'],
    monitor_sha256=provenance['monitor_sha256'],plan_sha256=sha(a.base/'scanner-cpu-plan.json'),
    sources_sha256={f.name:sha(f) for f in sorted((a.base/'source').iterdir()) if f.is_file()},
    excluded='Twelve generated correctness cases plus 1t B/shared and 4t C/separate Twitter P1 pilots; all preserved separately.',evidence=runs)
a.output.write_text(json.dumps(result,indent=2)+'\n');print('PASS:',nfiles,'files;',nbytes,'bytes')
