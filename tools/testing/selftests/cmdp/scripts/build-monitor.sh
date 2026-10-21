#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")/.." && pwd)
kernel=$(cd -- "$here/../../../.." && pwd)
out=${1:-"$here/build"}
mkdir -p "$out/include/linux"
cp "$kernel/include/uapi/linux/cmdp.h" "$out/include/linux/cmdp.h"
flags=(-O2 -Wall -Wextra -Werror -std=gnu11 -pthread -I"$out/include")
if [[ ${STATIC:-0} == 1 ]]; then flags+=(-static); fi
for program in cmdp_monitor cmdp_fixture cmdp_monitor_test; do
    "${CC:-cc}" "${flags[@]}" "$here/$program.c" -o "$out/$program"
done
printf 'Built controller, workload fixture, and deterministic tests in %s\n' "$out"
