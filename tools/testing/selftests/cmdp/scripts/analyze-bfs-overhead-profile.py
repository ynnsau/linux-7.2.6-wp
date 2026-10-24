#!/usr/bin/env python3
"""Decode saved perf script streams; task and exact work-item attribution.

Analysis only. Does not modify raw run evidence. Uses Python standard library.
Elapsed lock/notifier times and inclusive stack weights are non-additive.
"""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import re
import statistics

HEADER = re.compile(r'^\s*(.*?)\s+(-?\d+)\s+\[(\d+)\]\s+(\d+\.\d+):\s+(?:(\d+)\s+)?(\S+):\s*(.*)$')
FRAME = re.compile(r'^\s+[0-9a-f]+\s+(.+?)\s+\((.*)\)\s*$')
SWITCH = re.compile(r'prev_comm=(.*?) prev_pid=(\d+) prev_prio=\d+ prev_state=(\S+) ==> next_comm=(.*?) next_pid=(\d+)')
WAKE = re.compile(r'comm=(.*?) pid=(\d+) prio=\d+ target_cpu=(\d+)')


def events(path):
    record = None
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(str(path), 'rt', errors='replace') as f:
        for line in f:
            match = HEADER.match(line)
            if match:
                if record:
                    yield record
                comm, tid, cpu, ts, period, event, payload = match.groups()
                record = dict(comm=comm, tid=int(tid), cpu=int(cpu), ts=float(ts),
                              period=int(period or 0), event=event, payload=payload, frames=[],raw_frames=[])
            elif record:
                match = FRAME.match(line)
                if match:
                    symbol, dso = match.groups()
                    record['raw_frames'].append((int(line.split()[0],16),symbol,dso))
                    if not record['frames']:
                        # x86-64 kernel modules also have high canonical IPs;
                        # their DSO names need not contain "kernel" or "vmlinux".
                        record['kernel_frame'] = bool(int(line.split()[0],16) & (1<<63)) and dso != '[vsyscall]'
                    record['frames'].append((re.sub(r'\+0x[0-9a-f]+$', '', symbol), dso))
                elif re.search(r'PERF_RECORD_LOST|LOST \d+|lost \d+ events', line):
                    raise AssertionError('Lost perf events: ' + line)
    if record:
        yield record


def total(intervals):
    return sum(b-a for a,b in intervals)


def merge(intervals):
    result = []
    for a,b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a,b])
    return result


def intersect(left, right):
    left,right=merge(left),merge(right)
    i=j=0; result=[]
    while i<len(left) and j<len(right):
        a,b=max(left[i][0],right[j][0]),min(left[i][1],right[j][1])
        if b>a: result.append([a,b])
        if left[i][1] < right[j][1]: i+=1
        else: j+=1
    return result


def stack_kind(frames):
    names = ' '.join(x[0] for x in frames)
    if re.search(r'rwsem|mmap.*lock|lock_mm_and_find_vma', names): return 'mmap/rwsem'
    if re.search(r'__vma_start_exclude_readers|__vma_start_write', names): return 'VMA reader exclusion'
    if 'futex' in names: return 'futex/barrier'
    if 'cmdp_monitor_scan' in names and 'schedule_timeout_uninterruptible' in names: return 'scanner retry sleep'
    if 'lru_add_drain_all' in names and 'wait_for_completion' in names: return 'LRU drain completion'
    if re.search(r'io_schedule|wait_on_page|folio_wait', names): return 'IO/page wait'
    if 'completion' in names: return 'completion'
    if re.search(r'hrtimer_nanosleep|do_nanosleep|schedule_timeout|msleep', names): return 'sleep'
    return 'other/unknown'


def analyze_run(path):
    summary=json.loads((path/'summary.json').read_text())
    command=json.loads((path/'command.json').read_text())
    records=[json.loads(x) for x in (path/'monitor.jsonl').read_text().splitlines() if x.strip()]
    controller=next(x for x in records if x['event']=='controller')
    tids={controller['pid']}; tid_cpu={}
    for line in (path/'memory-samples.jsonl').read_text().splitlines():
        r=json.loads(line)
        if not r.get('error') and r.get('exe','').endswith('/bfs'):
            for task in r['threads']:
                tids.add(task['tid'])
                if len(task['cpu_allowed'])==1: tid_cpu[task['tid']]=task['cpu_allowed'][0]
    begin=command['launcher_begin_ns']/1e9; end=command['launcher_end_ns']/1e9
    out=dict(analysis_version=7,run=str(path), profile_stage=summary['profile_stage'],threads=summary['threads'],
        mode=summary['mode'],elapsed_seconds=summary['elapsed_seconds'],target_tids=sorted(tids),
        target_user_seconds=summary['target_rusage'].get('user_us',0)/1e6,
        target_system_seconds=summary['target_rusage'].get('system_us',0)/1e6)
    # Preserve actual rusage field names; no zero-default assumptions for report.
    out['rusage']=summary['target_rusage']
    if not (path/'perf-script.txt.gz').exists(): return out
    quality_path = path.parents[2] / 'quality' / (path.parent.name+'-'+path.name+'.json')
    q = json.loads(quality_path.read_text())
    assert q.get('schema_version',0) >= 2, 'Lifecycle clock metadata required'
    lifecycle = [x for x in q['lifecycle'] if x['pid'] == x['tid'] == command['controller_pid']]
    starts = [x['time_ns']/1e9 for x in lifecycle if x['event'] == 'fork']
    stops = [x['time_ns']/1e9 for x in lifecycle if x['event'] == 'exit']
    assert len(starts) == len(stops) == 1, lifecycle
    native_begin,native_end = starts[0],stops[0]
    assert native_end > native_begin
    alignment = dict(method='native perf controller fork/exit; durations stay in perf clock',
        monotonic_begin_s=begin,monotonic_end_s=end,perf_fork_s=native_begin,perf_exit_s=native_end,
        begin_offset_seconds=begin-native_begin,end_offset_seconds=end-native_end,
        duration_difference_seconds=(end-begin)-(native_end-native_begin),
        timeline_zero='controller fork in perf clock',
        boundary_note='Popen/wait timer brackets kernel fork/exit; no claim of an exact cross-clock offset')
    assert abs(alignment['duration_difference_seconds']) < .1, alignment
    begin,end=native_begin,native_end
    out['clock_alignment']=alignment
    counts=Counter(); work_active={}; work_intervals=defaultdict(list)
    on={}; off={}; sched_segments=defaultdict(list); waits=defaultdict(list)
    scanner_seen=set(); samples=defaultdict(Counter); inclusive=defaultdict(Counter)
    context_seconds=Counter(); scanner_sample_cpus=Counter(); mmap_pending={}; mmap_held={}
    mmap_results=defaultdict(lambda:dict(calls=0,wait_s=0.,hold_s=0.,failed=0,wait_max_s=0.,hold_max_s=0.))
    invalidations=[]; tlb=Counter(); tlb_pages=Counter(); blocked_stacks=Counter(); unknown=Counter()
    scanner_blocked_details=defaultdict(list);scanner_blocked_stacks=Counter()
    timeline=defaultdict(Counter)
    first_ts=last_ts=None;sample_unit=None
    def scope(tid):
        if tid in tids: return 'bfs'
        if tid in work_active: return 'scanner'
        return 'other'
    for e in events(path/'perf-script.txt.gz'):
        ts,tid,cpu,event,payload=e['ts'],e['tid'],e['cpu'],e['event'],e['payload']
        if first_ts is None:first_ts=ts
        last_ts=ts;counts[event]+=1
        if event=='workqueue:workqueue_execute_start':
            if tid in work_active: unknown['nested_scanner_start']+=1
            work_active[tid]=ts;scanner_seen.add(tid)
        elif event=='workqueue:workqueue_execute_end':
            start=work_active.pop(tid,None)
            if start is None: unknown['unmatched_scanner_end']+=1
            else:work_intervals[tid].append([max(begin,start),min(end,ts)])
        elif event.startswith('sched:sched_switch'):
            m=SWITCH.search(payload)
            if not m: unknown['unparsed_switch']+=1;continue
            pc,prev,state,nc,nxt=m.groups();prev,nxt=int(prev),int(nxt)
            if pc=='bfs':tids.add(prev)
            if nc=='bfs':tids.add(nxt)
            # PID zero is per-CPU idle; only real tasks are keyed here.
            start=on.pop(prev,None) if prev else None
            if start and (prev in tids or prev in scanner_seen):
                lo,hi=max(begin,start[0]),min(end,ts)
                if hi>lo:sched_segments[prev].append([lo,hi,start[1]])
            if prev and (prev in tids or prev in scanner_seen):
                off[prev]=dict(start=ts,wake=ts if state.startswith('R') else None,
                               state=state,frames=e['frames'],cpu=cpu)
            old=off.pop(nxt,None)
            if old:
                lo,hi=max(begin,old['start']),min(end,ts)
                if hi>lo:
                    wake=old['wake']
                    if wake is None:
                        kind='blocked' if not old['state'].startswith('R') else 'runnable'
                        if nxt in tids and kind=='blocked':unknown['target_resume_without_observed_wakeup']+=1
                        waits[nxt].append([lo,hi,kind,stack_kind(old['frames']),old['cpu']])
                    else:
                        wake=max(lo,min(hi,wake))
                        if wake>lo:waits[nxt].append([lo,wake,'blocked',stack_kind(old['frames']),old['cpu']])
                        if hi>wake:waits[nxt].append([wake,hi,'runnable',stack_kind(old['frames']),old['cpu']])
                    if not old['state'].startswith('R'):
                        block_end=hi if wake is None else max(lo,min(hi,wake))
                        key=';'.join(x[0] for x in old['frames'])
                        if nxt in tids:blocked_stacks[key]+=max(0,block_end-lo)
                        elif nxt in scanner_seen:scanner_blocked_details[nxt].append([lo,block_end,key])
            if nxt:on[nxt]=(ts,cpu)
        elif event in ('sched:sched_wakeup','sched:sched_wakeup_new'):
            m=WAKE.search(payload)
            if m:
                target=int(m.group(2))
                if target in off and off[target]['wake'] is None:off[target]['wake']=ts
        elif event.startswith(('cpu-clock','cycles/')) and begin<=ts<=end:
            s=scope(tid)
            if not tid:s='idle'
            elif s=='bfs':s='bfs-kernel' if e.get('kernel_frame',False) else 'bfs-user'
            unit='seconds' if event.startswith('cpu-clock') else 'cycles'
            assert sample_unit in (None,unit), 'Mixed sample units'
            sample_unit=unit
            weight=e['period']/1e9 if unit=='seconds' else e['period']
            context_seconds[s]+=weight
            if s in ('scanner','bfs-kernel','bfs-user'):
                if e['frames']:samples[s][e['frames'][0][0]]+=weight
                for name in set(x[0] for x in e['frames']):inclusive[s][name]+=weight
                timeline[int(ts-begin)][s]+=weight
                if s=='scanner':scanner_sample_cpus[cpu]+=weight
        elif event.startswith('mmap_lock:'):
            f=dict(re.findall(r'(\w+)=([^ ]+)',payload));key=(tid,f.get('mm'),f.get('write'));s=scope(tid)
            if event.endswith('start_locking'):mmap_pending[key]=(ts,s)
            elif event.endswith('acquire_returned'):
                start=mmap_pending.pop(key,None)
                if start:
                    stat=mmap_results[start[1]+'-'+str(f.get('write'))]
                    wait=max(0,ts-start[0]);stat['calls']+=1;stat['wait_s']+=wait;stat['wait_max_s']=max(stat['wait_max_s'],wait)
                    if f.get('success')=='true':mmap_held[key]=(ts,start[1])
                    else:stat['failed']+=1
            elif event.endswith('released'):
                start=mmap_held.pop(key,None)
                if start:
                    stat=mmap_results[start[1]+'-'+str(f.get('write'))]
                    hold=max(0,ts-start[0]);stat['hold_s']+=hold;stat['hold_max_s']=max(stat['hold_max_s'],hold)
        elif event=='cmdp:cmdp_invalidation':
            f=dict(re.findall(r'(\w+)=([^ ]+)',payload))
            invalidations.append(dict(at_s=ts-begin,tid=tid,cpu=cpu,scope=scope(tid),**f))
        elif event=='tlb:tlb_flush' and begin<=ts<=end:
            m=re.search(r'reason:(.*?) \((\d+)\)',payload)
            if m:
                key='{}|cpu{}|{}'.format(scope(tid),cpu,m.group(1));tlb[key]+=1
                pages=re.search(r'pages:(-?\d+)',payload);assert pages,payload
                tlb_pages[key+'|pages'+pages.group(1)]+=1
    # Close observed on-CPU spans at trace end. Work usually ends before STOP.
    for tid,(start,cpu) in on.items():
        if tid in tids or tid in scanner_seen:
            lo,hi=max(begin,start),min(end,last_ts)
            if hi>lo:sched_segments[tid].append([lo,hi,cpu])
    scanner_segments=[]; scanner_cpu=Counter()
    scanner_waits=Counter()
    for tid,windows in work_intervals.items():
        by_cpu=defaultdict(list)
        for lo,hi,cpu in sched_segments[tid]:by_cpu[cpu].append([lo,hi])
        for cpu,intervals in by_cpu.items():
            for a,b in intersect(intervals,windows):
                scanner_segments.append([a,b,cpu,tid]);scanner_cpu[cpu]+=b-a
        categories=defaultdict(list)
        for lo,hi,kind,stack,cpu in waits[tid]:
            categories[kind].append([lo,hi])
            if kind=='blocked':categories['blocked:'+stack].append([lo,hi])
        for kind,intervals in categories.items():
            scanner_waits[kind]+=total(intersect(intervals,windows))
        for lo,hi,key in scanner_blocked_details[tid]:
            scanner_blocked_stacks[key]+=total(intersect([[lo,hi]],windows))
    target_stats={}; target_runnable=[]; scanner_runnable_overlap=0.
    for tid in sorted(tids):
        w=waits[tid]; categories=Counter()
        for a,b,kind,stack,cpu in w:
            categories[kind]+=b-a
            if kind=='blocked':categories['blocked:'+stack]+=b-a
            elif kind=='runnable':
                target_runnable.append([a,b,tid_cpu.get(tid,cpu)])
        target_stats[tid]=dict(on_cpu_s=sum(b-a for a,b,_ in sched_segments[tid]),**categories)
    runnable_by_cpu=defaultdict(list);scanner_by_cpu=defaultdict(list)
    for a,b,cpu in target_runnable:runnable_by_cpu[cpu].append([a,b])
    for a,b,cpu,tid in scanner_segments:scanner_by_cpu[cpu].append([a,b])
    for cpu in scanner_by_cpu:
        scanner_runnable_overlap+=total(intersect(runnable_by_cpu[cpu],scanner_by_cpu[cpu]))
    inv_summary=defaultdict(lambda:dict(calls=0,elapsed_s=0.,revoked=0,histories=0))
    for item in invalidations:
        key=item['scope']+'-source'+item['source'];d=inv_summary[key]
        d['calls']+=1;d['elapsed_s']+=int(item['ns'])/1e9;d['revoked']+=int(item['revoked']);d['histories']+=int(item['histories'])
    out.update(trace_events=counts,trace_clock=dict(first_s=first_ts,last_s=last_ts,window_begin_s=begin,window_end_s=end),
        scanner_work_items=sum(map(len,work_intervals.values())),scanner_work_elapsed_s=sum(total(x) for x in work_intervals.values()),
        scanner_on_cpu_s=sum(scanner_cpu.values()),scanner_cpu_distribution_s=scanner_cpu,
        scanner_waits_s=scanner_waits,
        scanner_blocked_stacks_s=scanner_blocked_stacks.most_common(15),
        scanner_overlap_target_runnable_s=scanner_runnable_overlap,target_scheduling=target_stats,
        mmap=mmap_results,invalidations=inv_summary,invalidation_timeline=invalidations,tlb_events=tlb,tlb_page_histogram=tlb_pages,
        sample_weight_unit=sample_unit,
        sampled_context_cpu_s=context_seconds if sample_unit=='seconds' else {},
        scanner_sample_cpu_s=scanner_sample_cpus if sample_unit=='seconds' else {},
        leaf_cpu_s={k:v.most_common() for k,v in samples.items()} if sample_unit=='seconds' else {},
        inclusive_cpu_s={k:v.most_common() for k,v in inclusive.items()} if sample_unit=='seconds' else {},
        sampled_context_cycles=context_seconds if sample_unit=='cycles' else {},
        scanner_sample_cycles=scanner_sample_cpus if sample_unit=='cycles' else {},
        leaf_cycles={k:v.most_common() for k,v in samples.items()} if sample_unit=='cycles' else {},
        inclusive_cycles={k:v.most_common() for k,v in inclusive.items()} if sample_unit=='cycles' else {},
        blocked_stacks_s=blocked_stacks.most_common(15),anomalies=unknown,unclosed_scanner_items=len(work_active),
        cpu_timeline_s=dict(timeline) if sample_unit=='seconds' else {},
        cycle_timeline=dict(timeline) if sample_unit=='cycles' else {},
        scheduling_timeline=dict(scanner=[[a-begin,b-begin,c,t] for a,b,c,t in scanner_segments],
            bfs_running={str(t):[[a-begin,b-begin,c] for a,b,c in sched_segments[t]] for t in sorted(tids)},
            bfs_waits={str(t):[[a-begin,b-begin,k,s,c] for a,b,k,s,c in waits[t]] for t in sorted(tids)}))
    assert q['lost_samples'] == q['throttle_records'] == 0, q
    assert sum(counts.values()) == q['record_counts'].get('9', 0), ('Decoded sample count mismatch', sum(counts.values()), q)
    out['raw_record_quality'] = q
    if (path/'pmu.csv').exists():
        sums=defaultdict(Counter);percentages=[];intervals=defaultdict(lambda:defaultdict(Counter))
        for line in (path/'pmu.csv').read_text().splitlines():
            if line.startswith('#') or not line.strip():continue
            f=line.split(',')
            if len(f)<7:continue
            cpu=int(f[1].replace('CPU',''));value=float(f[2]);event=f[4]
            sums[cpu][event]+=value;percentages.append(float(f[6]));intervals[float(f[0])][cpu][event]+=value
        groups={}
        for name,cpus in [('application_cores',range(summary['threads'])),('collector_cores',[24,25]),('other_cores',[x for x in range(52) if x not in set(range(summary['threads']))|{24,25}])]:
            agg=Counter()
            for cpu in cpus:agg.update(sums[cpu])
            groups[name]=agg
        out['pmu']=dict(groups=groups,per_cpu=sums,min_running_percent=min(percentages),
                        scope='bracketing system-wide window, per CPU; not task-exclusive',intervals=intervals)
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('base',type=Path,nargs='*');p.add_argument('--stage-base',nargs=2,action='append',default=[]);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(exist_ok=True,parents=True)
    outputs=[]
    receipts=[p for base in a.base for p in (base/'measured').glob('*/*/summary.json')]
    receipts += [p for stage,base in a.stage_base for p in (Path(base)/'measured'/stage).glob('*/summary.json')]
    keys=[p.parent.parent.name+'-'+p.parent.name for p in receipts]
    assert len(keys)==len(set(keys)), 'Duplicate retained run keys'
    for receipt in sorted(receipts):
        target=a.output/(receipt.parent.parent.name+'-'+receipt.parent.name+'.json')
        cached=json.loads(target.read_text()) if target.exists() else {}
        if cached.get('analysis_version') == 7 and cached.get('run') == str(receipt.parent):
            result=cached
        else:
            print('Analyze',receipt.parent,flush=True);result=analyze_run(receipt.parent)
            target.write_text(json.dumps(result,indent=2)+'\n')
        outputs.append(result)
    (a.output/'index.json').write_text(json.dumps([dict(run=x['run'],stage=x['profile_stage'],threads=x['threads'],mode=x['mode'],elapsed=x['elapsed_seconds']) for x in outputs],indent=2)+'\n')


if __name__=='__main__':main()
