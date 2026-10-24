#!/usr/bin/env python3
"""Read matching ELF layout and map weighted symbol offsets. Run between jobs."""
import argparse, hashlib, json, subprocess
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--vmlinux',type=Path,required=True)
p.add_argument('--source',type=Path,required=True)
p.add_argument('--callsites',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
commands=[]
def run(argv):
    commands.append(argv)
    return subprocess.check_output(argv,universal_newlines=True,stderr=subprocess.STDOUT)

layouts={}
for name in ('cmdp_alias','cmdp_entry','cmdp_history','cmdp_region'):
    layouts[name]=run(['pahole','-C',name,str(a.vmlinux)])
    (a.output/(name+'.layout.txt')).write_text(layouts[name])

symbols={}
for line in run(['nm','-n','--defined-only',str(a.vmlinux)]).splitlines():
    fields=line.split()
    if len(fields)==3 and fields[1] in ('t','T') and fields[2].startswith('cmdp_'):
        symbols[fields[2]]=int(fields[0],16)
(a.output/'cmdp-symbols.json').write_text(json.dumps({k:hex(v) for k,v in symbols.items()},indent=2)+'\n')

requests=json.loads(a.callsites.read_text())
mapped=[]
for item in requests:
    pieces=item['symbol_offset'].rsplit('+0x',1)
    name=pieces[0];offset=int(pieces[1],16) if len(pieces)==2 else 0
    address=symbols[name]+offset-(1 if item['kind'].startswith('caller_of:') else 0)
    mapped.append(dict(item,elf_address=hex(address)))
if mapped:
    argv=['addr2line','-a','-i','-f','-e',str(a.vmlinux)]+[x['elf_address'] for x in mapped]
    output=run(argv)
    (a.output/'addr2line-output.txt').write_text(output)
    blocks=[]
    for line in output.splitlines():
        if line.startswith('0x'):
            blocks.append(dict(address=int(line,16),lines=[]))
        else:blocks[-1]['lines'].append(line)
    assert len(blocks)==len(mapped)
    for item,block in zip(mapped,blocks):
        assert int(item['elf_address'],16)==block['address']
        item['source_inline_chain']=block['lines']
(a.output/'mapped-callsites.json').write_text(json.dumps(mapped,indent=2)+'\n')

source_files={}
for relative in ('mm/cmdp.c','arch/x86/mm/tlb.c','include/linux/cmdp.h'):
    source=a.source/relative
    data=source.read_bytes()
    (a.output/relative.replace('/','_')).write_bytes(data)
    source_files[relative]=hashlib.sha256(data).hexdigest()
slab={}
for key in ('object_size','size','slab_size','objects','slabs'):
    path=Path('/sys/kernel/slab/kmalloc-1k')/key
    if path.exists():slab[key]=path.read_text().strip()
manifest=dict(commands=commands,source_files_sha256=source_files,
    source_commit=run(['git','-C',str(a.source),'rev-parse','HEAD']).strip(),
    source_status=run(['git','-C',str(a.source),'status','--short']),
    kmalloc_1k_live=slab,
    note='Read-only ELF/source inspection between timed jobs. Caller return addresses mapped at minus one; sample IPs unchanged. Weights are overlapping views, not additive costs.')
(a.output/'inspection.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(dict(layouts=list(layouts),mapped_offsets=len(mapped),source_clean=not manifest['source_status'])))
