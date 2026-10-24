#!/usr/bin/env python3
"""Verify the exact retained 72-run archive, including every raw evidence hash."""
import argparse,hashlib,json
from collections import Counter
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args()
stages={'P0':'run','P1':'run-v3','P2':'run-v4','P3':'run-v5'}
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()
reference=json.loads((a.root/'run/provenance.json').read_text())
host=json.loads((a.root/'run/host-initial.json').read_text())
sources={};runs=[];counts=Counter();profiles=[]
for stage,base in stages.items():
    root=a.root/base
    provenance=json.loads((root/'provenance.json').read_text())
    for key in ('kernel_commit','running_build_id','graph_sha256','gapbs_commit','binary_sha256','monitor_sha256','source_sha256'):
        assert provenance[key]==reference[key],(stage,key)
    assert sha(root/'source/run-fused-overhead.py')==sha(a.root/'run/source/run-fused-overhead.py')
    found=sorted((root/'measured'/stage).glob('*/summary.json'));assert len(found)==18,(stage,len(found))
    assert sha(root/'tools/cmdp_monitor')==provenance['monitor_sha256']
    for name,expected in provenance['source_sha256'].items():
        assert sha(root/'source'/name)==expected,(stage,'source',name)
    profiles.append(dict(stage=stage,base=base,source_commit=provenance['source_commit'],plan_sha256=sha(root/'profile-plan.json'),
                         source_snapshot_sha256={p.name:sha(p) for p in sorted((root/'source').iterdir()) if p.is_file()}))
    for receipt in found:
        s=json.loads(receipt.read_text());assert s['pass'] and s['profile_stage']==stage
        assert s['boot_id']==host['boot_id']
        assert s['trials']=={1:38,4:127}[s['threads']]
        assert s['dirty_batch_pages']==s['wp_batch_pages']==512
        counts[stage,s['threads'],s['mode']]+=1
        if s['threads'] not in sources:sources[s['threads']]=s['sources']
        assert s['sources']==sources[s['threads']]
        files={}
        for filename,expected in s['evidence_sha256'].items():
            actual=sha(receipt.parent/filename);assert actual==expected,(receipt,filename)
            files[filename]=dict(sha256=actual,bytes=(receipt.parent/filename).stat().st_size)
        for filename in ('host-before.json','host-after.json'):
            check=json.loads((receipt.parent/filename).read_text())
            for key in ('kernel','boot_id','numa_balancing','taint','settings','governors'):
                assert check[key]==host[key],(receipt,filename,key)
            for key in ('entries','managed','isolated','test_batch_waiting'):
                assert check['stats'][key]==0,(receipt,filename,key)
        qfile=root/'quality'/(stage+'-'+receipt.parent.name+'.json')
        q=json.loads(qfile.read_text()) if stage!='P0' else None
        if q:
            assert q['pass_'] and q['lost_samples']==q['throttle_records']==q['unthrottle_records']==0
            assert q['bytes']==files['perf.data']['bytes']
        runs.append(dict(run=str(receipt.parent.relative_to(a.root)),summary_sha256=sha(receipt),files=files,
                         quality_sha256=sha(qfile) if q else None,passed=True))
    print(stage,'verified',len(found),'receipts and all evidence',flush=True)
assert len(runs)==72 and len(counts)==24 and set(counts.values())=={3}
final_audit=json.loads((a.root/'run-v5/final-audit.json').read_text())
assert final_audit['passed'] and final_audit['host']['boot_id']==host['boot_id']
result=dict(passed=True,retained_runs=72,three_repetitions_per_cell=True,stages=profiles,
    common_provenance={key:reference[key] for key in ('kernel_commit','running_build_id','graph_sha256','gapbs_commit','binary_sha256','monitor_sha256','source_sha256')},
    boot_id=host['boot_id'],raw_and_decoded_evidence_hashes_verified=True,loss_and_throttle_records=0,
    final_audit_sha256=sha(a.root/'run-v5/final-audit.json'),
    automatic_perf_ceiling=dict(initial=final_audit['perf_auto_ceiling_initial'],final=final_audit['perf_auto_ceiling_final'],requested_P2_hz=99),
    excluded='Broad v1 P1 pilot, incomplete-readiness v2 P1 pilots, three v3 CPU-clock P2 pilots',runs=runs)
(a.root/'analysis/archive-verification.json').write_text(json.dumps(result,indent=2)+'\n')
print('PASS: 72 retained runs, guarded host settings and local raw evidence verified; automatic perf ceiling recorded separately')
