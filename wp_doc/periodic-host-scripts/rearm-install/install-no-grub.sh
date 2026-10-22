#!/bin/bash
# Install the reviewed, versioned image/modules directly. No bootloader hooks.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
record_sha=${1:?Pass the independently reviewed build-verification.json SHA-256}
[[ $record_sha =~ ^[0-9a-f]{64}$ ]]
src=/home/yans3/cmdp-v2/linux-rearm-recovery
build_run=/home/yans3/cmdp-v2/periodic/rearm-recovery-20261021
build=$build_run/build-production
run=/home/yans3/cmdp-v2/periodic/native-install-rearm-recovery-20261021
scripts=$src/wp_doc/periodic-host-scripts/rearm-install
release=7.2.6-cmdp-rearm-production
test "$(id -u)" = 0
test -d "$run"
exec 9>"$run/install.lock"
flock -n 9
test ! -e "$run/host-before.json"
python3 "$scripts/preflight.py" --record-sha256 "$record_sha"
cp "$build_run/records/build-verification.json" "$run/build-verification.json"
python3 "$scripts/host-snapshot.py" before
python3 - "$run/host-before.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]))
stats=dict(line.split() for line in s['cmdp_stats'].splitlines())
assert all(int(stats[k]) == 0 for k in ('entries', 'managed', 'isolated'))
PY
# This target invokes in-tree module installation/signing and depmod only.
# Do not use `make install`, update-initramfs, or /etc/kernel postinst hooks.
make -C "$src" O="$build" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= INSTALL_MOD_STRIP=1 -j16 modules_install
install -m 0644 "$build/arch/x86/boot/bzImage" "/boot/vmlinuz-$release"
install -m 0644 "$build/System.map" "/boot/System.map-$release"
install -m 0644 "$build/.config" "/boot/config-$release"
mkinitramfs -o "$run/initrd.img-$release" "$release"
lsinitramfs "$run/initrd.img-$release" > "$run/initramfs-contents.txt"
python3 - "$run/initramfs-contents.txt" "$release" <<'PY'
import pathlib,sys
lines=pathlib.Path(sys.argv[1]).read_text().splitlines()
for basename in ('nvme.ko','nvme-core.ko'):
    suffix='lib/modules/'+sys.argv[2]+'/kernel/drivers/nvme/host/'+basename
    assert any(x == suffix or x.endswith('/'+suffix) for x in lines), suffix
PY
install -m 0600 "$run/initrd.img-$release" "/boot/initrd.img-$release"
python3 - "$run" "$release" <<'PY'
import hashlib,json,os,pathlib,sys
run=pathlib.Path(sys.argv[1]); release=sys.argv[2]
image=pathlib.Path('/boot/initrd.img-'+release)
registration=pathlib.Path('/var/lib/initramfs-tools')/release
content=hashlib.sha1(image.read_bytes()).hexdigest()+'  '+str(image)+'\n'
with registration.open('x') as f:
    f.write(content); f.flush(); os.fsync(f.fileno())
os.chown(str(registration),0,0); os.chmod(str(registration),0o644)
(run/'initramfs-registration.json').write_text(json.dumps({'path':str(registration),'content_match':registration.read_text()==content,'content':content},indent=2)+'\n')
PY
extract_dir=$(mktemp -d /tmp/cmdp-rearm-initramfs.XXXXXX)
printf '%s\n' "$extract_dir" > "$run/initramfs-extraction-path.txt"
unmkinitramfs "$run/initrd.img-$release" "$extract_dir"
sync
python3 "$scripts/host-snapshot.py" after
python3 "$scripts/verify-install.py" --record-sha256 "$record_sha" --initramfs-root "$extract_dir"
date -u +%FT%TZ > "$run/install-complete"
