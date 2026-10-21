#!/bin/bash
set -euo pipefail
base=/home/yans3/cmdp-v2
kernel=${1:-$base/build/arch/x86/boot/bzImage}
label=${2:-validation}
sha256sum "$kernel" "$base/periodic/vm/initramfs.cpio.gz" > "$base/periodic/records/vm-$label-inputs.sha256"
timeout --foreground --signal=TERM --kill-after=10 600 qemu-system-x86_64 \
    -enable-kvm -cpu host -smp 4 -m 4096 \
    -kernel "$kernel" -initrd "$base/periodic/vm/initramfs.cpio.gz" \
    -append 'console=ttyS0 rdinit=/init panic=-1 oops=panic' \
    -nographic -monitor none -no-reboot -net none \
    > "$base/periodic/logs/vm-$label.log" 2>&1
sha256sum -c "$base/periodic/records/vm-$label-inputs.sha256"
grep -E 'CMDP_VM_|^ok |^not ok |BUG:|WARNING:|Call Trace|Kernel panic|^# Totals' \
    "$base/periodic/logs/vm-$label.log" || true
grep -q '^CMDP_VM_RESULT=0' "$base/periodic/logs/vm-$label.log"
if grep -Eq 'BUG:|WARNING:|Kernel panic|possible circular locking|inconsistent lock state' "$base/periodic/logs/vm-$label.log"; then
    exit 1
fi
