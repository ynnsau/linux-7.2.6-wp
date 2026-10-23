#!/usr/bin/env python3
"""Extract complete range/dirty VM evidence and validate counters and workloads."""
import argparse, hashlib, importlib.util, json, pathlib, re, sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('run', type=pathlib.Path)
p.add_argument('--scripts-dir', type=pathlib.Path, required=True)
a=p.parse_args()
run=a.run
raw=(run/'serial.log').read_bytes()
lines=raw.decode('utf-8',errors='replace').splitlines()
errors=[]
def require(ok, message):
    if not ok: errors.append(message)
require((run/'qemu.exit').read_text().strip()=='0','QEMU exit is not zero')
require(sum(x.strip()=='CMDP_RANGE_VM_START' for x in lines)==1,'Missing/ambiguous VM start')
require(sum(x.strip()=='CMDP_RANGE_VM_RESULT=0' for x in lines)==1,'Missing/ambiguous passing VM result')
for item in ('host-boot-id','host-uname','host-cmdp'):
    require((run/(item+'.before')).read_bytes()==(run/(item+'.after')).read_bytes(), item+' changed')
bad=re.compile(r'BUG:|WARNING:|Oops:|KASAN:|Kernel panic|possible circular locking|inconsistent lock state|general protection fault|CMDP_CONSOLE_DRAIN_FAILED|^not ok ')
warnings=[x for x in lines if bad.search(x)]
require(not warnings,'Guest warning/failure signature')
# Every TAP suite must have an explicit, complete sequence and nonzero plan.
taps=[]; tap=None
for line in lines:
    v=line.strip()
    if v=='TAP version 13':
        tap={'checks':[], 'plans':[], 'failures':[]}; taps.append(tap)
    elif tap is not None:
        plan=re.fullmatch(r'1\.\.([0-9]+)(?:\s+#.*)?',v)
        check=re.match(r'^(not ok|ok)\s+([0-9]+)\b',v)
        if plan: tap['plans'].append(int(plan.group(1)))
        if check:
            tap['checks'].append(int(check.group(2)))
            if check.group(1)=='not ok': tap['failures'].append(v)
for t in taps:
    n=t['plans'][0] if len(t['plans'])==1 else 0
    t['pass']=bool(n and t['checks']==list(range(1,n+1)) and not t['failures'])
    t['planned']=n; t['observed']=len(t.pop('checks'))
require(bool(taps) and all(t['pass'] for t in taps),'Incomplete/failing TAP suite')
# Parse bounded report blocks; tolerate workload stderr mixed with JSON stdout.
blocks={}; active=None; previous=None
for line in lines:
    v=line.strip()
    b=re.fullmatch(r'CMDP_REPORT_BEGIN ([A-Za-z0-9_-]+)',v)
    e=re.fullmatch(r'CMDP_REPORT_END ([A-Za-z0-9_-]+) result=(-?[0-9]+)',v)
    if b:
        name=b.group(1)
        require(active is None and name not in blocks,'Nested/duplicate report block '+name)
        blocks[name]={'json':[], 'target':[], 'exit':None}; active=name; previous=None
    elif e:
        require(active==e.group(1),'Mismatched report end '+e.group(1))
        if active:
            blocks[active]['exit']=int(e.group(2)); previous=active
        active=None
    elif active:
        blocks[active]['json' if v.startswith('{') else 'target'].append(line)
    elif previous:
        blocks[previous]['target'].append(line)
require(active is None,'Unclosed report block')
spec=importlib.util.spec_from_file_location('validate_run',str(a.scripts_dir/'validate-run.py'))
validator=importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
report_root=run/'reports'
report_root.mkdir(exist_ok=True)
workloads={}
for name, block in blocks.items():
    out=report_root/name; out.mkdir(exist_ok=True)
    (out/'monitor.jsonl').write_text('\n'.join(block['json'])+'\n')
    target='\n'.join(block['target'])+'\n'; (out/'target.log').write_text(target)
    result=validator.validate(out, minimum=1)
    local_errors=list(result['errors'])
    if block['exit']!=0: local_errors.append('Controller block exit is not zero')
    if name.startswith('recovery-') and not result.get('diagnostics'):
        local_errors.append('Recovery workload lacks invalidation/sampling diagnostics')
    if 'bfs' in name or name.startswith('recovery-pr'):
        marks=re.findall(r'Verification:\s*(PASS|FAIL)',target)
        trials=re.findall(r'Trial Time:\s*[0-9.eE+-]+',target)
        if marks!=['PASS']*8 or len(trials)!=8: local_errors.append('Expected exactly 8 verified generated graph trials')
        application='pr' if name.startswith('recovery-pr') else 'bfs'
        result[application+'_verifications']=len(marks); result[application+'_trials']=len(trials)
    if name in ('hotcold','concurrent','churn'):
        if re.findall(r'fixture_validation=(PASS|FAIL)',target)!=['PASS']:
            local_errors.append('Expected one fixture validation PASS')
    result['errors']=local_errors; result['pass']=not local_errors
    (out/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    workloads[name]=result
    require(result['pass'],'Workload failed: '+name)
suite_match=re.search(r'cmdp_suite=([^ "\\]+)',(run/'qemu-command.txt').read_text())
suite=suite_match.group(1) if suite_match else 'unknown'
if suite=='fused':
    require(not blocks and len(taps)==1 and taps[0]['planned']>=490, 'Missing shared-flush fixture TAP checks')
    cases=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_FUSED_CASE ')]
    pairs=((1,1),(1,64),(64,1),(17,91),(64,64),(512,512),(4096,4096),
           (16384,16384),(65536,65536),(65536,64),(64,65536))
    expected={(str(d),str(w),str(t)) for d,w in pairs for t in (1,4)}
    require(len(cases)==22 and {(x.get('dirty'),x.get('wp'),x.get('threads')) for x in cases}==expected,
            'Missing asymmetric/large-limit one/four-thread matrix')
    metrics=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_FUSED_METRIC ')]
    require(len(metrics)==22 and all(0<int(x['max_dirty'])<=int(x['dirty']) and
            0<int(x['max_wp'])<=int(x['wp']) and int(x['span'])<=max(int(x['dirty']),int(x['wp'])) and
            int(x['allocated'])>=int(x['payload'])>0 for x in metrics), 'Invalid fused flush measurements')
    require(all(int(x['shared'])>0 for x in metrics if min(int(x['dirty']),int(x['wp']))>1),
            'Missing mixed dirty/ARM shared flush evidence')
    writer_cases=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_FUSED_MIXED_WRITERS ')]
    require(len(writer_cases)==1 and writer_cases[0].get('resumed')=='4' and
            int(writer_cases[0].get('wp',0))>=64 and int(writer_cases[0].get('shared',0))>0,
            'Missing concurrent four-writer shared-flush proof')
    allocation=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_FUSED_ALLOCATION_BOUND ')]
    require(len(allocation)==1 and int(allocation[0]['history'])<=256 and
            int(allocation[0]['allocated'])<=41984 and int(allocation[0]['resources'])>0,
            'Missing bounded history-allocation evidence')
elif suite=='batch':
    require(not blocks and len(taps)==1 and taps[0]['planned']>=250, 'Missing batch fixture TAP checks')
    cases=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_BATCH_CASE ')]
    expected={(str(n),str(t)) for n in (1,63,64,65,129,513) for t in (1,4)}
    require({(x.get('pages'),x.get('threads')) for x in cases if x.get('kind')=='boundary'}==expected,
            'Missing one/four-thread batch boundaries')
    for kind in ('reset-window','partial-abort','arm-writers','stop-pending','capacity'):
        require(sum(x.get('kind')==kind for x in cases)==1,'Missing batch case '+kind)
    metrics=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_BATCH_METRIC ')]
    require(len(metrics)==24 and all(0<int(x['batches'])<=int(x['pages'])<=64*int(x['batches']) for x in metrics),
            'Invalid/missing actual batch flush measurements')
elif suite=='dirty':
    require(set(blocks)=={'observe-bfs1','observe-bfs4','qualify-bfs1','qualify-bfs4'},'Missing dirty BFS mode/thread blocks')
    require(len(taps)==5 and taps[0]['planned']==18 and taps[1]['planned']==59 and taps[-1]['planned']>=80,'Unexpected dirty regression TAP suites')
    require(sum(x.strip()=='CMDP_DIRTY_SWAP_READY' for x in lines)==1,'Swap was not enabled')
    require(sum(x.strip()=='CMDP_DIRTY_SWAP_DRAINED' for x in lines)==1,'Swap was not drained')
    require(any(re.search(r'^# swapped anonymous pages=[1-9][0-9]* ',x) for x in lines),'No actual swapout evidence')
elif suite=='small':
    require(set(blocks)=={'hotcold','concurrent','churn','bfs'},'Missing small workload blocks')
    require(len(taps)>=2 and taps[0]['planned']==18 and taps[1]['planned']==59,'Missing legacy regression tests')
elif suite in ('recovery-baseline','recovery','recovery-numa'):
    expected_taps=4 if suite=='recovery-numa' else 2
    require(len(taps)==expected_taps,'Missing recovery thread/topology TAP cases')
    configurations=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_RECOVERY_CONFIG ')]
    require(len(configurations)==expected_taps,'Missing recovery fixture configurations')
    if suite=='recovery-baseline':
        require(not blocks,'Baseline reproduction unexpectedly ran workloads')
        require(sorted(x.get('threads') for x in configurations)==['1','4'],'Missing one/four-thread baseline')
        require(all(x.get('expected')=='stranded' for x in configurations),'Baseline must explicitly expect failure')
        require(sum(x.strip()=='CMDP_RECOVERY_EXPECTED_FAILURE_REPRODUCED stranded=yes' for x in lines)==2,
                'Missing deterministic pre-fix stranded evidence')
    elif suite=='recovery':
        require(set(blocks)=={'recovery-bfs1','recovery-bfs4','recovery-pr1','recovery-pr4'},'Missing recovery BFS/PR correctness matrix')
        require(sorted(x.get('threads') for x in configurations)==['1','4'],'Missing one/four-thread recovery')
        require(all(x.get('expected')=='recovered' for x in configurations),'Fixed suite must use strict recovered expectation')
        require(sum(x.strip()=='CMDP_RECOVERY_FIXED_VERIFIED recovered=yes' for x in lines)==2,
                'Missing strict fixed-behavior evidence')
        pages=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_RECOVERY_PAGE ')]
        for stage in ('armed','invalidated','first-observation','recovered','saved-dirty-untracked','saved-dirty-primed'):
            require(sum(x.get('stage')==stage for x in pages)==2,'Missing page evidence: '+stage)
        recovered=[x for x in pages if x.get('stage')=='recovered']
        require(all(int(x['qualified_ns'])>=int(x['quiet_ns'])+200000000 for x in recovered),'Recovery reused old quiet age')
    else:
        require(not blocks,'NUMA fixture unexpectedly ran workloads')
        events=[dict(re.findall(r'(\w+)=([^ ]+)',x)) for x in lines if x.startswith('CMDP_RECOVERY_NUMA ')]
        require({(x.get('threads'),x.get('cross_node')) for x in events}=={('1','0'),('1','1'),('4','0'),('4','1')},
                'Missing local/cross-node one/four-thread NUMA cases')
        require(all(int(x.get('calls',0))>0 and int(x.get('arm_revoked',0))>0 and x.get('recovered')=='1' for x in events),
                'Actual NUMA invalidation did not revoke ARM in every case')
stats={}
for line in lines:
    m=re.fullmatch(r'(arms|publishes|revokes|waiters|rejects|entries|managed|isolated|stale|arm_ns|revoke_ns|dirty_flush_batches|dirty_flush_pages|arm_flush_batches|arm_flush_pages|scan_active_skips|scan_page_lookups|test_batch_waits|test_batch_waiting)\s+([0-9]+)',line.strip())
    if m: stats[m.group(1)]=int(m.group(2))
for key in ('entries','managed','isolated'): require(stats.get(key)==0,'Final '+key+' is not zero')
summary={'pass':not errors,'suite':suite,'errors':errors,'tap_suites':taps,'workloads':workloads,'global_stats':stats,'warning_signatures':warnings,'serial_sha256':hashlib.sha256(raw).hexdigest()}
(run/'validation.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({'run':str(run),'suite':suite,'pass':summary['pass'],'tap_checks':[t['planned'] for t in taps],'workloads':list(workloads),'errors':errors}))
sys.exit(0 if summary['pass'] else 1)
