#!/usr/bin/env python3
"""Shared, read-only installation provenance checks (Python 3.6 compatible)."""
import hashlib
import json
import pathlib
import re
import shlex
import subprocess

SOURCE = pathlib.Path('/home/yans3/cmdp-v2/linux-rearm-recovery')
BUILD_RUN = pathlib.Path('/home/yans3/cmdp-v2/periodic/rearm-recovery-20261021')
BUILD = BUILD_RUN / 'build-production'
RUN = pathlib.Path('/home/yans3/cmdp-v2/periodic/native-install-rearm-recovery-20261021')
RECORD = BUILD_RUN / 'records/build-verification.json'
RELEASE = '7.2.6-cmdp-rearm-production'
SUITES = ('dirty', 'small', 'scale', 'numa', 'recovery', 'recovery-numa')

def sha(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def output(argv):
    return subprocess.check_output(argv).decode().strip()

def relative(name):
    p = pathlib.Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Unsafe artifact path: ' + name)
    return p

def module_paths():
    lines = (BUILD / 'modules.order').read_text().splitlines()
    names = [str(relative(x[:-2] + '.ko' if x.endswith('.o') else x))
             for x in lines if x.strip()]
    if any(not x.endswith('.ko') for x in names) or len(names) != len(set(names)):
        raise ValueError('Invalid or duplicate modules.order paths')
    return names

def verified_record(expected_sha):
    """Bind a reviewed report to actual clean source, build, and guest evidence."""
    if not re.fullmatch('[0-9a-f]{64}', expected_sha):
        raise ValueError('Expected build record SHA-256 must be 64 lowercase hex characters')
    if sha(RECORD) != expected_sha:
        raise ValueError('Build record changed since review')
    record = json.loads(RECORD.read_text())
    checks = {}
    def check(name, condition):
        checks[name] = bool(condition)
    check('build_report_pass', record.get('pass') is True and not record.get('errors'))
    check('release', record.get('release') == RELEASE ==
          (BUILD / 'include/config/kernel.release').read_text().strip())
    check('record_locations', record.get('source_path') == str(SOURCE) and
          record.get('build_path') == str(BUILD))
    check('source_commit', output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']) ==
          record.get('commit'))
    check('source_clean', not output(['git', '-C', str(SOURCE), 'status', '--porcelain']))
    required = {'arch/x86/boot/bzImage', 'vmlinux', 'System.map', '.config',
                'Module.symvers', 'modules.order', 'modules.builtin', 'certs/signing_key.x509'}
    check('required_artifacts_recorded', required <= set(record.get('artifacts', {})))
    for name, item in record.get('artifacts', {}).items():
        p = BUILD / relative(name)
        check('artifact:' + name, p.is_file() and sha(p) == item.get('sha256') and
              p.stat().st_size == item.get('bytes'))
    check('required_source_hashes', {'mm/cmdp.c', 'include/uapi/linux/cmdp.h'} <=
          set(record.get('source_sha256', {})))
    for name, digest in record.get('source_sha256', {}).items():
        p = SOURCE / relative(name)
        check('source:' + name, p.is_file() and sha(p) == digest)
    config = (BUILD / '.config').read_text()
    for flag in ('CMDP', 'CMDP_TEST', 'MODULES', 'MODULE_SIG', 'MODULE_SIG_ALL', 'KERNEL_GZIP'):
        check('config:' + flag, 'CONFIG_' + flag + '=y\n' in config)
    check('uncompressed_modules', '# CONFIG_MODULE_COMPRESS is not set\n' in config)
    check('nvme_root_modules', 'CONFIG_BLK_DEV_NVME=m\n' in config and 'CONFIG_NVME_CORE=m\n' in config)
    names = module_paths()
    check('module_count', len(names) == record.get('module_count') and len(names) > 5000)
    check('module_outputs_present', all((BUILD / name).is_file() for name in names))
    job = record.get('build_job', '')
    check('build_job', bool(re.fullmatch('production-build-[A-Za-z0-9][A-Za-z0-9_-]*', job)))
    if checks['build_job']:
        log = BUILD_RUN / 'logs' / (job + '.log')
        status = BUILD_RUN / 'logs' / (job + '.exit')
        check('build_completed', log.is_file() and status.is_file() and
              status.read_text().strip() == record.get('build_exit_status') == '0' and
              str(log) == record.get('build_log'))
    image_sha = record['artifacts']['arch/x86/boot/bzImage']['sha256']
    suites = record.get('production_vm_suites', {})
    check('six_vm_suites', set(SUITES) <= set(suites))
    evidence = {}
    for suite in SUITES:
        if suite not in suites:
            continue
        item = suites[suite]
        path = pathlib.Path(item.get('path', ''))
        if path.parent.parent != BUILD_RUN or not path.parent.name.startswith('production-' + suite + '-v'):
            check('vm:' + suite + ':location', False)
            continue
        report = json.loads(path.read_text())
        vm = path.parent
        check('vm:' + suite + ':pass', item.get('pass') is True and report.get('pass') is True and
              not report.get('errors') and report.get('suite') == suite)
        check('vm:' + suite + ':image', item.get('kernel_sha256') == image_sha)
        check('vm:' + suite + ':serial', sha(vm / 'serial.log') == report.get('serial_sha256'))
        check('vm:' + suite + ':exit', (vm / 'qemu.exit').read_text().strip() == '0')
        for guard in ('host-boot-id', 'host-uname', 'host-cmdp'):
            check('vm:' + suite + ':' + guard,
                  (vm / (guard + '.before')).read_bytes() == (vm / (guard + '.after')).read_bytes())
        inputs = {}
        for line in (vm / 'inputs.sha256').read_text().splitlines():
            digest, filename = line.split(None, 1)
            filename = filename.lstrip('* ')
            inputs[filename] = digest
            check('vm:' + suite + ':input:' + filename, pathlib.Path(filename).is_file() and
                  sha(filename) == digest)
        check('vm:' + suite + ':recorded_image', inputs.get(str(BUILD / 'arch/x86/boot/bzImage')) == image_sha)
        cmd = shlex.split((vm / 'qemu-command.txt').read_text())
        check('vm:' + suite + ':command_image', '-kernel' in cmd and
              cmd[cmd.index('-kernel') + 1] == str(BUILD / 'arch/x86/boot/bzImage'))
        append = cmd[cmd.index('-append') + 1] if '-append' in cmd else ''
        check('vm:' + suite + ':command_suite', 'cmdp_suite=' + suite in append.split())
        evidence[suite] = {'validation_sha256': sha(path), 'serial_sha256': sha(vm / 'serial.log'),
                           'inputs_sha256': sha(vm / 'inputs.sha256'), 'path': str(path)}
    if not all(checks.values()):
        raise ValueError('Provenance verification failed: ' + ', '.join(k for k, v in checks.items() if not v))
    return record, {'pass': True, 'build_record_sha256': expected_sha, 'checks': checks,
                    'vm_evidence': evidence}
