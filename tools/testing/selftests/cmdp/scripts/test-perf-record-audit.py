#!/usr/bin/env python3
"""Small binary fixtures for perf's documented record layouts."""
import importlib.util,struct,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('audit',str(Path(__file__).with_name('audit-perf-data.py')))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def record(kind,payload):return struct.pack('<IHH',kind,0,8+len(payload))+payload
def fixture(path,body):
    header=bytearray(104);header[:8]=b'PERFILE2'
    struct.pack_into('<QQ',header,40,len(header),len(body))
    path.write_bytes(header+body)
with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'perf.data'
    body=record(7,struct.pack('<IIIIQ',123,99,123,99,10000000000))
    body+=record(9,b'opaque sample payload')
    body+=record(4,struct.pack('<IIIIQ',123,99,123,99,20000000000))
    fixture(path,body);q=m.audit(path)
    assert q['pass_'] and q['record_counts'][9]==1
    assert [x['event'] for x in q['lifecycle']]==['fork','exit']
    assert [x['time_ns'] for x in q['lifecycle']]==[10000000000,20000000000]
    assert all(x['pid']==x['tid']==123 for x in q['lifecycle'])
    fixture(path,body+record(2,struct.pack('<QQ',42,7))+record(13,struct.pack('<Q',11)))
    q=m.audit(path);assert not q['pass_'] and q['lost_samples']==18
    fixture(path,body+record(5,struct.pack('<QQQ',11,42,43))+record(6,struct.pack('<QQQ',12,42,43)))
    q=m.audit(path);assert not q['pass_'] and q['throttle_records']==q['unthrottle_records']==1
    fixture(path,body[:-1])
    try:m.audit(path)
    except AssertionError:pass
    else:raise AssertionError('Truncated record was accepted')
print('PASS: raw lifecycle times, both loss record forms, throttling and truncated-file rejection')
