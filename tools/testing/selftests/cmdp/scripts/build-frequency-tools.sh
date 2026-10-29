#!/bin/sh
# Build experiment-local tools without installing a service or changing the kernel.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
ROOT=${1:-/home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1}
mkdir -p "$ROOT/bin" "$ROOT/build/cpupower" "$ROOT/build/turbostat" "$ROOT/build/logs"
make -C "$REPO/tools/power/cpupower" O="$ROOT/build/cpupower" NLS=false CPUFREQ_BENCH=false DEBUG=false STATIC=true -j4 > "$ROOT/build/logs/cpupower.log" 2>&1
make -C "$REPO/tools/power/x86/turbostat" O="$ROOT/build/turbostat" -j4 > "$ROOT/build/logs/turbostat.log" 2>&1
cc -O2 -std=gnu11 -Wall -Wextra -Werror -fno-tree-vectorize -fno-tree-slp-vectorize -march=x86-64 -mno-avx -mno-avx2 -mno-avx512f "$REPO/tools/testing/selftests/cmdp/frequency_load.c" -o "$ROOT/build/frequency_load" > "$ROOT/build/logs/frequency_load.log" 2>&1
cp "$ROOT/build/cpupower/cpupower" "$ROOT/bin/cpupower"
cp "$ROOT/build/turbostat/turbostat" "$ROOT/bin/turbostat"
cp "$ROOT/build/frequency_load" "$ROOT/bin/frequency_load"
python3 - "$ROOT" "$REPO" <<'PY'
import hashlib,json,pathlib,shlex,subprocess,sys,time
root,repo=map(pathlib.Path,sys.argv[1:])
wrapper=root/'bin'/'cpu-frequency'
wrapper.write_text('#!/bin/sh\nexec python3 {} --root {} \"$@\"\n'.format(shlex.quote(str(repo/'tools/testing/selftests/cmdp/scripts/cpu-frequency.py')),shlex.quote(str(root))))
wrapper.chmod(0o755)
def out(cmd): return subprocess.check_output(cmd,stderr=subprocess.STDOUT).decode().strip()
r={'schema':1,'built_epoch':time.time(),'source_commit':out(['git','-C',str(repo),'rev-parse','HEAD']),'source_status':out(['git','-C',str(repo),'status','--short']),'compiler':out(['cc','--version']).splitlines()[0],'tools':{}}
for name in ('cpupower','turbostat','frequency_load','cpu-frequency'):
 p=root/'bin'/name
 if p.is_file(): r['tools'][p.name]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}
r['sources']={}
for rel in ('tools/testing/selftests/cmdp/frequency_load.c','tools/testing/selftests/cmdp/scripts/build-frequency-tools.sh','tools/power/x86/turbostat/turbostat.c','tools/power/cpupower/Makefile'):
 p=repo/rel
 r['sources'][rel]=hashlib.sha256(p.read_bytes()).hexdigest()
for name in ('cpupower','turbostat'): r['tools'][name]['version']=out([str(root/'bin'/name),'--version'])
(root/'build'/'manifest.json').write_text(json.dumps(r,indent=2,sort_keys=True)+'\n')
print(json.dumps(r,indent=2,sort_keys=True))
PY
