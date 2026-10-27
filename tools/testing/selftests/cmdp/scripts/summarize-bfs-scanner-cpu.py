#!/usr/bin/env python3
"""Fixed BFS, explicit scanner CPU comparison; separate timing and scheduling blocks."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('base',type=Path);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
def stats(v):return dict(values=v,median=statistics.median(v),min=min(v),max=max(v))
def save(name,obj):(a.output/name).write_text(json.dumps(obj,indent=2)+'\n')
rows=[];lookup={};source_sequences={}
for f in sorted((a.base/'measured').glob('*/*/summary.json')):
    s=json.loads(f.read_text());assert s['pass']
    stage,t,mode,place=s['profile_stage'],s['threads'],s['mode'],s['placement']
    rep=int(f.parent.name[1:3]);key=(stage,t,mode,place,rep);assert key not in lookup
    expected=list(range(1,t+1))
    assert s['application_cpus']==expected and s['controller_cpus']==[5]
    assert s['scanner_cpu']==(None if mode=='A' else (1 if place=='shared' else 0))
    if mode!='A':
        assert s['cpu']['work_items']>0 and s['cpu']['cpu']==s['scanner_cpu']
        assert s['cpu']['error']==s['cpu']['offline_events']==s['cpu']['wrong_cpu_work_items']==0
    assert s['trials']=={1:38,4:127}[t] and s['dirty_batch_pages']==s['wp_batch_pages']==512
    if t not in source_sequences:source_sequences[t]=s['sources']
    assert s['sources']==source_sequences[t]
    r=dict(stage=stage,threads=t,mode=mode,placement=place,rep=rep,run=str(f.parent),
        elapsed_s=s['elapsed_seconds'],trial_s=s['trial_seconds'],peak_rss_gib=s['target_rusage']['maxrss_kib']/2**20,
        target_cpu_s=(s['target_rusage']['user_us']+s['target_rusage']['system_us'])/1e6,
        arm_exposure_gib_s=s['final'].get('protected_byte_ms',0)/1000/2**30,
        wp_calls=s['progress'].get('write_calls',0),wp_hook_us=s['progress'].get('write_ns',0)/1000,
        samples=s['dirty'].get('samples',0),sweeps=s['completed_sweeps'],
        scanner_work_items=s['cpu'].get('work_items',0),
        flush_calls=s['batch'].get('flush_calls',0),
        flush_s=s['batch'].get('flush_ns',0)/1e9,
        dirty_reset_pages=s['batch'].get('dirty_pages',0),
        armed_batch_pages=s['batch'].get('wp_pages',0),
        shared_flush_calls=s['batch'].get('shared_flush_calls',0),
        region_skipped_pages=s['batch'].get('region_skip_pages',0),
        pmd_walks=s['batch'].get('pmd_walks',0),
        pmd_cache_hits=s['batch'].get('pmd_cache_hits',0),
        scan_lock_wait_s=s['progress'].get('scan_lock_wait_total_ns',0)/1e9,
        range_callback_s=s['progress'].get('range_ns',0)/1e9,
        history_payload_mib=s['dirty'].get('metadata_bytes',0)/2**20)
    r['mean_live_arm_gib']=r['arm_exposure_gib_s']/r['elapsed_s']
    command=json.loads((f.parent/'command.json').read_text())
    begin,end=command['launcher_begin_ns'],command['launcher_end_ns'];death=s['target_rusage']['exit_observed_ns']
    points=[(begin,0),(min(end,death),0),(end,0)]
    for line in (f.parent/'memory-samples.jsonl').read_text().splitlines():
        sample=json.loads(line)
        if 'memory_kib' not in sample:continue
        ts=(sample['read_begin_ns']+sample['read_end_ns'])//2
        if begin<=ts<=min(end,death):points.append((ts,sample['memory_kib']['VmRSS']*1024))
    points.sort();r['rss_exposure_gib_s']=sum((y[0]-x[0])/1e9*(x[1]+y[1])/2/2**30 for x,y in zip(points,points[1:]))
    r['arm_rss_exposure_pct']=100*r['arm_exposure_gib_s']/r['rss_exposure_gib_s']
    if stage=='P1':
        d=json.loads((a.base/'contexts'/('P1-'+f.parent.name+'.json')).read_text())
        assert not d['anomalies'] and not d['unclosed_scanner_items'] and d['raw_record_quality']['pass_']
        assert d['explicit_placement_verified'] and d['scanner_work_items']==r['scanner_work_items']
        for field in ('scanner_on_cpu_s','scanner_work_elapsed_s','scanner_overlap_target_runnable_s','scanner_on_application_cpu_s','separation_verified'):
            r[field]=d[field]
        r['scanner_runnable_s']=d['scanner_waits_s'].get('runnable',0)
        r['scanner_blocked_s']=d['scanner_waits_s'].get('blocked',0)
        for name in ('mmap/rwsem','VMA reader exclusion','scanner retry sleep','LRU drain completion'):
            r['scanner_blocked_'+name+'_s']=d['scanner_waits_s'].get('blocked:'+name,0)
        r['scanner_cpu_distribution_s']=d['scanner_cpu_distribution_s']
        r['scanner_node1_s']=sum(v for k,v in d['scanner_cpu_distribution_s'].items() if int(k)>=26)
        for field,label in [('on_cpu_s','bfs_on_cpu_s'),('runnable','bfs_runnable_s'),('blocked','bfs_blocked_s'),('blocked:mmap/rwsem','bfs_mmap_blocked_s')]:
            r[label]=sum(v.get(field,0) for v in d['target_scheduling'].values())
    lookup[key]=r;rows.append(r)
assert len(rows)==72,'Require complete campaign before final report'
counts=Counter((r['stage'],r['threads'],r['mode'],r['placement']) for r in rows)
assert len(counts)==24 and set(counts.values())=={3}
for r in rows:
    off=lookup[r['stage'],r['threads'],'A',r['placement'],r['rep']]
    r['overhead_pct']=100*(r['elapsed_s']/off['elapsed_s']-1)
    r['excess_s']=r['elapsed_s']-off['elapsed_s']
cells={}
for stage,t,mode,place in counts:
    runs=[r for r in rows if (r['stage'],r['threads'],r['mode'],r['placement'])==(stage,t,mode,place)]
    fields=[k for k,v in runs[0].items() if isinstance(v,(int,float)) and not isinstance(v,bool) and k not in ('rep','threads')]
    cells['{}-t{}-{}-{}'.format(stage,t,mode,place)]={k:stats([r[k] for r in runs]) for k in fields}
effects={}
for stage in ('P0','P1'):
    for t in (1,4):
        for mode in 'ABC':
            pairs=[(lookup[stage,t,mode,'shared',rep],lookup[stage,t,mode,'separate',rep]) for rep in (1,2,3)]
            effects['{}-t{}-{}'.format(stage,t,mode)]=dict(
                overhead_reduction_percentage_points=stats([x['overhead_pct']-y['overhead_pct'] for x,y in pairs]),
                excess_time_reduction_s=stats([x['excess_s']-y['excess_s'] for x,y in pairs]),
                whole_job_time_reduction_s=stats([x['elapsed_s']-y['elapsed_s'] for x,y in pairs]),
                separate_vs_shared_runtime_pct=stats([100*(y['elapsed_s']/x['elapsed_s']-1) for x,y in pairs]))
save('scanner-cpu-summary.json',dict(rows=rows,cells=cells,paired_effects=effects,
    notes=['P0 is the ordinary timing reference; P1 is a separate scheduling block.',
           'Three-run ranges are not confidence intervals.',
           'BFS scheduler time is summed thread-seconds, not additive wall-time cost.',
           'CPU entry counters verify the selector in P0/P1; P1 independently verifies all scheduled scanner intervals.',
           'RSS exposure is approximated by linear interpolation of one-second observations.']))
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})
colors={'shared':'#AD5A43','separate':'#287A9F'};labels={'shared':'Scanner CPU1 (shared)','separate':'Scanner CPU0 (separate)'}
def finish(fig,name):
    fig.savefig(a.output/(name+'.png'),dpi=180,bbox_inches='tight')
    fig.savefig(a.output/(name+'.svg'),bbox_inches='tight');plt.close(fig)
fig,axs=plt.subplots(1,2,figsize=(11,4.5),sharey=True,layout='constrained')
all_overhead=[r['overhead_pct'] for r in rows if r['stage']=='P0' and r['mode']!='A']
for ax,t in zip(axs,(1,4)):
    all_values=[]
    for j,place in enumerate(('shared','separate')):
        for i,mode in enumerate('BC'):
            v=cells['P0-t{}-{}-{}'.format(t,mode,place)]['overhead_pct']['values'];x=i+(j-.5)*.34
            ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
            ax.scatter([x-.05,x,x+.05],v,s=22,color='#222',zorder=3)
            all_values+=v
    ax.axhline(0,color='#888',lw=.8);ax.set_xticks([0,1],['Sampling only','Dirty → ARM'])
    ax.set_title('{} BFS thread{}'.format(t,'' if t==1 else 's'));ax.set_ylabel('Overhead vs same-placement off (%)')
    ax.legend(fontsize=9);ax.set_ylim(min(0,min(all_overhead)-1),max(1,max(all_overhead)*1.25+1))
fig.suptitle('Separating the scanner reduces BFS overhead\nFixed work · three paired repetitions · no added profiling')
finish(fig,'scanner-cpu-overhead')
fig,axs=plt.subplots(2,2,figsize=(11,8),sharey='row',layout='constrained')
for col,t in enumerate((1,4)):
    for row,(metric,title) in enumerate([('bfs_runnable_s','BFS runnable delay'),('scanner_overlap_target_runnable_s','Scanner on same core while BFS runnable')]):
        ax=axs[row,col]
        for j,place in enumerate(('shared','separate')):
            for i,mode in enumerate('BC'):
                v=cells['P1-t{}-{}-{}'.format(t,mode,place)][metric]['values'];x=i+(j-.5)*.34
                ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
                ax.scatter([x-.05,x,x+.05],v,s=22,color='#222',zorder=3)
        ax.set_xticks([0,1],['Sampling only','Dirty → ARM']);ax.set_title('{} · {} thread{}'.format(title,t,'' if t==1 else 's'))
        ax.set_ylabel('Seconds (summed across BFS threads)');ax.legend(fontsize=8)
fig.suptitle('Scheduling evidence from separate traced runs\nBFS fixed on CPU1 / CPU1–4 · scanner binding independently verified')
finish(fig,'scanner-cpu-scheduling')
fig,axs=plt.subplots(1,2,figsize=(11,4.6),sharex=True,layout='constrained')
for ax,t in zip(axs,(1,4)):
    selected=[r for r in rows if r['stage']=='P1' and r['threads']==t and r['mode']!='A']
    selected.sort(key=lambda r:(r['mode'],r['placement'],r['rep']))
    for i,r in enumerate(selected):
        app=r['scanner_on_application_cpu_s'];other0=r['scanner_cpu_distribution_s'].get('0',0) if r['placement']=='separate' else 0
        other=max(0,r['scanner_on_cpu_s']-app-other0)
        left=0
        for value,color,label in [(app,'#AD5A43','On BFS CPUs'),(other0,'#287A9F','CPU 0, off BFS'),(other,'#8D99AE','Other CPUs')]:
            ax.barh(i,value,left=left,color=color,label=label if i==0 else None);left+=value
    ax.set_yticks(range(len(selected)),['{} {} r{}'.format(r['mode'],r['placement'],r['rep']) for r in selected])
    ax.invert_yaxis();ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'));ax.set_xlabel('Scanner scheduled CPU seconds');ax.legend(fontsize=8)
fig.suptitle('Where the scanner actually ran\nB = sampling; C = dirty→ARM · every traced repetition shown')
finish(fig,'scanner-cpu-scanner-cpus')
fig,axs=plt.subplots(1,2,figsize=(10,4.2),layout='constrained')
for ax,metric,title in zip(axs,('mean_live_arm_gib','arm_rss_exposure_pct'),('Mean live ARM (GiB)','ARM / RSS exposure (%)')):
    for j,place in enumerate(('shared','separate')):
        for i,t in enumerate((1,4)):
            v=cells['P0-t{}-C-{}'.format(t,place)][metric]['values'];x=i+(j-.5)*.34
            ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
            ax.scatter([x-.05,x,x+.05],v,color='#222',s=22,zorder=3)
    ax.set_xticks([0,1],['1 thread','4 threads']);ax.set_ylabel(title);ax.legend(fontsize=8)
fig.suptitle('Substantial ARM coverage remains with a separate scanner core\nDirty→ARM, ordinary timing runs · RSS exposure is approximate')
finish(fig,'scanner-cpu-coverage')
print(json.dumps(dict(passed=True,runs=len(rows),cells=len(cells))))
