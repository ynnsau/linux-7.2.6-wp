#!/usr/bin/env python3
"""Refuse installation unless reviewed build provenance and fresh target paths match."""
import argparse
import json
import pathlib
import sys
sys.dont_write_bytecode = True
from common import BUILD, RELEASE, RUN, SOURCE, module_paths, output, sha, verified_record

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--record-sha256', required=True)
a = p.parse_args()
record, provenance = verified_record(a.record_sha256)
checks = dict(provenance['checks'])
for prefix in ('vmlinuz', 'System.map', 'config', 'initrd.img'):
    target = pathlib.Path('/boot') / (prefix + '-' + RELEASE)
    checks[str(target) + ':absent'] = not target.exists() and not target.is_symlink()
for target in (pathlib.Path('/lib/modules') / RELEASE, pathlib.Path('/var/lib/initramfs-tools') / RELEASE):
    checks[str(target) + ':absent'] = not target.exists() and not target.is_symlink()
checks['target_not_running'] = output(['uname', '-r']) != RELEASE
checks['first_install_attempt'] = not (RUN / 'host-before.json').exists()
stats = dict(line.split() for line in pathlib.Path('/sys/kernel/debug/cmdp/stats').read_text().splitlines())
checks['zero_host_ownership'] = all(int(stats[k]) == 0 for k in ('entries', 'managed', 'isolated'))
record_path = pathlib.Path(__file__).resolve().parent
scripts = {x.name: sha(x) for x in record_path.iterdir() if x.suffix in ('.py', '.sh')}
result = {'pass': all(checks.values()), 'checks': checks, 'release': RELEASE,
          'commit': record['commit'], 'expected_kernel_sha256': record['artifacts']['arch/x86/boot/bzImage']['sha256'],
          'expected_module_count': record['module_count'], 'build_record_sha256': a.record_sha256,
          'provenance': provenance, 'installer_scripts_sha256': scripts}
(RUN / 'preflight-verification.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
sys.exit(0 if result['pass'] else 1)
