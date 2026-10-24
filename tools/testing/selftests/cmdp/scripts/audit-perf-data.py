#!/usr/bin/env python3
"""Read uncompressed perf.data record headers for loss/throttle accounting.

Preserves all evidence. PERF_RECORD_LOST (2), THROTTLE (5), UNTHROTTLE (6),
LOST_SAMPLES (13). Decoding does not depend on symbols or trace print formats.
"""
import argparse,json,mmap,struct
from collections import Counter
from pathlib import Path

def audit(path):
 with open(str(path),'rb') as f:
  with mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
   assert data[:8]==b'PERFILE2','Expected native little-endian perf v2 file'
   start,size=struct.unpack_from('<QQ',data,40)
   stop=start+size;assert stop<=len(data)
   offset=start;counts=Counter();lost=0;lifecycle=[]
   while offset<stop:
    typ,misc,length=struct.unpack_from('<IHH',data,offset)
    assert length>=8 and offset+length<=stop,(offset,typ,length,stop)
    counts[typ]+=1
    if typ in (4,7):
     pid,ppid,tid,ptid,timestamp=struct.unpack_from('<IIIIQ',data,offset+8)
     if timestamp:
      lifecycle.append(dict(event='exit' if typ==4 else 'fork',pid=pid,ppid=ppid,tid=tid,ptid=ptid,time_ns=timestamp))
    if typ==2:lost+=struct.unpack_from('<Q',data,offset+16)[0]
    elif typ==13:lost+=struct.unpack_from('<Q',data,offset+8)[0]
    offset+=length
   return dict(schema_version=2,lifecycle=lifecycle,file=str(path),bytes=len(data),data_offset=start,data_bytes=size,record_counts=dict(counts),lost_samples=lost,throttle_records=counts[5],unthrottle_records=counts[6],pass_=lost==0 and counts[5]==0)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('paths',nargs='+',type=Path);a=p.parse_args()
 print(json.dumps([audit(x) for x in a.paths],indent=2))
