import pathlib,json,subprocess,hashlib,datetime
r=pathlib.Path('/home/yans3/cmdp-v2/redis-ycsb-study/eligibility-v2')
state=json.loads((r/'verify-state.json').read_text()); assert state['status'] in ('complete','failed')
text=lambda p:pathlib.Path(p).read_text().strip()
ps=sorted(pathlib.Path('/sys/devices/system/cpu/cpufreq').glob('policy*'))
pol={p.name:{k:text(p/k) for k in ('scaling_governor','scaling_min_freq','scaling_max_freq','boost')} for p in ps}
stats={k:int(v) for k,v in (x.split() for x in text('/sys/kernel/debug/cmdp/stats').splitlines())}
processes=subprocess.run(['ps','-C','redis-server,java,cmdp_monitor','-o','pid=,comm='],stdout=subprocess.PIPE,universal_newlines=True).stdout.strip()
dmesg=subprocess.check_output(['dmesg'],universal_newlines=True);(r/'dmesg-after.txt').write_text(dmesg)
a=(r/'dmesg-before.txt').read_text().splitlines();b=dmesg.splitlines();assert b[:len(a)]==a,'Kernel ring buffer rotated; inspect manually'
helper=pathlib.Path('/home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py')
legacy_sha=hashlib.sha256(helper.read_bytes()).hexdigest()
repo='/home/yans3/cmdp-v2/linux-fused-scanner'
s={'at':datetime.datetime.utcnow().isoformat()+'Z','kernel':subprocess.check_output(['uname','-r'],universal_newlines=True).strip(),'boot_id':text('/proc/sys/kernel/random/boot_id'),'cpu_policies':pol,'global_boost':text('/sys/devices/system/cpu/cpufreq/boost'),'global_thp':text('/sys/kernel/mm/transparent_hugepage/enabled'),'cmdp':stats,'matching_processes':processes,'new_dmesg_lines':b[len(a):],'legacy_helper_sha256':legacy_sha,'source_commit':subprocess.check_output(['git','-C',repo,'rev-parse','HEAD'],universal_newlines=True).strip(),'source_status':subprocess.check_output(['git','-C',repo,'status','--short'],universal_newlines=True)}
s['passed']=len(pol)==52 and all(p=={'scaling_governor':'performance','scaling_min_freq':'2100000','scaling_max_freq':'2100000','boost':'0'} for p in pol.values()) and s['global_boost']=='0' and s['global_thp']=='always [madvise] never' and all(stats[k]==0 for k in ('entries','managed','isolated','waiters')) and not processes and legacy_sha=='1abe8e924b5c500ecc31290aeb42ede216f861b4fc650f6b3213ed862f36b796' and not s['source_status'] and not s['new_dmesg_lines']
(r/'machine-after.json').write_text(json.dumps(s,sort_keys=True,indent=2)+'\n')
print(json.dumps({k:s[k] for k in ('passed','kernel','matching_processes','new_dmesg_lines','source_commit','source_status')}))
