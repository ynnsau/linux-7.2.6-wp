#!/bin/bash
set -euo pipefail
base=/home/yans3/cmdp-v2
src=$base/linux
out=$base/build
records=$base/periodic/records
release=$(cat "$records/kernel-release.txt")
test "$release" = 7.2.6-cmdp-periodic-v1
test -s "$records/build-artifacts.sha256"
test -f "$records/runtime-validation.json"
test -z "$(git -C "$src" status --porcelain=v1)"
sha256sum -c "$records/build-artifacts.sha256"
python3 - "$records/runtime-validation.json" "$src" "$release" <<'PY'
import hashlib, json, pathlib, subprocess, sys
record = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert record.get('schema') == 1 and record.get('pass') is True
commit = subprocess.check_output(['git', '-C', sys.argv[2], 'rev-parse', 'HEAD'], universal_newlines=True).strip()
assert record['source_commit'] == commit
assert commit == (pathlib.Path(sys.argv[1]).parent / 'source-head-at-build').read_text().strip()
assert record['kernel_release'] == sys.argv[3]
base = pathlib.Path(sys.argv[2]).parent
for name in ('build/arch/x86/boot/bzImage', 'build/.config', 'build/System.map',
             'periodic/vm/initramfs.cpio.gz', 'periodic/vm/test-binaries.sha256',
             'periodic/logs/vm-production-final.log', 'periodic/logs/vm-debug-final.log',
             'periodic/records/vm-production-final-inputs.sha256',
             'periodic/records/vm-debug-final-inputs.sha256',
             'periodic/reports/production-final/vm-validation.json',
             'periodic/reports/debug-final/vm-validation.json'):
    assert str(base / name) in record['artifacts'], 'Missing validation artifact: ' + name
for name, expected in record['artifacts'].items():
    digest = hashlib.sha256()
    with open(name, 'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    assert digest.hexdigest() == expected, name + ' changed after validation'
PY
cmp /etc/default/grub "$records/grub-defaults-before"
test ! -e "/boot/vmlinuz-$release"
test ! -e "/lib/modules/$release"
sha256sum /boot/vmlinuz-7.2.6-cmdp-v1-be39a758+ \
    /boot/initrd.img-7.2.6-cmdp-v1-be39a758+ > "$records/previous-kernel-before.sha256"
cp /boot/grub/grubenv "$records/grubenv-before-install"
if grub-editenv /boot/grub/grubenv list | grep -q '^next_entry='; then
    echo 'Unexpected pending one-shot GRUB entry; installation stopped' >&2
    exit 1
fi
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= \
    INSTALL_MOD_STRIP=1 modules_install
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= install
sha256sum -c "$records/build-artifacts.sha256"
test -s "/boot/vmlinuz-$release"
test -s "/boot/initrd.img-$release"
test -d "/lib/modules/$release"
cmp "$out/arch/x86/boot/bzImage" "/boot/vmlinuz-$release"
cmp "$out/.config" "/boot/config-$release"
cmp "$out/System.map" "/boot/System.map-$release"
cmp /etc/default/grub "$records/grub-defaults-before"
cmp /boot/grub/grubenv "$records/grubenv-before-install"
grep -F 'set default="Advanced options for Ubuntu>Ubuntu, with Linux 7.2.6-cmdp-v1-be39a758+"' /boot/grub/grub.cfg
grep -F "menuentry 'Ubuntu, with Linux 7.2.6-cmdp-v1-be39a758+'" /boot/grub/grub.cfg
grep -F "menuentry 'Ubuntu, with Linux $release'" /boot/grub/grub.cfg
sha256sum -c "$records/previous-kernel-before.sha256"
cmp /proc/sys/kernel/random/boot_id "$records/boot-id-before"
sha256sum "/boot/vmlinuz-$release" "/boot/initrd.img-$release" \
    "/boot/System.map-$release" "/boot/config-$release" \
    > "$records/installed-artifacts.sha256"
cp /boot/grub/grub.cfg "$records/grub-after.cfg"
git -C "$src" rev-parse HEAD > "$records/installed-source-commit"
uname -r > "$records/running-kernel-after-install"
date -u +%FT%TZ > "$records/install-finished"
printf 'Installed %s; current kernel is %s; no reboot performed.\n' "$release" "$(uname -r)"
