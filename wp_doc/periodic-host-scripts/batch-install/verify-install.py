#!/usr/bin/env python3
"""Verify versioned installation and unchanged GRUB/host state; never installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import zlib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--record-sha256', required=True)
parser.add_argument('--initramfs-root', type=Path, required=True,
                    help='required unmkinitramfs extraction root outside the archived run')
args = parser.parse_args()
import sys
sys.dont_write_bytecode = True
from common import BUILD, RELEASE, RUN, SOURCE, verified_record
provenance, provenance_checks = verified_record(args.record_sha256)
COMMIT = provenance['commit']
IMAGE_SHA256 = provenance['artifacts']['arch/x86/boot/bzImage']['sha256']
before = json.loads((RUN / 'host-before.json').read_text())
after = json.loads((RUN / 'host-after.json').read_text())
checks = dict(provenance_checks['checks'])
errors = []

def check(name, value):
    checks[name] = bool(value)

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

check('validated_build_provenance', provenance.get('pass') is True and
      provenance.get('release') == RELEASE and provenance.get('commit') == COMMIT and
      provenance.get('module_count', 0) > 5000 and
      provenance.get('artifacts', {}).get('arch/x86/boot/bzImage', {}).get('sha256') == IMAGE_SHA256)
check('source_commit_matches', subprocess.check_output(
    ['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).decode().strip() == COMMIT)
check('source_tree_clean', not subprocess.check_output(
    ['git', '-C', str(SOURCE), 'status', '--porcelain']).decode().strip())
check('grub_unchanged', before['grub'] == after['grub'])
check('boot_links_unchanged', before['boot_links'] == after['boot_links'])
check('previous_boot_files_unchanged', all(
    after['boot_files'].get(k) == v for k, v in before['boot_files'].items()))
check('running_kernel_unchanged', before['kernel'] == after['kernel'])
check('boot_id_unchanged', before['boot_id'] == after['boot_id'])
check('host_cmdp_unchanged', before['cmdp_stats'] == after['cmdp_stats'])
log_before = (RUN / 'dmesg-before.log').read_text().splitlines()
log_after = (RUN / 'dmesg-after.log').read_text().splitlines()
new_log = log_after[len(log_before):]
perf_notice = re.compile(r'^\[\s*\d+(?:\.\d+)?\] perf: interrupt took too long '
                         r'\(\d+ > \d+\), lowering kernel\.perf_event_max_sample_rate to \d+$')
unexpected_log = [line for line in new_log if not perf_notice.fullmatch(line)]
check('kernel_log_prefix_preserved', log_after[:len(log_before)] == log_before)
check('kernel_log_only_standard_perf_notices', not unexpected_log)
expected_new = {'/boot/' + p + '-' + RELEASE for p in
                ['vmlinuz', 'System.map', 'config', 'initrd.img']}
check('only_expected_boot_files_added',
      set(after['boot_files']) - set(before['boot_files']) == expected_new)
build_pairs = {'vmlinuz': 'arch/x86/boot/bzImage', 'System.map': 'System.map', 'config': '.config'}
for prefix, relative in build_pairs.items():
    installed = Path('/boot/' + prefix + '-' + RELEASE)
    expected = provenance['artifacts'][relative]['sha256']
    check(prefix + '_matches_validated_build', installed.is_file() and
          sha(installed) == expected == sha(BUILD / relative))
    metadata = after['boot_files'].get(str(installed), {})
    check(prefix + '_ownership_and_mode', metadata.get('uid') == 0 and
          metadata.get('gid') == 0 and metadata.get('mode') == 0o644)
staged_initrd = RUN / ('initrd.img-' + RELEASE)
initrd = Path('/boot/initrd.img-' + RELEASE)
check('initrd_matches_staged_image', staged_initrd.is_file() and initrd.is_file() and
      sha(staged_initrd) == sha(initrd))
initrd_metadata = after['boot_files'].get(str(initrd), {})
check('initrd_ownership_and_mode', initrd_metadata.get('uid') == 0 and
      initrd_metadata.get('gid') == 0 and initrd_metadata.get('mode') == 0o600)

certificate = (BUILD / 'certs/signing_key.x509').read_bytes()
check('public_certificate_matches_validated_build', hashlib.sha256(certificate).hexdigest() ==
      provenance['artifacts']['certs/signing_key.x509']['sha256'])
installed_image = Path('/boot/vmlinuz-' + RELEASE).read_bytes()
payload = None
position = 0
while True:
    position = installed_image.find(b'\x1f\x8b\x08', position)
    if position < 0:
        break
    try:
        candidate = zlib.decompress(installed_image[position:], 16 + zlib.MAX_WBITS)
        if candidate.startswith(b'\x7fELF'):
            payload = candidate
            break
    except zlib.error:
        pass
    position += 1
check('public_signing_certificate_embedded_in_installed_kernel',
      payload is not None and certificate in payload)
del payload
certificate_pem = subprocess.check_output(
    ['openssl', 'x509', '-inform', 'DER', '-outform', 'PEM'], input=certificate)

def verify_signature(filename):
    data = Path(filename).read_bytes()
    magic = b'~Module signature appended~\n'
    if not data.endswith(magic):
        return {'pass': False, 'error': 'Missing module signature trailer'}
    end = len(data) - len(magic) - 12
    fields = struct.unpack('>BBBBB3sI', data[end:end + 12])
    if not (fields[:5] == (0, 0, 2, 0, 0) and fields[5] == b'\0' * 3 and 0 < fields[6] < end):
        return {'pass': False, 'error': 'Unexpected module signature trailer fields'}
    signature = data[end - fields[6]:end]
    content = data[:end - fields[6]]
    with tempfile.TemporaryDirectory(prefix='cmdp-public-signature-') as temporary:
        certfile = Path(temporary) / 'public-cert.pem'
        contentfile = Path(temporary) / 'module-content.bin'
        certfile.write_bytes(certificate_pem)
        contentfile.write_bytes(content)
        source_relative = Path(filename).relative_to(module_root / 'kernel')
        stripped = Path(temporary) / 'stripped-build-module.ko'
        subprocess.check_call(['strip', '--strip-debug', '-o', str(stripped), str(BUILD / source_relative)])
        payload_matches_build = stripped.read_bytes() == content
        # Skip CA policy, not signature checking. -nointern pins signer lookup
        # to the supplied certificate already checked in the installed kernel.
        verify = subprocess.run(
            ['openssl', 'cms', '-verify', '-binary', '-inform', 'DER', '-noverify',
             '-nointern', '-certfile', str(certfile), '-content', str(contentfile),
             '-out', '/dev/null'], input=signature,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {'pass': verify.returncode == 0 and payload_matches_build,
            'payload_matches_stripped_build_module': payload_matches_build, 'signature_bytes': len(signature),
            'openssl_message': verify.stderr.decode().strip()}

module_root = Path('/lib/modules') / RELEASE
modules = {}
for name in ['nvme', 'nvme_core', 'ahci', 'i40e', 'ixgbe']:
    try:
        def field(key):
            return subprocess.check_output(['modinfo', '-k', RELEASE, '-F', key, name]).decode().strip()
        vermagic, filename, signer = field('vermagic'), field('filename'), field('signer')
        sections = subprocess.check_output(['readelf', '-S', filename]).decode()
        signature = verify_signature(filename)
        modules[name] = {'vermagic': vermagic, 'file': filename, 'modinfo_signer': signer,
                         'bytes': Path(filename).stat().st_size,
                         'sha256': sha(filename), 'signature_verification': signature}
        check('module_' + name, vermagic.split()[0] == RELEASE and
              filename.startswith(str(module_root) + '/'))
        check('module_' + name + '_stripped_and_signed', '.debug_' not in sections and signature['pass'])
    except Exception as exc:
        modules[name] = {'error': str(exc)}
        check('module_' + name, False)
        errors.append('Cannot verify module ' + name + ': ' + str(exc))

ordered = [x.strip() for x in (BUILD / 'modules.order').read_text().splitlines() if x.strip()]
expected_paths = []
for entry in ordered:
    entry = entry[:-2] + '.ko' if entry.endswith('.o') else entry
    assert not entry.startswith('/') and '..' not in Path(entry).parts
    # Kbuild always installs in-tree source-relative paths below MODLIB/kernel,
    # including source files already under kernel/ (e.g. kernel/kheaders.o).
    expected_paths.append('kernel/' + entry)
installed_paths = []
compressed_paths = []
for directory, _, filenames in os.walk(str(module_root), followlinks=False):
    for filename in filenames:
        if filename.endswith(('.ko.gz', '.ko.xz', '.ko.zst')):
            compressed_paths.append(str(Path(directory) / filename))
        if filename.endswith('.ko'):
            installed_paths.append(str((Path(directory) / filename).relative_to(module_root)))
missing_modules = sorted(set(expected_paths) - set(installed_paths))
unexpected_modules = sorted(set(installed_paths) - set(expected_paths))
check('all_expected_module_paths_installed', not missing_modules and not unexpected_modules and not compressed_paths)
check('all_modules_installed', len(installed_paths) == len(expected_paths) ==
      len(set(expected_paths)) == provenance['module_count'])

listing = (RUN / 'initramfs-contents.txt').read_text().splitlines()
extracted = {'performed': args.initramfs_root is not None, 'modules': {}}
for name, basename in [('nvme', 'nvme.ko'), ('nvme_core', 'nvme-core.ko')]:
    suffix = 'lib/modules/' + RELEASE + '/kernel/drivers/nvme/host/' + basename
    check('initramfs_' + name + '_listed', any(
        item == suffix or item.endswith('/' + suffix) for item in listing))
    if args.initramfs_root is not None:
        candidates = [path for path in args.initramfs_root.rglob(basename)
                      if str(path).endswith('/' + suffix) and path.is_file()]
        matches = bool(candidates) and all(sha(path) == modules.get(name, {}).get('sha256')
                                          for path in candidates)
        check('initramfs_' + name + '_matches_installed_signed_module', matches)
        extracted['modules'][name] = {'paths': [str(p) for p in candidates], 'pass': matches}

registration = Path('/var/lib/initramfs-tools') / RELEASE
registration_expected = hashlib.sha1(initrd.read_bytes()).hexdigest() + '  ' + str(initrd) + '\n'
check('initramfs_registered_for_future_maintenance', registration.is_file() and
      registration.read_text() == registration_expected and
      registration.stat().st_uid == 0 and registration.stat().st_gid == 0 and
      registration.stat().st_mode & 0o777 == 0o644)
result = {'pass': all(checks.values()), 'release': RELEASE, 'commit': COMMIT,
          'checks': checks, 'errors': errors, 'modules': modules,
          'build_record_sha256': args.record_sha256, 'provenance': provenance_checks,
          'installed_module_count': len(installed_paths), 'expected_module_count': len(expected_paths),
          'missing_modules': missing_modules, 'unexpected_modules': unexpected_modules, 'unexpected_compressed_modules': compressed_paths,
          'new_kernel_log_lines': new_log, 'unexpected_kernel_log_lines': unexpected_log,
          'public_certificate_sha256': hashlib.sha256(certificate).hexdigest(),
          'module_tool': subprocess.check_output(['modinfo', '--version']).decode().strip(),
          'initramfs_extracted_content': extracted,
          'signature_method': 'OpenSSL detached CMS verification with -nointern using only the public certificate embedded in the installed VM-tested kernel image, plus signed payload byte comparison to the stripped build module; old kmod signer field is not a pass criterion.'}
(RUN / 'installation-verification.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['pass'] else 1)
