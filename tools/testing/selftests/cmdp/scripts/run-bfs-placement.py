#!/usr/bin/env python3
"""Paired BFS CPU placement, separate unprofiled and scheduling blocks.

Controller stays on CPU 0; child affinity is set before its direct BFS exec.
Keep the kernel, work, policies and original complete-job timing executor.
Python 3.6 compatible. No shared-worker affinity or global policy changes.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil

SCRIPTS = Path(__file__).resolve().parent
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m
profile = load('placement_profile','run-bfs-overhead-profile.py')
fused, matched = profile.fused, profile.matched
audit = load('placement_audit','audit-perf-data.py')
analysis = load('placement_analysis','analyze-bfs-overhead-profile.py')

def expand_schedule(schedule):
    result = []
    for i, entry in enumerate(schedule):
        placements = ('shared','shifted') if i % 2 == 0 else ('shifted','shared')
        for placement in placements:
            result.append(dict(entry,placement=placement))
    return result

class Campaign(profile.Campaign):
    def __init__(self, a):
        self.placement = 'shared'
        super().__init__(a)

    def state(self, stage, **extra):
        extra.update(profile_stage=self.profile_stage,placement=self.placement,
            measured_completed=sum(k.startswith('measured/') for k in self.summaries),
            measured_total=36*len(self.a.stages))
        matched.Campaign.state(self, stage, **extra)

    def cpus(self, threads):
        start = 0 if self.placement == 'shared' else 1
        return list(range(start,start+threads))

    def preflight(self):
        # Inherited profile-plan.json preserves the base 18-run recipe.
        # placement-plan.json expands it to the actual paired 36-run block.
        super().preflight()
        assert set(range(5)) <= os.sched_getaffinity(0)
        topology = {}
        for cpu in range(5):
            p = Path('/sys/devices/system/cpu/cpu'+str(cpu))
            topology[cpu] = {name:(p/'topology'/name).read_text().strip()
                for name in ('physical_package_id','core_id','thread_siblings_list')}
            assert (p/'node0').exists(), 'Both placements must remain on node 0'
        assert len({(v['physical_package_id'],v['core_id']) for v in topology.values()})==5
        for cpu in range(5):
            assert topology[cpu]['thread_siblings_list']==str(cpu), 'No SMT sharing'
        actual = dict(self.plan)
        actual.update(schedule=expand_schedule(self.plan['schedule']),
            measured_runs=36*len(self.a.stages),controller_cpus=[0],
            placements=dict(shared={'1':[0],'4':[0,1,2,3]},shifted={'1':[1],'4':[1,2,3,4]}),
            topology=topology,scanner_policy='unchanged delayed work; initial queue on CPU 0, timer migration possible; measure actual CPU in P1',
            changed_monitor='child-only --target-cpus before direct exec; common to all fresh cases',
            pair_order='alternate shared/shifted first for adjacent matched pairs',
            recipe='profile-plan.json is the inherited base recipe; this expanded plan governs execution')
        target = self.base/'placement-plan.json'
        if target.exists(): assert json.loads(target.read_text())==json.loads(json.dumps(actual))
        else: matched.save(target,actual)
        self.plan = actual
        for name in ('run-bfs-placement.py','test-monitor-affinity.py','check-bfs-placement.py','audit-perf-data.py','analyze-bfs-overhead-profile.py'):
            src,dest = SCRIPTS/name,self.base/'source'/name
            if dest.exists(): assert matched.sha(src)==matched.sha(dest)
            else: shutil.copyfile(str(src),str(dest))
        src = self.a.source/'wp_doc/bfs-placement-protocol.md'
        shutil.copyfile(str(src),str(self.base/'source'/src.name))
        for name in ('quality','contexts'):
            (self.base/name).mkdir(exist_ok=True)

    def check_workers(self, path, binary, threads, sustained):
        samples = matched.rows(path/'memory-samples.jsonl')
        valid = [r for r in samples if not r.get('error') and r['exe']==str(binary)]
        assert valid and all(r['thp_enabled']=='0' for r in valid)
        assert len({r['start_ticks'] for r in valid})==1
        wanted = set(self.cpus(threads))
        assert all(set(t['cpu_allowed'])<=wanted for r in valid for t in r['threads'])
        matching = [r for r in valid if len(r['threads'])==threads]
        def bound(r):
            return all(len(t['cpu_allowed'])==1 for t in r['threads']) and {t['cpu_allowed'][0] for t in r['threads']}==wanted
        bound_rows = [r for r in matching if bound(r)]
        if sustained:
            assert bound_rows, 'Expected worker binding never observed'
            assert all(bound(r) for r in matching[matching.index(bound_rows[0]):])
            active = set()
            for x,y in zip(bound_rows,bound_rows[1:]):
                previous = {t['tid']:t['utime_ticks']+t['stime_ticks'] for t in x['threads']}
                for t in y['threads']:
                    if t['tid'] in previous and t['utime_ticks']+t['stime_ticks']>previous[t['tid']]:active.add(t['tid'])
            assert len(active)==threads, (active,threads)
        return dict(samples=len(samples),valid_samples=len(valid),bound_samples=len(bound_rows),
            sample_errors=sum(bool(r.get('error')) for r in samples),expected_cpus=sorted(wanted),
            sampled_peak_rss_kib=max(r['memory_kib'].get('VmRSS',0) for r in valid))

    def workload(self, name, app, threads, mode, count, generated=False, verify=False, sustained=False):
        assert app=='bfs'
        path = self.base/name
        cached = self.receipt(path)
        if cached: return cached
        before = self.guard(path/'host-before.json')
        if not generated: self.warm(path)
        cpus = self.cpus(threads)
        argv = ['taskset','-c','0']+self.controller_args(mode,generated)
        argv += ['--target-cpus',','.join(map(str,cpus)),'--',self.a.gapbs/app]
        argv += ['-g','18'] if generated else ['-f',self.graph]
        argv += ['-n',str(count),'-l']
        if verify: argv += ['-v']
        env = dict(OMP_NUM_THREADS=str(threads),OMP_DYNAMIC='FALSE',OMP_PROC_BIND='close',
                   OMP_PLACES=','.join('{'+str(i)+'}' for i in cpus))
        command = self.execute(path,argv,env,timeout=300 if generated else 1200)
        v,timing = self.validate_controller(path,mode,before,mode=='C' and sustained,minimum=0 if generated else 1)
        if mode!='A':
            assert v['dirty']['quiet_ms']==(10 if generated else 10000)
            assert v['diagnostics']
        parsed = matched.parse_log((path/'target.log').read_text(),app,count,verify)
        workers = self.check_workers(path,self.a.gapbs/app,threads,sustained=not generated)
        result = dict(application=app,threads=threads,mode=mode,trials=count,placement=self.placement,
            application_cpus=cpus,controller_cpus=[0],generated=generated,verify=verify,
            elapsed_seconds=command['elapsed_seconds'],workers=workers,completed_sweeps=v['completed_sweeps'],
            dirty=v['dirty'],final=v['final'],progress=v['progress'],diagnostics=v['diagnostics'])
        result.update(parsed); result.update(timing)
        return self.finish(path,result)

    def check_trace(self, path):
        q = audit.audit(path/'perf.data')
        matched.save(self.base/'quality'/(path.parent.name+'-'+path.name+'.json'),q)
        assert q['pass_'] and not q['unthrottle_records'], 'Trace loss/throttle'
        d = analysis.analyze_run(path)
        d['placement'] = self.placement
        assert not d['unclosed_scanner_items'] and not d['anomalies'], d['anomalies']
        gap=d['scanner_work_elapsed_s']-d['scanner_on_cpu_s']-sum(d['scanner_waits_s'].get(k,0) for k in ('runnable','blocked'))
        assert abs(gap)<.001, ('scanner schedule balance',gap)
        cpus = {int(k) for k,v in d['scanner_cpu_distribution_s'].items() if v>0}
        if d['mode']!='A': assert d['scanner_work_items']>0 and cpus
        d['scanner_only_cpu0']=cpus <= {0}
        d['scanner_on_application_cpu_s']=sum(v for k,v in d['scanner_cpu_distribution_s'].items() if int(k) in self.cpus(d['threads']))
        d['separation_verified']=d['mode']=='A' or d['scanner_on_application_cpu_s']==0
        matched.save(self.base/'contexts'/(path.parent.name+'-'+path.name+'.json'),d)
        self.state('trace-verified',run=str(path.relative_to(self.base)),scanner_cpus=sorted(cpus),
                   scanner_on_application_cpu_s=d['scanner_on_application_cpu_s'],separation_verified=d['separation_verified'])

    def run(self):
        self.preflight()
        references = {}
        for stage in self.a.stages:
            self.profile_stage = stage
            for entry in self.plan['schedule']:
                cell,mode,block = entry['cell'],entry['mode'],entry['block']
                self.placement = entry['placement']; work = self.plan['cells'][cell]
                name='measured/{}/b{:02d}-{}-{}-{}'.format(stage,block,cell,mode,self.placement)
                result=self.workload(name,'bfs',work['threads'],mode,work['trials'],sustained=True)
                if cell not in references:
                    assert mode=='A' and block==1
                    references[cell]=result['sources']
                    assert result['sources'][:work['pilot_trials']]==work['pilot_sources']
                assert result['sources']==references[cell]
                if stage=='P1': self.check_trace(self.base/name)
            self.state('block-complete',block=stage)
        self.guard(self.base/'host-final.json')
        matched.save(self.base/'campaign-summary.json',dict(passed=True,at=matched.now(),
            source_commit=self.commit,stages=self.a.stages,measured_runs=len(self.summaries),runs=self.summaries))
        self.state('complete',passed=True)
        (self.base/'campaign.exit').write_text('0\n')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base',type=Path,required=True)
    p.add_argument('--source',type=Path,default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--gapbs',type=Path,default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--prior',type=Path,default=Path('/home/yans3/cmdp-v2/periodic/fused-bfs-batch-sweep-20261023-v1'))
    p.add_argument('--reference',type=Path,default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    p.add_argument('--stages',nargs='+',choices=('P0','P1'),default=['P0','P1'])
    p.add_argument('--resume',action='store_true')
    a=p.parse_args()
    a.dirty_batch_pages=a.wp_batch_pages=512
    a.unprofiled_reference=a.scheduling_reference=a.cpu_reference=None
    matched.KERNEL_COMMIT=json.loads(fused.BUILD_RECORD.read_text())['source_freezes']['fixed']['commit']
    with (a.base/'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        def interrupted(signum,frame): raise RuntimeError('Driver interrupted: '+str(signum))
        signal.signal(signal.SIGINT,interrupted);signal.signal(signal.SIGTERM,interrupted)
        campaign=Campaign(a)
        try: campaign.run()
        except BaseException as exc:
            for _,proc,log in campaign.collectors:
                if proc.poll() is None: proc.send_signal(signal.SIGINT)
                proc.wait(timeout=30);log.close()
            campaign.failure(exc)
            raise

if __name__=='__main__': main()
