import importlib.util,json,gzip,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('a',str(Path(__file__).with_name('analyze-bfs-overhead-profile.py')));a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)/'measured/P1/test';p.mkdir(parents=True)
 (p/'summary.json').write_text(json.dumps(dict(profile_stage='P1',threads=1,mode='B',elapsed_seconds=10,target_rusage={})))
 (p/'command.json').write_text(json.dumps(dict(launcher_begin_ns=110*10**9,launcher_end_ns=120*10**9,controller_pid=50)))
 (p/'monitor.jsonl').write_text(json.dumps(dict(event='controller',pid=100))+'\n')
 (p/'memory-samples.jsonl').write_text(json.dumps(dict(exe='/bfs',threads=[dict(tid=100,cpu_allowed=[0])]))+'\n')
 trace=[]
 def ev(tid,ts,event,payload,frames=''):
  trace.append('task {} [000] {:.9f}: {}: {}\n{}\n'.format(tid,ts,event,payload,frames))
 def switch(ts,prev,pc,state,nxt,nc,frames=''):
  ev(prev,ts,'sched:sched_switch','prev_comm={} prev_pid={} prev_prio=120 prev_state={} ==> next_comm={} next_pid={} next_prio=120'.format(pc,prev,state,nc,nxt),frames)
 switch(10,0,'swapper','R',100,'bfs')
 switch(12,100,'bfs','R',200,'kworker')
 ev(200,12,'workqueue:workqueue_execute_start','work struct abc: function cmdp_monitor_work')
 ev(200,14,'workqueue:workqueue_execute_end','work struct abc: function cmdp_monitor_work')
 switch(14,200,'kworker','I',100,'bfs')
 switch(16,100,'bfs','D',0,'swapper','\tffffffff001 rwsem_down_read_slowpath+0x1 (vmlinux)')
 ev(0,17,'sched:sched_wakeup','comm=bfs pid=100 prio=120 target_cpu=000')
 switch(18,0,'swapper','R',100,'bfs')
 switch(20,100,'bfs','X',0,'swapper')
 trace[-1]=trace[-1].replace('task 100 [000]', ':-1 -1 [000]')  # perf may lose the display TID at exit; payload retains it
 with gzip.open(str(p/'perf-script.txt.gz'),'wt') as f:f.write(''.join(trace))
 qdir=Path(tmp)/'quality';qdir.mkdir()
 (qdir/'P1-test.json').write_text(json.dumps(dict(schema_version=2,lifecycle=[dict(pid=50,tid=50,event='fork',time_ns=10*10**9),dict(pid=50,tid=50,event='exit',time_ns=20*10**9)],lost_samples=0,throttle_records=0,record_counts={'9':len(trace)})))
 out=a.analyze_run(p)
 assert out['clock_alignment']['begin_offset_seconds']==100,out
 assert out['target_scheduling'][100]['on_cpu_s']==6,out
 assert out['target_scheduling'][100]['runnable']==3,out
 assert out['target_scheduling'][100]['blocked']==1,out
 assert out['target_scheduling'][100]['blocked:mmap/rwsem']==1,out
 assert out['scanner_on_cpu_s']==2,out
 assert out['scanner_overlap_target_runnable_s']==2,out
 assert out['scanner_work_elapsed_s']==2,out
 assert out['scanner_work_items']==1,out
 print('PASS: synthetic schedule separates on-CPU, runnable, blocked and exact scanner overlap across distinct clocks')
 # Split the scanner's work item with a half-second preemption by BFS.
 trace=[]
 with gzip.open(str(p/'perf-script.txt.gz'),'rt') as f:original=f.read()
 switch(13,200,'kworker','R',100,'bfs')
 switch(13.5,100,'bfs','R',200,'kworker')
 position=original.index('task 200 [000] 14.')
 with gzip.open(str(p/'perf-script.txt.gz'),'wt') as f:f.write(original[:position]+''.join(trace)+original[position:])
 q=json.loads((qdir/'P1-test.json').read_text());q['record_counts']['9']+=2
 (qdir/'P1-test.json').write_text(json.dumps(q))
 out=a.analyze_run(p)
 assert out['scanner_on_cpu_s']==1.5 and out['scanner_waits_s']['runnable']==.5,out
 assert out['target_scheduling'][100]['on_cpu_s']==6.5 and out['target_scheduling'][100]['runnable']==2.5,out
 assert out['scanner_overlap_target_runnable_s']==1.5,out
 print('PASS: scanner work-item elapsed time separates CPU execution from preemption')

with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)/'measured/P2/test';p.mkdir(parents=True)
 (p/'summary.json').write_text(json.dumps(dict(profile_stage='P2',threads=1,mode='C',elapsed_seconds=10,target_rusage={})))
 (p/'command.json').write_text(json.dumps(dict(launcher_begin_ns=10*10**9,launcher_end_ns=20*10**9,controller_pid=50)))
 (p/'monitor.jsonl').write_text(json.dumps(dict(event='controller',pid=100))+'\n')
 (p/'memory-samples.jsonl').write_text(json.dumps(dict(exe='/bfs',threads=[dict(tid=100,cpu_allowed=[0])]))+'\n')
 lines=[
  'kworker 200 [000] 11.000000000: workqueue:workqueue_execute_start: function cmdp_monitor_work\n',
  'kworker 200 [000] 11.500000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\tffffffff81000001 cmdp_monitor_scan+0x1 (vmlinux)\n',
  'bfs 100 [000] 12.000000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\t123456 DirectionOptimizingBFS+0x1 (/path/bfs)\n',
  'bfs 100 [000] 12.500000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\tffffffff81000002 cmdp_revoke+0x1 (vmlinux)\n',
  'bfs 100 [000] 12.600000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\tffffffffc0000001 ext4_file_read_iter+0x1 ([ext4])\n',
  'bfs 100 [000] 12.700000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\t123456 [unknown] ([unknown])\n',
  'kworker 200 [000] 13.000000000: workqueue:workqueue_execute_end: function cmdp_monitor_work\n',
  'kworker 200 [000] 14.000000000: 10000000 cpu-clock/period=10000000,call-graph=fp/I: \n\tffffffff003 unrelated_work+0x1 (vmlinux)\n',
 ]
 with gzip.open(str(p/'perf-script.txt.gz'),'wt') as f:f.write(''.join(lines))
 qdir=Path(tmp)/'quality';qdir.mkdir()
 (qdir/'P2-test.json').write_text(json.dumps(dict(schema_version=2,lifecycle=[dict(pid=50,tid=50,event='fork',time_ns=10*10**9),dict(pid=50,tid=50,event='exit',time_ns=20*10**9)],lost_samples=0,throttle_records=0,record_counts={'9':len(lines)})))
 out=a.analyze_run(p)
 assert out['sampled_context_cpu_s']=={'scanner':.01,'bfs-user':.02,'bfs-kernel':.02,'other':.01},out
 assert out['leaf_cpu_s']['scanner']==[('cmdp_monitor_scan',.01)],out
 assert out['scanner_work_items']==1 and out['scanner_work_elapsed_s']==2,out
 print('PASS: CPU sample periods and kernel/user scopes; a reused worker is not always the scanner')
 with gzip.open(str(p/'perf-script.txt.gz'),'rt') as f:clock_trace=f.read()
 with gzip.open(str(p/'perf-script.txt.gz'),'wt') as f:f.write(clock_trace.replace('cpu-clock/period=10000000,call-graph=fp/I','cycles/freq=99,call-graph=fp/'))
 out=a.analyze_run(p)
 assert out['sample_weight_unit']=='cycles' and out['sampled_context_cpu_s']=={},out
 assert out['sampled_context_cycles']=={'scanner':10000000,'bfs-user':20000000,'bfs-kernel':20000000,'other':10000000},out
 assert out['leaf_cycles']['scanner']==[('cmdp_monitor_scan',10000000)],out
 print('PASS: hardware cycle weights stay in cycles and never masquerade as seconds')

with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)/'measured/P3/test';p.mkdir(parents=True)
 (p/'summary.json').write_text(json.dumps(dict(profile_stage='P3',threads=1,mode='B',elapsed_seconds=10,target_rusage={})))
 (p/'command.json').write_text(json.dumps(dict(launcher_begin_ns=10*10**9,launcher_end_ns=20*10**9,controller_pid=50)))
 (p/'monitor.jsonl').write_text(json.dumps(dict(event='controller',pid=100))+'\n')
 (p/'memory-samples.jsonl').write_text(json.dumps(dict(exe='/bfs',threads=[dict(tid=100,cpu_allowed=[0])]))+'\n')
 lines=[
  'kworker 200 [000] 11.000000000: workqueue:workqueue_execute_start: function cmdp_monitor_work\n',
  'kworker 200 [000] 11.500000000: tlb:tlb_flush: pages:512 reason:remote IPI send (4)\n',
  'kworker 200 [000] 12.000000000: workqueue:workqueue_execute_end: function cmdp_monitor_work\n',
  'bfs 100 [000] 12.500000000: tlb:tlb_flush: pages:-1 reason:flush on task switch (0)\n',
  'bfs 100 [000] 13.000000000: tlb:tlb_flush: pages:0 reason:flush on task switch (0)\n',
  'bfs 100 [000] 13.500000000: tlb:tlb_flush: pages:12 reason:local MM shootdown (3)\n',
  'bfs 100 [000] 21.000000000: tlb:tlb_flush: pages:-1 reason:flush on task switch (0)\n',
 ]
 with gzip.open(str(p/'perf-script.txt.gz'),'wt') as f:f.write(''.join(lines))
 qdir=Path(tmp)/'quality';qdir.mkdir()
 (qdir/'P3-test.json').write_text(json.dumps(dict(schema_version=2,lifecycle=[dict(pid=50,tid=50,event='fork',time_ns=10*10**9),dict(pid=50,tid=50,event='exit',time_ns=20*10**9)],lost_samples=0,throttle_records=0,record_counts={'9':len(lines)})))
 (p/'pmu.csv').write_text('1.000,CPU0,100,,cycles,1000000000,100.00\n1.000,CPU0,200,,instructions,1000000000,100.00\n1.000,CPU24,50,,cycles,1000000000,100.00\n')
 out=a.analyze_run(p)
 assert out['tlb_page_histogram']=={'scanner|cpu0|remote IPI send|pages512':1,'bfs|cpu0|flush on task switch|pages-1':1,'bfs|cpu0|flush on task switch|pages0':1,'bfs|cpu0|local MM shootdown|pages12':1},out
 assert out['pmu']['groups']['application_cores']=={'cycles':100,'instructions':200},out
 assert out['pmu']['groups']['collector_cores']=={'cycles':50},out
 print('PASS: TLB page counts preserve full/no-op/range distinctions, native boundaries and per-core PMU scope')
