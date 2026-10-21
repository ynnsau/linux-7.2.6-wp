#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")/.." && pwd)
gapbs= graph= out= trials= period=10 minimum=20 threads=1
while (($#)); do
    case $1 in
        --gapbs-dir) gapbs=$2; shift 2;; --graph) graph=$2; shift 2;;
        --out) out=$2; shift 2;; --trials) trials=$2; shift 2;;
        --period-sec) period=$2; shift 2;; --min-sweeps) minimum=$2; shift 2;;
        --threads) threads=$2; shift 2;;
        *) echo 'Usage: run-bfs.sh --gapbs-dir DIR --graph FILE --out NEW_DIR [--trials N] [--period-sec 10] [--min-sweeps 20] [--threads 1]' >&2; exit 2;;
    esac
done
[[ -x $gapbs/bfs && -f $graph && -n $out && ! -e $out ]] || { echo 'Valid BFS, graph, and a new output directory are required' >&2; exit 2; }
for value in "$period" "$minimum" "$threads"; do [[ $value =~ ^[1-9][0-9]*$ ]] || exit 2; done
[[ -z $trials || $trials =~ ^[1-9][0-9]*$ ]] || exit 2
mkdir -p "$out"
bin=${CMDP_BIN_DIR:-"$here/build"}
"$here/scripts/preflight.sh" > "$out/environment.json"
export OMP_NUM_THREADS=$threads OMP_DYNAMIC=FALSE
# Correctness is checked in a separate monitored run. Verifier writes are never
# included in the subsequent long observation's metrics.
mkdir "$out/verify"
set +e
"$bin/cmdp_monitor" --period-ms "$((period * 1000))" --max-pages 8388608 --max-history 8388608 -- \
    "$gapbs/bfs" -f "$graph" -n 1 -v > "$out/verify/monitor.jsonl" 2> "$out/verify/target.log"
verify_rc=$?
set -e
printf '%s\n' "$verify_rc" > "$out/verify/controller.exit"
set +e
python3 "$here/scripts/validate-run.py" "$out/verify" --min-sweeps 1
verify_validation=$?
python3 - "$out/verify/target.log" <<'PY'
import pathlib,re,sys
s=pathlib.Path(sys.argv[1]).read_text(errors='replace')
if not re.search(r'Verification:\s*PASS',s) or re.search(r'Verification:\s*FAIL',s):
    raise SystemExit('Monitored BFS verification did not report PASS')
PY
verify_content=$?
set -e
python3 "$here/scripts/report-run.py" "$out/verify"
if ((verify_rc || verify_validation || verify_content)); then
    echo 'Monitored correctness preflight failed; long run was not started.' >&2
    exit 1
fi
# Runtime sizing only, not an overhead benchmark. No sleeps extend BFS lifetime.
if [[ -z $trials ]]; then
    "$gapbs/bfs" -f "$graph" -n 2 > "$out/runtime-sizing.log" 2>&1
    trials=$(python3 - "$out/runtime-sizing.log" "$period" "$minimum" <<'PY'
import math,pathlib,re,sys
samples=[float(x) for x in re.findall(r'Trial Time:\s*([0-9.eE+-]+)',pathlib.Path(sys.argv[1]).read_text())]
if not samples or min(samples)<=0: raise SystemExit('Cannot determine BFS trial runtime; provide --trials')
print(max(2,math.ceil(int(sys.argv[2])*int(sys.argv[3])*1.5/min(samples))))
PY
)
fi
python3 - "$out" "$gapbs/bfs" "$graph" "$trials" "$threads" "$period" "$minimum" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); b=pathlib.Path(sys.argv[2]); g=pathlib.Path(sys.argv[3])
# Graph hashing can be recorded separately during dataset construction; avoid a second multi-GB read here.
data={'binary':str(b.resolve()),'binary_sha256':hashlib.sha256(b.read_bytes()).hexdigest(),
      'graph':str(g.resolve()),'graph_bytes':g.stat().st_size,'argv':[str(b),'-f',str(g),'-n',sys.argv[4]],
      'threads':int(sys.argv[5]),'period_seconds':int(sys.argv[6]),'min_sweeps':int(sys.argv[7]),
      'max_pages':8388608,'max_history':8388608,'thp_policy':'disabled per target before exec'}
(p/'manifest.json').write_text(json.dumps(data,indent=2)+'\n')
PY
set +e
"$bin/cmdp_monitor" --period-ms "$((period * 1000))" --max-pages 8388608 --max-history 8388608 -- \
    "$gapbs/bfs" -f "$graph" -n "$trials" > "$out/monitor.jsonl" 2> "$out/target.log"
rc=$?
set -e
printf '%s\n' "$rc" > "$out/controller.exit"
set +e
python3 "$here/scripts/validate-run.py" "$out" --min-sweeps "$minimum" --min-compute-seconds "$((period * minimum))"
validation=$?
set -e
python3 "$here/scripts/report-run.py" "$out"
if ((rc || validation)); then exit 1; fi
