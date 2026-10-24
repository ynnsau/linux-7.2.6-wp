#!/usr/bin/env python3
"""Summarize independently collected attribution stages; no causal pie chart."""
import argparse,json,statistics
from collections import Counter,defaultdict
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

p=argparse.ArgumentParser();p.add_argument('contexts',type=Path);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(exist_ok=True,parents=True)
data=[json.loads(x.read_text()) for x in sorted(a.contexts.glob('P*.json'))]
labels={'A':'Off','B':'Sampling','C':'Dirty → ARM'};colors={'A':'#8D99AE','B':'#277DA1','C':'#D17B0F'}
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})

def median(values):return statistics.median(values)
def stats(values):return dict(values=values,median=median(values),min=min(values),max=max(values))
def savefig(fig,name):
 fig.savefig(a.output/(name+'.png'),dpi=180,bbox_inches='tight')
 fig.savefig(a.output/(name+'.svg'),bbox_inches='tight');plt.close(fig)

rows=[]
for d in data:
 r={k:d[k] for k in ('profile_stage','threads','mode','elapsed_seconds')}
 r['run']=d['run'];r['rep']=int(Path(d['run']).name[1:3])
 r['target_cpu_s']=(d['rusage']['user_us']+d['rusage']['system_us'])/1e6
 receipt=json.loads((Path(d['run'])/'summary.json').read_text())
 diag=(receipt.get('diagnostics') or {}).get('invalidation_sources',{})
 progress=receipt.get('progress') or {};final=receipt.get('final') or {}
 r['wp_calls']=progress.get('write_calls',0);r['wp_us']=progress.get('write_ns',0)/1000
 r['arm_episodes']=final.get('first_arms',0)+final.get('rearms',0)
 r['mean_live_arm_gib']=final.get('protected_byte_ms',0)/1000/2**30/r['elapsed_seconds']
 r['numa_revoked_gib']=diag.get('numa',{}).get('arm_revoked',0)*4096/2**30
 if d['profile_stage']!='P0':
  for i,name in enumerate(['unknown','numa','protection','mprotect','mremap','unmap','clear','test']):
   v=[x for k,x in d['invalidations'].items() if k.endswith('-source'+str(i))]
   r['callback_'+name+'_elapsed_s']=sum(x['elapsed_s'] for x in v)
   r['callback_'+name+'_revoked']=sum(x['revoked'] for x in v)
 if d['profile_stage']=='P1':
  for field,label in [('on_cpu_s','on_cpu_s'),('runnable','runnable_s'),('blocked','blocked_s'),('blocked:mmap/rwsem','mmap_blocked_s'),('blocked:futex/barrier','futex_blocked_s')]:
   r[label]=sum(v.get(field,0) for v in d['target_scheduling'].values())
  for f in ('scanner_on_cpu_s','scanner_overlap_target_runnable_s','scanner_work_elapsed_s'):r[f]=d[f]
  for kind in ('runnable','blocked'):r['scanner_'+kind+'_s']=d['scanner_waits_s'].get(kind,0)
  r['scanner_mmap_wait_s']=sum(v['wait_s'] for k,v in d['mmap'].items() if k.startswith('scanner-'))
  r['scanner_mmap_hold_s']=sum(v['hold_s'] for k,v in d['mmap'].items() if k.startswith('scanner-'))
  r['scanner_items']=d['scanner_work_items'];r['anomalies']=d['anomalies']
  r['unclosed_scanner_items']=d['unclosed_scanner_items']
 if d['profile_stage']=='P2':
  assert d['sample_weight_unit']=='cycles', 'Retained P2 must use hardware cycles'
  for context in ('bfs-user','bfs-kernel','scanner','other'):
   r['sampled_'+context+'_cycles']=d['sampled_context_cycles'].get(context,0)
 if d['profile_stage']=='P3':
  g=d['pmu']['groups']['application_cores'];ins=g['instructions']
  r['app_core_CPI']=g['cycles']/ins
  for event,name in [('cache-misses','cache_misses'),('dtlb_load_misses.walk_completed','load_walks'),('dtlb_store_misses.walk_completed','store_walks')]:
   r['app_core_'+name+'_per_ki']=1000*g[event]/ins;r['app_core_'+name]=g[event]
  r['app_core_instructions']=ins;r['pmu_min_running_pct']=d['pmu']['min_running_percent']
  flushes=[(int(k.split('|')[3][5:]),v) for k,v in d['tlb_page_histogram'].items() if int(k.split('|')[1][3:])<d['threads'] and k.split('|')[2] in ('flush on task switch','remote shootdown','local shootdown','local MM shootdown')]
  r['app_core_full_tlb_flushes']=sum(v for pages,v in flushes if pages==-1)
  r['app_core_range_tlb_flushes']=sum(v for pages,v in flushes if pages>0)
  for reason in ('remote shootdown','remote IPI send','local shootdown','local MM shootdown','remote wrong CPU'):
   r['app_core_tlb_'+reason]=sum(v for k,v in d['tlb_events'].items() if int(k.split('|')[1][3:])<d['threads'] and k.split('|')[2]==reason)
   r['scanner_tlb_'+reason]=sum(v for k,v in d['tlb_events'].items() if k.startswith('scanner|') and k.split('|')[2]==reason)
 rows.append(r)

cells={};lookup={(r['profile_stage'],r['threads'],r['mode'],r['rep']):r for r in rows}
for stage in ('P1','P2','P3'):
 for t in (1,4):
  for mode in 'ABC':
   group=[r for r in rows if (r['profile_stage'],r['threads'],r['mode'])==(stage,t,mode)]
   if not group:continue
   cell=dict(n=len(group))
   keys=[k for k,v in group[0].items() if isinstance(v,(int,float)) and k not in ('rep','threads')]
   for k in keys:cell[k]=stats([r[k] for r in group])
   paired=[(r,lookup.get((stage,t,'A',r['rep']))) for r in group]
   paired=[(r,ref) for r,ref in paired if ref]
   cell['paired_delta_vs_off']={k:stats([r[k]-ref[k] for r,ref in paired]) for k in keys} if paired else {}
   cells['{}-t{}-{}'.format(stage,t,mode)]=cell

functions={}
for t in (1,4):
 for mode in 'BC':
  group=[d for d in data if (d['profile_stage'],d['threads'],d['mode'])==('P2',t,mode)]
  if not group:continue
  for context in ('scanner','bfs-kernel'):
   sums=Counter();inc=Counter()
   for d in group:
    sums.update(dict(d['leaf_cycles'].get(context,[])));inc.update(dict(d['inclusive_cycles'].get(context,[])))
   functions['t{}-{}-{}'.format(t,mode,context)]=dict(n=len(group),leaf_mean_cycles=[(k,v/len(group)) for k,v in sums.most_common()],inclusive_mean_cycles=[(k,v/len(group)) for k,v in inc.most_common()],leaf_cycle_percent=[(k,100*v/sum(d['sampled_context_cycles'].get(context,0) for d in group)) for k,v in sums.most_common()])
(a.output/'attribution-summary.json').write_text(json.dumps(dict(rows=rows,cells=cells,functions=functions),indent=2)+'\n')

if all(cells.get('P1-t{}-{}'.format(t,m),{}).get('n')==3 for t in (1,4) for m in 'ABC'):
 fig,axs=plt.subplots(2,2,figsize=(12,8),layout='constrained')
 for col,t in enumerate((1,4)):
  ax=axs[0,col]
  for i,m in enumerate('ABC'):
   c=cells['P1-t{}-{}'.format(t,m)]
   for j,(key,color) in enumerate([('runnable_s','#7B61A8'),('blocked_s','#C44E52')]):
    v=c[key]['values'];x=i+(j-.5)*.3
    ax.bar(x,median(v),width=.27,color=color,label=['Runnable delay','Blocked time'][j] if i==0 else None)
    ax.scatter([x-.035,x,x+.035],v,color='#222',s=16,zorder=3)
  ax.set_xticks(range(3),[labels[m] for m in 'ABC']);ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'))
  ax.set_ylabel('Summed BFS thread-seconds');ax.legend(fontsize=9)
  ax=axs[1,col]
  for i,m in enumerate('BC'):
   c=cells['P1-t{}-{}'.format(t,m)]
   for j,(key,color) in enumerate([('scanner_on_cpu_s','#277DA1'),('scanner_overlap_target_runnable_s','#58A681')]):
    v=c[key]['values'];x=i+(j-.5)*.3
    ax.bar(x,median(v),width=.27,color=color,label=['Scanner on CPU','Same core; BFS runnable'][j] if i==0 else None)
    ax.scatter([x-.035,x,x+.035],v,color='#222',s=16,zorder=3)
  ax.set_xticks(range(2),[labels[m] for m in 'BC']);ax.set_ylabel('Seconds');ax.legend(fontsize=9)
 fig.suptitle('Where BFS waits, and when the scanner competes for its CPU\nScheduling profiles · medians and three repetitions',fontsize=14)
 savefig(fig,'scheduling-attribution')
 # Fixed representative: repetition two, chosen before seeing its results.
 group={d['mode']:d for d in data if d['profile_stage']=='P1' and d['threads']==1 and Path(d['run']).name.startswith('b02-')}
 fig,axs=plt.subplots(3,1,figsize=(12,6.4),sharex=True,layout='constrained')
 for ax,m in zip(axs,'ABC'):
  d=group[m];tl=d['scheduling_timeline']
  run=[(lo,hi-lo) for v in tl['bfs_running'].values() for lo,hi,cpu in v]
  ax.broken_barh(run,(1,.65),facecolors='#D9E6EE')
  for kind,color in [('runnable','#7B61A8'),('blocked','#C44E52')]:
   spans=[(lo,hi-lo) for v in tl['bfs_waits'].values() for lo,hi,k,s,cpu in v if k==kind]
   ax.broken_barh(spans,(1,.65),facecolors=color)
  ax.broken_barh([(lo,hi-lo) for lo,hi,cpu,tid in tl['scanner']],(0,.65),facecolors='#277DA1')
  ax.set_yticks([.325,1.325],['Scanner','BFS']);ax.set_title(labels[m],loc='left');ax.set_ylim(-.2,1.9)
  ax.axvline(d['elapsed_seconds'],color='#777',linestyle=':',lw=1)
 axs[-1].set_xlabel('Seconds since controller fork (complete job)')
 fig.suptitle('One-thread timeline · fixed representative repetition 2\nBFS: pale = running, purple = runnable wait, red = blocked',fontsize=13)
 savefig(fig,'scheduling-timeline')

if all(cells.get('P2-t{}-{}'.format(t,m),{}).get('n')==3 for t in (1,4) for m in 'ABC'):
 fig,axs=plt.subplots(2,2,figsize=(13,8.5),layout='constrained')
 for row,mode in enumerate('BC'):
  for col,t in enumerate((1,4)):
   ax=axs[row,col];f=functions['t{}-{}-scanner'.format(t,mode)]['leaf_cycle_percent'][:8]
   ax.barh(range(len(f)),[v for k,v in f],color=colors[mode]);ax.set_yticks(range(len(f)),[k for k,v in f],fontsize=9);ax.invert_yaxis()
   ax.set_xlabel('Share of scanner cycle sample weight (%)');ax.set_title('{} · {} thread{}'.format(labels[mode],t,'' if t==1 else 's'))
 fig.suptitle('Scanner CPU hotspots\nHardware-cycle leaf samples · approximately 99 Hz · three jobs',fontsize=14)
 savefig(fig,'scanner-hotspots')

if all(cells.get('P3-t{}-{}'.format(t,m),{}).get('n')==3 for t in (1,4) for m in 'ABC'):
 fig,axs=plt.subplots(2,3,figsize=(13,7),layout='constrained')
 fields=[('app_core_cache_misses','Cache-miss events (millions)',1e6),('app_core_load_walks','Completed load page walks (millions)',1e6),('app_core_full_tlb_flushes','Full-context TLB flush events',1)]
 for row,t in enumerate((1,4)):
  for col,(key,label,scale) in enumerate(fields):
   ax=axs[row,col]
   for i,m in enumerate('ABC'):
    v=[x/scale for x in cells['P3-t{}-{}'.format(t,m)][key]['values']];ax.bar(i,median(v),color=colors[m],width=.55);ax.scatter([i-.06,i,i+.06],v,s=20,color='#222',zorder=3)
    if m!='A':
     group=[r for r in rows if (r['profile_stage'],r['threads'],r['mode'])==('P3',t,m)]
     paired=[100*(r[key]/lookup['P3',t,'A',r['rep']][key]-1) for r in group]
     ymax=max(cells['P3-t{}-{}'.format(t,mode)][key]['max']/scale for mode in 'ABC')
     ax.text(i,max(v)+.035*ymax,'{:+.1f}%'.format(median(paired)),ha='center',fontsize=10)
     ax.set_ylim(0,1.20*ymax)
   ax.set_xticks(range(3),[labels[m] for m in 'ABC']);ax.set_ylabel(label);ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'))
 fig.suptitle('Translation and cache activity on the BFS cores\nPer-CPU totals include scanner/background work · medians, three runs; labels = paired change vs off',fontsize=13)
 savefig(fig,'tlb-cache-evidence')

if len(rows)==72:
 fig,axs=plt.subplots(1,2,figsize=(11,4.8),layout='constrained')
 stagecolors={'P0':'#666666','P1':'#8B5FBF','P2':'#1B9E77','P3':'#D95F02'}
 for ax,t in zip(axs,(1,4)):
  for stage in ('P0','P1','P2','P3'):
   group=[r for r in rows if (r['profile_stage'],r['threads'],r['mode'])==(stage,t,'C')]
   x=[r['numa_revoked_gib'] for r in group]
   y=[100*(r['elapsed_seconds']/lookup[stage,t,'A',r['rep']]['elapsed_seconds']-1) for r in group]
   ax.scatter(x,y,label=stage,color=stagecolors[stage],s=55,alpha=.8,edgecolors='white',linewidth=.5)
  ax.set_title('{} thread{}'.format(t,'' if t==1 else 's'))
  ax.set_xlabel('Cumulative NUMA-triggered ARM revocation (GiB)')
  ax.set_ylabel('Dirty → ARM overhead vs matched off (%)');ax.legend(title='Collection stage',fontsize=9)
 fig.suptitle('NUMA revocation and runtime variation\nAssociation across separate collection stages; repeated page revocations count again',fontsize=13)
 savefig(fig,'numa-runtime-variation')

print('Summarized {} runs; {} attribution cells'.format(len(rows),len(cells)))
