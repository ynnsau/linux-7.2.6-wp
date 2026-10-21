#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")/.." && pwd)
which=all
out=
while (($#)); do
    case $1 in
        --case) which=$2; shift 2;;
        --out) out=$2; shift 2;;
        *) printf 'Usage: %s [--case all|functional|concurrency|lifecycle] --out NEW_DIR\n' "$0" >&2; exit 2;;
    esac
done
[[ -n $out && ! -e $out ]] || { echo 'A new --out directory is required' >&2; exit 2; }
mkdir -p "$out"
bin=${CMDP_BIN_DIR:-"$here/build"}
"$here/scripts/preflight.sh" > "$out/environment.json"
dmesg > "$out/dmesg-before.txt"
set +e
timeout --signal=TERM --kill-after=10s 100s "$bin/cmdp_monitor_test" --case "$which" > "$out/tests.tap" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > "$out/test.exit"
dmesg > "$out/dmesg-after.txt"
python3 - "$out" <<'PY'
import json,pathlib,re,sys
p=pathlib.Path(sys.argv[1]); before=(p/'dmesg-before.txt').read_text().splitlines(); after=(p/'dmesg-after.txt').read_text().splitlines()
prefix=after[:len(before)]==before
new=after[len(before):] if prefix else after
(p/'dmesg-new.txt').write_text('\n'.join(new)+'\n')
bad=[x for x in new if re.search(r'BUG:|WARNING:|Oops:|KASAN:|general protection fault|possible circular locking dependency',x)]
tap=(p/'tests.tap').read_text(); rc=int((p/'test.exit').read_text())
ok=rc==0 and not re.search(r'^not ok\b',tap,re.M) and prefix and not bad
(p/'validation.json').write_text(json.dumps({'pass':ok,'test_exit':rc,'kernel_log_continuous':prefix,'new_kernel_warnings':bad},indent=2)+'\n')
print(tap,end='')
raise SystemExit(0 if ok else 1)
PY
