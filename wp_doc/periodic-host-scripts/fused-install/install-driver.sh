#!/bin/bash
# Run only after the parent operator has reviewed and approved the build record.
set -euo pipefail
record_sha=${1:?Pass the reviewed build-verification.json SHA-256}
[[ $record_sha =~ ^[0-9a-f]{64}$ ]]
run=/home/yans3/cmdp-v2/periodic/native-install-fused-scanner-20261022
scripts=/home/yans3/cmdp-v2/linux-fused-scanner/wp_doc/periodic-host-scripts/fused-install
test "$(id -u)" = 0
test -d "$run"
test ! -e "$run/install.started"
date -u +%FT%TZ > "$run/install.started"
printf '%s\n' "$record_sha" > "$run/approved-build-record.sha256"
set +e
bash "$scripts/install-no-grub.sh" "$record_sha" > "$run/install.log" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > "$run/install.exit"
date -u +%FT%TZ > "$run/install.finished"
exit "$rc"
