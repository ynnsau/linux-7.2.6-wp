#!/bin/bash
# No host kernel installation, host CMD-P workload, disk passthrough, or network.
set -euo pipefail
kernel=${1:?Usage: run-range-vm.sh KERNEL INITRAMFS NEW_RUN_DIR small|scale|numa|dirty|recovery-baseline|recovery|recovery-numa|batch|fused|cpu}
initrd=${2:?initramfs required}
out=${3:?new run directory required}
suite=${4:-small}
case "$suite" in small|scale|numa|dirty|recovery-baseline|recovery|recovery-numa|batch|fused|cpu) ;; *) exit 2;; esac
test ! -e "$out" || { echo "Refusing existing run directory: $out" >&2; exit 2; }
test -r "$kernel" && test -r "$initrd" && test -r /dev/kvm && test -w /dev/kvm
qemu=${QEMU:-qemu-system-x86_64}
wall=${CMDP_VM_TIMEOUT:-1200}
[[ $wall =~ ^[0-9]+$ ]] && ((wall >= 60 && wall <= 3600))
mkdir -p "$out"
sha256sum "$kernel" "$initrd" > "$out/inputs.sha256"
"$qemu" --version > "$out/qemu-version.txt"
uname -a > "$out/host-uname.before"
cat /proc/sys/kernel/random/boot_id > "$out/host-boot-id.before"
cat /proc/meminfo > "$out/host-meminfo.before"
dmesg > "$out/host-dmesg.before"
available=$(awk '$1 == "MemAvailable:" {print $2}' /proc/meminfo)
test "$available" -ge 25165824 || { echo 'Require at least 24 GiB available host RAM' >&2; exit 2; }
cat /sys/kernel/debug/cmdp/stats > "$out/host-cmdp.before"
for key in entries managed isolated; do
    test "$(awk -v key="$key" '$1==key {print $2}' "$out/host-cmdp.before")" = 0
done
date -u +%FT%TZ > "$out/started-at"
extra_kernel_args=
if test "$suite" = dirty; then extra_kernel_args=' cmdp_disposable_swap=1'; fi
args=( -enable-kvm -cpu host -smp 4,sockets=2,cores=2,threads=1 -m 16384
    -object memory-backend-ram,id=ram0,size=8G
    -object memory-backend-ram,id=ram1,size=8G
    -numa node,nodeid=0,cpus=0-1,memdev=ram0
    -numa node,nodeid=1,cpus=2-3,memdev=ram1
    -kernel "$kernel" -initrd "$initrd"
    -append "console=ttyS0 rdinit=/init panic=-1 oops=panic cmdp_suite=$suite$extra_kernel_args"
    -nodefaults -display none -serial stdio -monitor none -no-reboot -net none )
if test "$suite" = dirty; then
    # Fresh regular file owned by this run; never pass through a host disk.
    truncate -s 256M "$out/swap.img"
    args+=( -drive "file=$out/swap.img,format=raw,if=virtio,cache=none" )
fi
printf '%q ' "$qemu" "${args[@]}" > "$out/qemu-command.txt"
printf '\n' >> "$out/qemu-command.txt"
set +e
timeout --foreground --signal=TERM --kill-after=10 "${wall}s" "$qemu" "${args[@]}" > "$out/serial.log" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > "$out/qemu.exit"
date -u +%FT%TZ > "$out/finished-at"
uname -a > "$out/host-uname.after"
cat /proc/sys/kernel/random/boot_id > "$out/host-boot-id.after"
cat /sys/kernel/debug/cmdp/stats > "$out/host-cmdp.after"
dmesg > "$out/host-dmesg.after"
sha256sum -c "$out/inputs.sha256"
cmp "$out/host-boot-id.before" "$out/host-boot-id.after"
cmp "$out/host-uname.before" "$out/host-uname.after"
cmp "$out/host-cmdp.before" "$out/host-cmdp.after"
grep -E 'CMDP_(RANGE_VM_|TOPOLOGY|NUMA_ENABLED|RANGE_METRIC)|^not ok |^# numa|^# stop_wait|BUG:|WARNING:|Kernel panic' "$out/serial.log" || true
test "$rc" = 0
test "$(grep -c '^CMDP_RANGE_VM_RESULT=0' "$out/serial.log")" = 1
if grep -Eq '^not ok |BUG:|WARNING:|Oops:|KASAN:|Kernel panic|possible circular locking|inconsistent lock state|CMDP_CONSOLE_DRAIN_FAILED' "$out/serial.log"; then
    exit 1
fi
printf 'PASS suite=%s host_unchanged=yes\n' "$suite" | tee "$out/result.txt"
