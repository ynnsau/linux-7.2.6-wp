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
KERNEL_INPUTS = ('mm/cmdp.c', 'mm/mprotect.c', 'mm/mremap.c',
                 'include/linux/cmdp.h', 'include/trace/events/cmdp.h',
                 'include/uapi/linux/cmdp.h')

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
    check('required_source_hashes', set(KERNEL_INPUTS) <=
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
    # Require the locked debug image and its configuration independently of
    # the production image. A build report's pass bit is not sufficient.
    debug = BUILD_RUN / 'build-debug'
    debug_record = record.get('debug_artifacts', {})
    check('debug:required_artifacts', {'arch/x86/boot/bzImage', '.config'} <= set(debug_record))
    for name, item in debug_record.items():
        target = debug / relative(name)
        check('debug:artifact:' + name, target.is_file() and sha(target) == item.get('sha256'))
    debug_config = (debug / '.config').read_text()
    check('debug:release', (debug / 'include/config/kernel.release').read_text().strip() ==
          '7.2.6-cmdp-rearm-debug')
    for flag in ('CMDP', 'CMDP_TEST', 'PROVE_LOCKING', 'DEBUG_ATOMIC_SLEEP', 'DEBUG_VM'):
        check('debug:config:' + flag, 'CONFIG_' + flag + '=y\n' in debug_config)

    # Bind the freeze files to the reviewed build report and to their actual
    # committed blobs. Later documentation/harness commits are allowed; none
    # of the six kernel inputs may differ from the fixed pre-build freeze.
    freezes = record.get('source_freezes', {})
    check('freeze:baseline_and_fixed_present', {'baseline', 'fixed'} <= set(freezes))
    frozen_records = {}
    for label in ('baseline', 'fixed'):
        item = freezes.get(label, {})
        path = BUILD_RUN / 'records' / (label + '-kernel-source.json')
        check('freeze:' + label + ':path', item.get('path') == str(path))
        check('freeze:' + label + ':hash', path.is_file() and sha(path) == item.get('sha256'))
        frozen = json.loads(path.read_text())
        frozen_records[label] = frozen
        commit = frozen.get('commit', '')
        hashes = frozen.get('source_sha256', {})
        check('freeze:' + label + ':record_matches', item.get('commit') == commit and
              item.get('source_sha256') == hashes)
        check('freeze:' + label + ':all_inputs', set(KERNEL_INPUTS) <= set(hashes))
        valid_commit = bool(re.fullmatch('[0-9a-f]{40}', commit))
        check('freeze:' + label + ':commit', valid_commit)
        for name in KERNEL_INPUTS:
            if valid_commit:
                blob = subprocess.check_output(['git', '-C', str(SOURCE), 'show', commit + ':' + name])
                check('freeze:' + label + ':git:' + name,
                      hashlib.sha256(blob).hexdigest() == hashes.get(name))
            if label == 'fixed':
                check('freeze:fixed:actual:' + name,
                      sha(SOURCE / name) == hashes.get(name) == record.get('source_sha256', {}).get(name))
    check('freeze:behavior_change', frozen_records['baseline'].get('source_sha256', {}).get('mm/cmdp.c') !=
          frozen_records['fixed'].get('source_sha256', {}).get('mm/cmdp.c'))
    baseline_image = BUILD_RUN / 'baseline-artifacts/bzImage'
    baseline = record.get('baseline_image', {})
    baseline_sha = baseline.get('sha256')
    check('baseline:image', baseline.get('path') == str(baseline_image) and baseline_image.is_file() and
          sha(baseline_image) == baseline_sha)
    check('baseline:source_copy', json.loads((BUILD_RUN / 'baseline-artifacts/source.json').read_text()) ==
          frozen_records['baseline'])
    debug_sha = debug_record.get('arch/x86/boot/bzImage', {}).get('sha256')
    check('baseline:distinct_from_fixed', baseline_sha and debug_sha and baseline_sha != debug_sha)

    evidence = {}
    def vm_evidence(prefix, suite, item, image, image_sha):
        key = 'vm:' + prefix + ':' + suite
        path = pathlib.Path(item.get('path', ''))
        pattern = re.compile(re.escape(prefix + '-' + suite) + r'-v([0-9]+)')
        if path.parent.parent != BUILD_RUN or not pattern.fullmatch(path.parent.name):
            check(key + ':location', False)
            return
        # Do not silently accept a recorded older pass if a newer attempt
        # exists and failed. Numeric versions make v10 newer than v9.
        versions = []
        for candidate in BUILD_RUN.glob(prefix + '-' + suite + '-v*/validation.json'):
            match = pattern.fullmatch(candidate.parent.name)
            if match:
                versions.append((int(match.group(1)), candidate))
        check(key + ':latest', bool(versions) and max(versions)[1] == path)
        report = json.loads(path.read_text())
        vm = path.parent
        check(key + ':validation_hash', sha(path) == item.get('validation_sha256'))
        check(key + ':pass', item.get('pass') is True and report.get('pass') is True and
              not report.get('errors') and report.get('suite') == suite)
        taps = report.get('tap_suites', [])
        check(key + ':complete_tap', bool(taps) and all(t.get('pass') is True and t.get('planned', 0) > 0 and
              t.get('planned') == t.get('observed') for t in taps) and
              [t.get('planned') for t in taps] == item.get('tap_checks'))
        check(key + ':image', item.get('kernel_sha256') == image_sha)
        serial_sha = sha(vm / 'serial.log')
        check(key + ':serial', serial_sha == report.get('serial_sha256') == item.get('serial_sha256'))
        check(key + ':exit', (vm / 'qemu.exit').read_text().strip() == '0')
        for guard in ('host-boot-id', 'host-uname', 'host-cmdp'):
            check(key + ':' + guard,
                  (vm / (guard + '.before')).read_bytes() == (vm / (guard + '.after')).read_bytes())
        inputs = {}
        for line in (vm / 'inputs.sha256').read_text().splitlines():
            digest, filename = line.split(None, 1)
            filename = filename.lstrip('* ')
            check(key + ':input_unique:' + filename, filename not in inputs)
            inputs[filename] = digest
            check(key + ':input:' + filename, bool(re.fullmatch('[0-9a-f]{64}', digest)) and
                  pathlib.Path(filename).is_file() and sha(filename) == digest)
        check(key + ':input_manifest', sha(vm / 'inputs.sha256') == item.get('inputs_sha256'))
        check(key + ':recorded_image', inputs.get(str(image)) == image_sha)
        cmd = shlex.split((vm / 'qemu-command.txt').read_text())
        check(key + ':command_image', '-kernel' in cmd and cmd[cmd.index('-kernel') + 1] == str(image))
        append = cmd[cmd.index('-append') + 1] if '-append' in cmd else ''
        check(key + ':command_suite', 'cmdp_suite=' + suite in append.split())
        check(key + ':command_initrd', '-initrd' in cmd and cmd[cmd.index('-initrd') + 1] in inputs)
        if suite == 'recovery-baseline':
            lines = (vm / 'serial.log').read_text(errors='replace').splitlines()
            configs = [dict(re.findall(r'(\w+)=([^ ]+)', line)) for line in lines
                       if line.startswith('CMDP_RECOVERY_CONFIG ')]
            check(key + ':expected_failure_cases', len(taps) == len(configs) == 2 and
                  sorted(x.get('threads') for x in configs) == ['1', '4'] and
                  all(x.get('expected') == 'stranded' for x in configs))
            check(key + ':expected_failure_evidence', sum(line.strip() ==
                  'CMDP_RECOVERY_EXPECTED_FAILURE_REPRODUCED stranded=yes' for line in lines) == 2 and
                  not any('CMDP_RECOVERY_FIXED_VERIFIED' in line for line in lines))
        evidence[prefix + ':' + suite] = {'validation_sha256': sha(path), 'serial_sha256': serial_sha,
                                          'inputs_sha256': sha(vm / 'inputs.sha256'), 'path': str(path)}

    image_sha = record['artifacts']['arch/x86/boot/bzImage']['sha256']
    for prefix, field, image, digest in (
            ('production', 'production_vm_suites', BUILD / 'arch/x86/boot/bzImage', image_sha),
            ('debug', 'debug_vm_suites', debug / 'arch/x86/boot/bzImage', debug_sha)):
        suites = record.get(field, {})
        check(prefix + ':six_vm_suites', set(SUITES) <= set(suites))
        for suite in SUITES:
            if suite in suites:
                vm_evidence(prefix, suite, suites[suite], image, digest)
    check('baseline:vm_record', bool(record.get('baseline_vm_suite')))
    if record.get('baseline_vm_suite'):
        vm_evidence('debug', 'recovery-baseline', record['baseline_vm_suite'], baseline_image, baseline_sha)
    if not all(checks.values()):
        raise ValueError('Provenance verification failed: ' + ', '.join(k for k, v in checks.items() if not v))
    return record, {'pass': True, 'build_record_sha256': expected_sha, 'checks': checks,
                    'vm_evidence': evidence}
