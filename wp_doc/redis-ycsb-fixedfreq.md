# Fixed-frequency Redis/YCSB handoff

This campaign compares **off, dirty sampling, and dirty sampling + ARM** for
YCSB A/B/C. Every run uses eight workers and a 70,000 logical-operations/s target,
excludes the first 20 seconds, and measures 600 seconds. Three repetitions per
workload/mode give 27 measured runs. Workload and mode order rotate.

At initial handoff on 2026-10-29, **2.1 GHz was the candidate and its full
30-minute all-core qualification was still running**. Consult `profile.json` and
the status command for the current result; this document is not qualification
evidence. The runner requires a passed 1,800-second qualification on this boot.

## Paths and fixed configuration

On `ssh bmp`:

```sh
kernel_repo=/home/yans3/cmdp-v2/linux-fused-scanner
study_root=/home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1
cd "$kernel_repo"
```

| Item | Setting |
|---|---|
| Kernel | `7.2.6-cmdp-cpu-production`; no kernel change required |
| Redis | All threads CPU1, memory node0, localhost:6380 |
| YCSB | Eight workers; JVM CPUs26–33, memory node1; 12 GiB G1 heap |
| Scanner / observer | CPU0 / CPU24 |
| Sampling / quiet interval | 10 seconds / 10 seconds |
| Dirty / ARM batch limits | 512 / 512 |
| Frequency observation | Grouped APERF/MPERF/TSC on all 52 CPUs every 11 seconds and at boundaries |

Redis restores the existing read-only golden RDB before every run. It contains
11,500,000 standard YCSB records, ten 100-byte fields each, plus `_indices`.
Restored memory is approximately 16 GiB. The RDB remains under
`/home/yans3/cmdp-v2/redis-ycsb-study/artifacts/golden/`; its SHA256 is
`67b93f3673f2645b47f17d21dbb6466ae17ad8bd1a2022bf223d9a463fcd2be2`.
`prepare` checks the actual RDB hash against its frozen manifest and saves
`golden-verification.json`. No reload through YCSB is needed. Persistence is disabled, shutdown uses NOSAVE,
and restore/export time is outside the selected latency window. Existing
baseline artifacts and their frozen helper are preserved.

## Build, qualify, and restore frequency

Build the private tools as `yans3`; these commands do not install a kernel:

```sh
sh tools/testing/selftests/cmdp/scripts/build-frequency-tools.sh "$study_root"
bash tools/testing/selftests/cmdp/scripts/build-measurement-support.sh "$study_root/bin"
python3 tools/testing/selftests/cmdp/scripts/test-redis-ycsb-fixedfreq.py \
  --boundary-smoke "$study_root/boundary-smoke"
"$study_root/bin/cpu-frequency" inspect
"$study_root/bin/cpu-frequency" status
```

The locally built cpupower/turbostat avoid Ubuntu's wrapper dependency on a
distribution-matching kernel package. Frequency application uses the driver's
advertised sysfs controls, verifies all policies, disables boost, and sets equal
minimum/maximum limits. The 2.101 GHz entry represents turbo access, not a precise
lock; qualification starts at the explicit 2.1 GHz non-turbo setting and tries
lower advertised settings if necessary. Idle states remain enabled.

Only run a new qualification when one is not already active:

```sh
sudo "$study_root/bin/cpu-frequency" qualify --start-khz 2100000 \
  --seconds 1800 --settle-seconds 20
```

Qualification runs a scalar worker on every core, checks busy frequency against
the target, and records thermal/throttle and machine-error evidence. Successful
qualification leaves the selected clock locked for this boot. Failure restores
the saved policy. The original state is `state.json`; attempts and turbostat
evidence are under `qualification/`; the decision is `profile.json`.

To explicitly apply a setting or restore the saved current-boot policy:

```sh
sudo "$study_root/bin/cpu-frequency" apply --khz 2100000
sudo "$study_root/bin/cpu-frequency" restore
```

`apply` alone does not qualify a frequency. Use the selected value in the profile
if qualification chose a lower setting. Restore verifies boot/topology identity.
There is no GRUB change, reboot, BIOS write, or boot-time persistence. Do not
restore between compared runs; leave the validated lock after the campaign unless
restoration is explicitly wanted. Frequency and experiment operations share an
exclusive lock.

## Campaign commands and durable execution

The supervisor runs as root to open CMD-P/per-CPU counters and launches Redis and
Java as `yans3`. Authenticate interactively with `sudo`; never place a password
in a script, environment file, command log, or unit.

After frequency qualification, prepare the configuration and source hashes:

```sh
sudo python3 tools/testing/selftests/cmdp/scripts/redis-ycsb-fixedfreq.py prepare
"$study_root/bin/redis-ycsb-campaign" status
```

The generated wrapper is `$study_root/bin/redis-ycsb-campaign`. Its `suite` action
runs **11 pilots, followed by 27 measured runs**:

- B off/sampling/ARM and A off: four 40-second measurement pilots.
- B off: one full 600-second raw-export/heap pilot.
- C off: three paired repetitions with the frequency observer enabled/disabled,
  six 120-second pilots with alternating order.
- The 27-run A/B/C campaign, then raw analysis with eight jobs while no queries
  are running, followed by aggregate JSON/Markdown reporting.

Every pilot also uses eight YCSB workers, the 70k target, and 20-second warmup.
Pilots are separate validation evidence and never count toward the three measured
repetitions. The suite stops on failure; an incomplete pilot requires review.

A systemd transient unit survives SSH disconnects. Start the complete suite once:

```sh
sudo systemd-run --unit=cmdp-redis-fixedfreq --description='CMD-P Redis/YCSB' \
  --property=RemainAfterExit=yes /bin/sh -c \
  'exec /home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1/bin/redis-ycsb-campaign suite >>/home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1/suite.log 2>&1'
systemctl status cmdp-redis-fixedfreq
systemctl show cmdp-redis-fixedfreq -p Result -p ExecMainCode -p ExecMainStatus
"$study_root/bin/redis-ycsb-campaign" status
```

After pilot fixes, `prepare` may update the source freeze only before campaign
attempts exist; it refuses changed sources afterward. `run` and `resume` verify
frozen hashes and completed receipts. Incomplete measured attempts are preserved
under `failed-attempts/` and retried only after checks for surviving owned
processes. Do not start a second supervisor while one is active.

For targeted checks or recovery after reviewing a failure, these interfaces are
also available. Use unique pilot names and a durable launcher for long actions:

```sh
sudo "$study_root/bin/redis-ycsb-campaign" pilot --name extra-off-r1 \
  --workload B --mode off --warmup 20 --seconds 40
sudo "$study_root/bin/redis-ycsb-campaign" run
sudo "$study_root/bin/redis-ycsb-campaign" resume
sudo "$study_root/bin/redis-ycsb-campaign" analyze --directory "$study_root/campaign-v1/A-off-r1"
sudo "$study_root/bin/redis-ycsb-campaign" analyze --jobs 8
python3 tools/testing/selftests/cmdp/scripts/report-redis-ycsb-fixedfreq.py campaign \
  --directory "$study_root/campaign-v1" --output-dir "$study_root/analysis" --plots
```

The plot command needs matplotlib on the analysis host. Raw capture and numeric
analysis use Python's standard library. Plan for query/restore time plus raw-file
export and analysis; the export allowance is separately bounded at 30 minutes
per run.

## Interpretation and evidence

YCSB's raw recorder retains every latency in the JVM and exports afterward. Its
allocation/GC cost is part of **all three newly matched modes**; the earlier HDR
baseline is not their direct control. The 12 GiB heap and GC logs support checking
this overhead. No samples are discarded by latency magnitude.

The 70k setting is an aggregate target for a bounded-worker client. Fixed 2.1 GHz
or monitoring may prevent Redis from sustaining it. Keep the target, report
achieved QPS/shortfall, and show both operation latency and intended-start latency
(including delay against the request schedule). Compute p90, p99, p99.9, p99.99,
and p99.999 from all samples in the timestamp window, separately for reads,
updates, and combined operations. Preserve sample counts and per-repetition
values; extreme update tails in workload B have limited statistical support.

CMD-P snapshots surround status/batch/progress observations before STOP cleanup.
Report actual timestamps, request lateness, and snapshot spans alongside the
nominal 600-second latency window. A realtime/monotonic clock-offset change of
100 ms or more fails the run, protecting the raw-timestamp alignment. First ARM plus re-ARM counts are events, not
unique pages; WP counts are write-terminated ARM generations. `flush_calls`
counts shared flushes once. `/proc/interrupts` TLB deltas are separate, host-wide
shootdown interrupts, not per-process flush instructions.

The frequency observer stays on CPU24. Remote grouped counter reads still cause
kernel activity/IPIs on observed cores; they are not themselves TLB shootdowns.
Use identical collection in the compared modes and retain the collector-overhead
pilots. Busy-frequency stability checks require intervals of at least one second
and at least 5% busy time. Shorter intervals remain visible as diagnostics; close
boundary/periodic samples can exaggerate timing noise. All-core qualification
still uses one-second observations. These are interval averages; idle intervals
do not establish a loaded frequency, which is why qualification is required.

Each `campaign-v1/WORKLOAD-MODE-rN/` retains raw latency, YCSB/GC logs, clock and
boundary records, CMD-P/frequency JSONL, process observations, validated summaries,
and hash receipts. `capture-receipt.json` means captured/validated; `receipt.json`
means raw analysis/reporting completed. `campaign-state.json`, `suite-state.json`,
`suite.log`, and `status` identify progress. Findings and dated evidence belong in
the study's results folder; protocol and implementation notes belong in their respective
folders. See [measurement interfaces](../tools/testing/selftests/cmdp/MEASUREMENT-SUPPORT.md)
and the [baseline protocol](redis-ycsb-baseline.md) for supporting details.
