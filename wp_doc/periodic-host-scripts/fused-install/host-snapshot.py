#!/usr/bin/env python3
"""Capture installation guards without changing host configuration."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
sys.dont_write_bytecode = True
from common import RUN
phase = sys.argv[1]
assert phase in ('before', 'after')

def describe(path):
    st = path.lstat()
    value = {'mode': stat.S_IMODE(st.st_mode), 'uid': st.st_uid,
             'gid': st.st_gid, 'mtime_ns': st.st_mtime_ns}
    if path.is_symlink():
        value['symlink'] = os.readlink(str(path))
    elif path.is_file():
        value['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        value['bytes'] = st.st_size
    else:
        value['directory'] = True
    return value

grub = {}
for name in ['/boot/grub', '/etc/default/grub', '/etc/default/grub.d', '/etc/grub.d']:
    path = Path(name)
    if not path.exists() and not path.is_symlink():
        grub[name] = {'absent': True}
        continue
    grub[name] = describe(path)
    if path.is_dir():
        for child in sorted(path.rglob('*')):
            grub[str(child)] = describe(child)
links = {}
for name in ['/vmlinuz', '/vmlinuz.old', '/initrd.img', '/initrd.img.old',
             '/boot/vmlinuz', '/boot/vmlinuz.old', '/boot/initrd.img', '/boot/initrd.img.old']:
    path = Path(name)
    links[name] = describe(path) if path.exists() or path.is_symlink() else {'absent': True}
boot = {}
for prefix in ['vmlinuz-', 'System.map-', 'config-', 'initrd.img-']:
    for path in sorted(Path('/boot').glob(prefix + '*')):
        boot[str(path)] = describe(path)
record = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'grub': grub, 'boot_links': links, 'boot_files': boot,
          'kernel': subprocess.check_output(['uname', '-r']).decode().strip(),
          'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'cmdp_stats': Path('/sys/kernel/debug/cmdp/stats').read_text()}
with (RUN / ('host-' + phase + '.json')).open('x') as out:
    out.write(json.dumps(record, indent=2) + '\n')
(RUN / ('dmesg-' + phase + '.log')).write_bytes(subprocess.check_output(['dmesg']))
print(json.dumps({'phase': phase, 'grub_entries': len(grub), 'boot_files': len(boot),
                  'kernel': record['kernel'], 'boot_id': record['boot_id']}))
