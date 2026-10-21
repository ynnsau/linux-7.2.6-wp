#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import datetime,json,os,pathlib,platform,subprocess
def read(p):
    try: return pathlib.Path(p).read_text().strip()
    except OSError: return None
data={"recorded_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "kernel":platform.uname()._asdict(),"uid":os.geteuid(),
      "page_size":os.sysconf("SC_PAGESIZE"),"affinity":sorted(os.sched_getaffinity(0)),
      "boot_id":read('/proc/sys/kernel/random/boot_id'),
      "cmdline":read('/proc/cmdline'),
      "thp_enabled":read('/sys/kernel/mm/transparent_hugepage/enabled'),
      "governor_cpu0":read('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'),
      "monitor_device":os.path.exists('/dev/cmdp_monitor'),
      "cmdp_stats":read('/sys/kernel/debug/cmdp/stats')}
print(json.dumps(data,indent=2))
if not data['monitor_device'] or data['uid'] != 0 or data['page_size'] != 4096:
    raise SystemExit(1)
PY
