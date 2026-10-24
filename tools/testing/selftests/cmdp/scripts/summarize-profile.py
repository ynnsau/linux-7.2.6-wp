#!/usr/bin/env python3
import argparse,json,statistics
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

p=argparse.ArgumentParser();p.add_argument('base',type=Path,nargs='*');p.add_argument('--stage-base',nargs=2,action='append',default=[]);p.add_argument('--analysis',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(exist_ok=True,parents=True)
rows=[]
receipts=[p for base in a.base for p in (base/'measured').glob('*/*/summary.json')]
receipts += [p for stage,base in a.stage_base for p in (Path(base)/'measured'/stage).glob('*/summary.json')]
assert len(receipts)==len(set(p.parent.parent.name+'-'+p.parent.name for p in receipts)), 'Duplicate run keys'
for f in sorted(receipts):
 r=json.loads(f.read_text());r['block']=int(f.parent.name[1:3]);r['path']=str(f.parent)
 r['mean_arm_gib']=r['final'].get('protected_byte_ms',0)/1000/2**30/r['elapsed_seconds']
 command=json.loads((f.parent/'command.json').read_text())
 begin,end=command['launcher_begin_ns'],command['launcher_end_ns'];death=r['target_rusage']['exit_observed_ns']
 points=[(begin,0),(min(end,death),0),(end,0)]
 for line in (f.parent/'memory-samples.jsonl').read_text().splitlines():
  sample=json.loads(line)
  if 'memory_kib' not in sample:continue
  ts=(sample['read_begin_ns']+sample['read_end_ns'])//2
  if begin<=ts<=min(end,death):points.append((ts,sample['memory_kib']['VmRSS']*1024))
 points=sorted(points);rss_byte_s=sum((b[0]-a0[0])/1e9*(a0[1]+b[1])/2 for a0,b in zip(points,points[1:]))
 r['rss_exposure_gib_s']=rss_byte_s/2**30
 r['arm_exposure_gib_s']=r['final'].get('protected_byte_ms',0)/1000/2**30
 r['arm_rss_exposure_pct']=100*r['arm_exposure_gib_s']/r['rss_exposure_gib_s']
 assert r['rss_exposure_gib_s']/r['elapsed_seconds'] <= r['target_rusage']['maxrss_kib']/2**20*1.01
 assert 0<=r['arm_rss_exposure_pct']<=101
 r['target_cpu_s']=(r['target_rusage']['user_us']+r['target_rusage']['system_us'])/1e6
 r['peak_rss_gib']=r['target_rusage']['maxrss_kib']/2**20
 rows.append(r)
lookup={(r['profile_stage'],r['threads'],r['block'],r['mode']):r for r in rows}
result={}
for stage in ('P0','P1','P2','P3'):
 result[stage]={}
 for t in (1,4):
  cells={}
  for mode in 'ABC':
   runs=[r for r in rows if (r['profile_stage'],r['threads'],r['mode'])==(stage,t,mode)]
   if not runs:continue
   cell=dict(n=len(runs),elapsed_s=[r['elapsed_seconds'] for r in runs],elapsed_median_s=statistics.median(r['elapsed_seconds'] for r in runs),
     target_cpu_s=[r['target_cpu_s'] for r in runs],peak_rss_gib=[r['peak_rss_gib'] for r in runs],
     mean_arm_gib=[r['mean_arm_gib'] for r in runs],arm_exposure_gib_s=[r['arm_exposure_gib_s'] for r in runs],
     rss_exposure_gib_s=[r['rss_exposure_gib_s'] for r in runs],arm_rss_exposure_pct=[r['arm_rss_exposure_pct'] for r in runs],
     rss_exposure_method='Linear interpolation of 1s /proc RSS observations; approximate; zero at launch and observed target exit')
   pairs=[100*(r['elapsed_seconds']/lookup[stage,t,r['block'],'A']['elapsed_seconds']-1) for r in runs if (stage,t,r['block'],'A') in lookup]
   if pairs:cell.update(paired_vs_off_pct=pairs,paired_vs_off_median_pct=statistics.median(pairs))
   if stage!='P0':
    shifts=[100*(r['elapsed_seconds']/lookup['P0',t,r['block'],mode]['elapsed_seconds']-1) for r in runs if ('P0',t,r['block'],mode) in lookup]
    cell['stage_vs_P0_pct']=shifts
   cells[mode]=cell
  result[stage][str(t)]=cells
(a.output/'runtime-summary.json').write_text(json.dumps(result,indent=2)+'\n')
if all(len(result['P0'][str(t)].get(m,{}).get('elapsed_s',[]))==3 for t in (1,4) for m in 'ABC'):
 plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})
 fig,axs=plt.subplots(1,2,figsize=(10,4.5),layout='constrained')
 colors=['#277DA1','#D17B0F']
 for j,t in enumerate((1,4)):
  ax=axs[j]
  for x,mode in enumerate('BC'):
   values=result['P0'][str(t)][mode]['paired_vs_off_pct'];med=statistics.median(values)
   ax.bar(x,med,color=colors[x],width=.55)
   ax.scatter([x-.08,x,x+.08],values,s=35,color='#222222',zorder=3)
   ax.text(x,max(values)+.25,'{:.2f}%'.format(med),ha='center',weight='bold')
  ax.set_xticks([0,1],['Sampling only','Dirty → ARM']);ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'))
  ax.set_ylabel('Complete-job overhead vs matched off (%)');ax.axhline(0,color='#777',lw=.7)
  ax.set_ylim(0,max(12,1.2*max(result['P0'][str(t)][m]['paired_vs_off_pct'][i] for m in 'BC' for i in range(3))))
 fig.suptitle('BFS overhead without added profiling\nFixed work · 512/512 batch limits · 3 paired repetitions',fontsize=14)
 fig.savefig(a.output/'reference-overhead.png',dpi=180);fig.savefig(a.output/'reference-overhead.svg');plt.close(fig)
print(json.dumps(result,indent=2))
