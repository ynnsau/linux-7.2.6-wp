#!/usr/bin/env python3
"""Reject malformed scanner placement before launch or any CMD-P access."""
import argparse, json, pathlib, subprocess
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('monitor',type=pathlib.Path)
p.add_argument('--output',type=pathlib.Path,required=True)
a=p.parse_args(); results=[]
for value in ('','-2','-1','+1',' 1','1 ','1,2','1-3','AUTO','2147483648','9999999999999999999999999'):
    options=['--scanner-cpu',value,'--','/bin/true']
    r=subprocess.run([str(a.monitor)]+options,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    assert r.returncode==2 and not r.stdout, (options,r.returncode,r.stdout,r.stderr)
    results.append({'options':options,'pass':True})
for options in (['--scanner-cpu','1','--scanner-cpu','2'], ['--scanner-cpu','auto','--monitor-off'],
                ['--scanner-cpu','1','--once']):
    r=subprocess.run([str(a.monitor)]+options+['--','/bin/true'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    assert r.returncode==2 and not r.stdout, (options,r.returncode,r.stdout,r.stderr)
    results.append({'options':options,'pass':True})
a.output.write_text(json.dumps({'pass':True,'checks':results},indent=2)+'\n')
print('PASS:',len(results),'scanner CPU parsing checks')
