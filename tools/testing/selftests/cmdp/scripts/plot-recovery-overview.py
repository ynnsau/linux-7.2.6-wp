#!/usr/bin/env python3
"""Draw a compact coverage / cumulative-event overview from validated analysis."""
import argparse
import json
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('analysis',type=Path)
    a=p.parse_args()
    results=json.loads((a.analysis/'analysis.json').read_text())
    plots=json.loads((a.analysis/'plot-data.json').read_text())
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator, FuncFormatter
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,'axes.spines.right':False})
    fig,axes=plt.subplots(3,len(results),figsize=(17,9),sharex='col',sharey='row',constrained_layout=True)
    summaries={}
    for col,(key,r) in enumerate(results.items()):
        plot=plots[key]; rows=plot['status']
        active=[x for x in rows if x['stop_reason']=='none']
        cleanup=[x for x in rows if x['stop_reason']!='none']
        duration=(r['final_status']['now_ns']-r['time_origin']['attach_ns'])/1e9
        mean=r['kernel_protection_integral']['gib_seconds_at_4k']/duration
        summary={'application':r['application'],'threads':r['threads'],
                 'elapsed_seconds':r['application_and_monitor_elapsed_seconds'],
                 'trial_count':r['workload']['trial_count'],'trial_seconds':r['workload']['trial_seconds'],
                 'ARM':r['counts']['ARM'],'WP':r['counts']['WP'],
                 'invalidations':r['counts']['terminals']['invalidate'],
                 'mean_protected_GiB':mean,'mean_denominator_seconds':duration,
                 'peak_sampled_GiB':r['sampled_live']['peak']['active_gib_at_4k'],
                 'protected_GiB_seconds':r['kernel_protection_integral']['gib_seconds_at_4k'],
                 'sweeps':r['sweeps'],'write_helper':r['write_handling']}
        summaries[key]=summary
        axes[0,col].plot([x['seconds_since_attach'] for x in active], [x['active_gib_at_4k'] for x in active],color='#117d76',lw=2)
        axes[0,col].axhline(mean,color='#117d76',ls='--',lw=1,alpha=.75)
        if cleanup:
            axes[0,col].scatter([x['seconds_since_attach'] for x in cleanup],[x['active_gib_at_4k'] for x in cleanup],color='#7f8995',s=24,marker='x',zorder=4)
        axes[0,col].set_title('{} · {} thread{}\n{:.1f} s · {} complete trial{}'.format(r['application'].upper(),r['threads'],'' if r['threads']==1 else 's',r['application_and_monitor_elapsed_seconds'],r['workload']['trial_count'],'' if r['workload']['trial_count']==1 else 's'),fontweight='bold',pad=12)
        for row,field,color,divisor in [(1,'ARM','#2563a3',1e6),(2,'WP','#b54425',1)]:
            axes[row,col].step([x['seconds_since_attach'] for x in rows],[x[field]/divisor for x in rows],where='post',color=color,lw=2)
            axes[row,col].text(.03,.94,'{:,.0f} total'.format(r['counts'][field]),transform=axes[row,col].transAxes,ha='left',va='top',fontsize=10,color=color)
        axes[0,col].text(.98,.94,'Mean {:.2f} GiB'.format(mean),transform=axes[0,col].transAxes,ha='right',va='top',fontsize=10,color='#117d76',bbox={'facecolor':'white','alpha':.85,'edgecolor':'none'})
        for ax in axes[:,col]:
            ax.set_ylim(bottom=0);ax.grid(True,alpha=.18);ax.set_xlim(left=0)
            ax.xaxis.set_major_locator(MaxNLocator(5))
            if plot['stop_seconds'] is not None: ax.axvline(plot['stop_seconds'],color='#8a949f',ls=':',lw=1)
        axes[2,col].set_xlabel('Seconds since monitor attachment')
    axes[0,0].set_ylabel('Live ARM coverage (GiB)')
    axes[1,0].set_ylabel('Cumulative ARM admissions\n(millions of generations)')
    axes[0,0].set_ylim(0, max(r['sampled_live']['peak']['active_gib_at_4k'] for r in results.values()) * 1.12)
    axes[1,0].set_ylim(0, max(r['counts']['ARM'] for r in results.values()) / 1e6 * 1.12)
    max_wp = max(r['counts']['WP'] for r in results.values())
    axes[2,0].set_yscale('symlog', linthresh=1)
    axes[2,0].set_ylim(0, max(1, max_wp) * 2)
    axes[2,0].set_yticks([v for v in (0, 1, 10, 100, 1000, 10000, 100000, 1000000) if v <= max(1,max_wp)*2])
    axes[2,0].yaxis.set_major_formatter(FuncFormatter(lambda v,p: '{:g}k'.format(v/1000) if v >= 1000 else '{:g}'.format(v)))
    axes[2,0].set_ylabel('Cumulative WP events\n(log scale above 1)')
    fig.suptitle('Twitter CC and BC: dirty-qualified ARM with recovery\nQ = 10 s · requested sweep = 10 s · dotted vertical line = STOP · × = cleanup',fontsize=16,fontweight='bold')
    fig.supxlabel('ARM admissions count generations, not unique pages. Dashed green lines show kernel-integrated mean coverage, including startup and cleanup.\nWP counts active ARM generations ended by writes. These runs do not measure end-to-end overhead.',fontsize=10)
    for suffix in ('png','svg','pdf'): fig.savefig(a.analysis/('coverage-events.'+suffix),dpi=180)
    plt.close(fig)
    (a.analysis/'overview-summary.json').write_text(json.dumps(summaries,indent=2)+'\n')
    print(json.dumps(summaries,indent=2))
if __name__=='__main__': main()
