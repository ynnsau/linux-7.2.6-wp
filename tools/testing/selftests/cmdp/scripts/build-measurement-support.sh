#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")/.." && pwd)
kernel=$(cd -- "$here/../../../.." && pwd)
out=${1:?Usage: build-measurement-support.sh OUTPUT_DIRECTORY}
mkdir -p "$out/include/linux"
cp "$kernel/include/uapi/linux/cmdp.h" "$out/include/linux/cmdp.h"
flags=(-O2 -Wall -Wextra -Werror -std=gnu11 -pthread -I"$out/include")
for program in cmdp_monitor frequency_collect counter_support_test; do
    "${CC:-cc}" "${flags[@]}" "$here/$program.c" -o "$out/$program"
done
"$out/counter_support_test"
printf 'Built and tested userspace measurement support in %s\n' "$out"
