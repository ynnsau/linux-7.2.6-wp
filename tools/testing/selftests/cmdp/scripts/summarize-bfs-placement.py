#!/usr/bin/env python3
"""Matched CPU-placement results; reference timing and scheduling stay separate."""
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
    expected=list(range(0 if place=='shared' else 1,t+(0 if place=='shared' else 1)))
    assert s['application_cpus']==expected and s['controller_cpus']==[0]
    assert s['trials']=={1:38,4:127}[t] and s['dirty_batch_pages']==s['wp_batch_pages']==512
    if t not in source_sequences:source_sequences[t]=s['sources']
    assert s['sources']==source_sequences[t]
    r=dict(stage=stage,threads=t,mode=mode,placement=place,rep=rep,run=str(f.parent),
        elapsed_s=s['elapsed_seconds'],trial_s=s['trial_seconds'],peak_rss_gib=s['target_rusage']['maxrss_kib']/2**20,
        target_cpu_s=(s['target_rusage']['user_us']+s['target_rusage']['system_us'])/1e6,
        arm_exposure_gib_s=s['final'].get('protected_byte_ms',0)/1000/2**30,
        wp_calls=s['progress'].get('write_calls',0),wp_hook_us=s['progress'].get('write_ns',0)/1000,
        samples=s['dirty'].get('samples',0),sweeps=s['completed_sweeps'])
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
        for field in ('scanner_on_cpu_s','scanner_work_elapsed_s','scanner_overlap_target_runnable_s','scanner_on_application_cpu_s','separation_verified'):
            r[field]=d[field]
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
            pairs=[(lookup[stage,t,mode,'shared',rep],lookup[stage,t,mode,'shifted',rep]) for rep in (1,2,3)]
            effects['{}-t{}-{}'.format(stage,t,mode)]=dict(
                overhead_reduction_percentage_points=stats([x['overhead_pct']-y['overhead_pct'] for x,y in pairs]),
                excess_time_reduction_s=stats([x['excess_s']-y['excess_s'] for x,y in pairs]),
                whole_job_time_reduction_s=stats([x['elapsed_s']-y['elapsed_s'] for x,y in pairs]),
                shifted_vs_shared_runtime_pct=stats([100*(y['elapsed_s']/x['elapsed_s']-1) for x,y in pairs]))
save('placement-summary.json',dict(rows=rows,cells=cells,paired_effects=effects,
    notes=['P0 is the ordinary timing reference; P1 is a separate scheduling block.',
           'Three-run ranges are not confidence intervals.',
           'BFS scheduler time is summed thread-seconds, not additive wall-time cost.',
           'Migration may undermine separation; scanner CPU distribution is measured in P1 only.',
           'RSS exposure is approximated by linear interpolation of one-second observations.']))
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})
colors={'shared':'#AD5A43','shifted':'#287A9F'};labels={'shared':'BFS includes CPU 0','shifted':'BFS excludes CPU 0'}
def finish(fig,name):
    fig.savefig(a.output/(name+'.png'),dpi=180,bbox_inches='tight')
    fig.savefig(a.output/(name+'.svg'),bbox_inches='tight');plt.close(fig)
fig,axs=plt.subplots(1,2,figsize=(11,4.5),layout='constrained')
for ax,t in zip(axs,(1,4)):
    all_values=[]
    for j,place in enumerate(('shared','shifted')):
        for i,mode in enumerate('BC'):
            v=cells['P0-t{}-{}-{}'.format(t,mode,place)]['overhead_pct']['values'];x=i+(j-.5)*.34
            ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
            ax.scatter([x-.05,x,x+.05],v,s=22,color='#222',zorder=3)
            all_values+=v
    ax.axhline(0,color='#888',lw=.8);ax.set_xticks([0,1],['Sampling only','Dirty → ARM'])
    ax.set_title('{} BFS thread{}'.format(t,'' if t==1 else 's'));ax.set_ylabel('Overhead vs same-placement off (%)')
    ax.legend(fontsize=9);ax.set_ylim(min(0,min(all_values)-1),max(1,max(all_values)*1.25+1))
fig.suptitle('Does moving BFS off CPU 0 reduce overhead?\nFixed work · three paired repetitions · no added profiling')
finish(fig,'placement-overhead')
fig,axs=plt.subplots(2,2,figsize=(11,8),layout='constrained')
for col,t in enumerate((1,4)):
    for row,(metric,title) in enumerate([('bfs_runnable_s','BFS runnable delay'),('scanner_overlap_target_runnable_s','Scanner on same core while BFS runnable')]):
        ax=axs[row,col]
        for j,place in enumerate(('shared','shifted')):
            for i,mode in enumerate('BC'):
                v=cells['P1-t{}-{}-{}'.format(t,mode,place)][metric]['values'];x=i+(j-.5)*.34
                ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
                ax.scatter([x-.05,x,x+.05],v,s=22,color='#222',zorder=3)
        ax.set_xticks([0,1],['Sampling only','Dirty → ARM']);ax.set_title('{} · {} thread{}'.format(title,t,'' if t==1 else 's'))
        ax.set_ylabel('Seconds (summed across BFS threads)');ax.legend(fontsize=8)
fig.suptitle('Scheduling evidence from separate traced runs\nShows actual overlap, including scanner migration')
finish(fig,'placement-scheduling')
fig,axs=plt.subplots(1,2,figsize=(11,4.6),layout='constrained')
for ax,t in zip(axs,(1,4)):
    selected=[r for r in rows if r['stage']=='P1' and r['threads']==t and r['mode']!='A']
    selected.sort(key=lambda r:(r['mode'],r['placement'],r['rep']))
    for i,r in enumerate(selected):
        app=r['scanner_on_application_cpu_s'];other0=r['scanner_cpu_distribution_s'].get('0',0) if r['placement']=='shifted' else 0
        other=max(0,r['scanner_on_cpu_s']-app-other0)
        left=0
        for value,color,label in [(app,'#AD5A43','On BFS CPUs'),(other0,'#287A9F','CPU 0, off BFS'),(other,'#8D99AE','Other CPUs')]:
            ax.barh(i,value,left=left,color=color,label=label if i==0 else None);left+=value
    ax.set_yticks(range(len(selected)),['{} {} r{}'.format(r['mode'],r['placement'],r['rep']) for r in selected])
    ax.invert_yaxis();ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'));ax.set_xlabel('Scanner scheduled CPU seconds');ax.legend(fontsize=8)
fig.suptitle('Where the scanner actually ran\nB = sampling; C = dirty→ARM · every traced repetition shown')
finish(fig,'placement-scanner-cpus')
fig,axs=plt.subplots(1,2,figsize=(10,4.2),layout='constrained')
for ax,metric,title in zip(axs,('mean_live_arm_gib','arm_rss_exposure_pct'),('Mean live ARM (GiB)','ARM / RSS exposure (%)')):
    for j,place in enumerate(('shared','shifted')):
        for i,t in enumerate((1,4)):
            v=cells['P0-t{}-C-{}'.format(t,place)][metric]['values'];x=i+(j-.5)*.34
            ax.bar(x,statistics.median(v),width=.3,color=colors[place],label=labels[place] if i==0 else None)
            ax.scatter([x-.05,x,x+.05],v,color='#222',s=22,zorder=3)
    ax.set_xticks([0,1],['1 thread','4 threads']);ax.set_ylabel(title);ax.legend(fontsize=8)
fig.suptitle('Keep protection coverage visible alongside runtime\nDirty→ARM, ordinary timing runs · RSS exposure is approximate')
finish(fig,'placement-coverage')
print(json.dumps(dict(passed=True,runs=len(rows),cells=len(cells),paired_effects=effects),indent=2))
