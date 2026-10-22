# Native recovery experiment readiness

These tools are prepared for **after user activation** of
`7.2.6-cmdp-rearm-production`. They do not update GRUB, reboot, install a kernel,
change host settings, or automatically start a workload during preparation.
Historical dirty-kernel runners keep their original defaults and archives.

The new scripts require the exact validated kernel implementation commit,
production `bzImage` SHA-256, running GNU build ID, and clean source HEAD.
The build ID must match `/sys/kernel/notes`, not merely the filename in `/boot`.
The passing installation-verification receipt, its host-after boot-file hashes,
and the checksum-linked build-verification receipt must bind `/boot/vmlinuz-7.2.6-cmdp-rearm-production`
to the same hash. Obtain these values from the final build/installation evidence;
do not substitute the previous dirty-production kernel's receipt.

## Preparation and gates

`prepare-recovery-native.py --help` lists the required identity arguments and
paths. Select a new `--base` directory. Preparation validates current release,
build identity, boot ID, ownership cleanup, clean source, same stock GAPBS source,
BFS/PR binaries and the exact directed Twitter dimensions. It hashes the complete
Twitter graph and requires the original study SHA-256
`4b7e5193a8e3384b8319359dc8c92534b8aee8e48d6c3bd0820451bb307d6eea`. It compiles seven fresh tools, including `cmdp_recovery_test`.
The final `postboot-audit.json` has `recovery_ready=true` only after all work passes.
A prior boot's audit or fixture receipt is never reusable.

Example sequence after setting the identity variables to recorded final values:

```sh
cmdp_src=/home/yans3/cmdp-v2/linux-rearm-recovery
cmdp_scripts="$cmdp_src/tools/testing/selftests/cmdp/scripts"
cmdp_run=/home/yans3/cmdp-v2/periodic/recovery-native-NEW-RUN
# Set cmdp_kernel_commit, cmdp_image_sha, cmdp_build_id, cmdp_install_record and cmdp_build_record
# from the final validation/installation receipt before running this block.
cmdp_source_head=$(git -C "$cmdp_src" rev-parse HEAD)
sudo python3 "$cmdp_scripts/prepare-recovery-native.py" \
  --base "$cmdp_run" --source "$cmdp_src" \
  --kernel-commit "$cmdp_kernel_commit" --kernel-sha256 "$cmdp_image_sha" \
  --kernel-build-id "$cmdp_build_id" --installation-record "$cmdp_install_record" \
  --build-record "$cmdp_build_record" \
  --expected-source-commit "$cmdp_source_head" || exit 1

cmdp_phase() {
  sudo python3 "$cmdp_scripts/run-recovery-native.py" "$@" \
    --base "$cmdp_run" --source "$cmdp_src" \
    --kernel-commit "$cmdp_kernel_commit" --kernel-sha256 "$cmdp_image_sha" \
    --kernel-build-id "$cmdp_build_id" --expected-source-commit "$cmdp_source_head"
}
cmdp_phase small || exit 1
for cmdp_app in bfs pr; do
  for cmdp_threads in 1 4; do
    cmdp_phase twitter-verify --application "$cmdp_app" --threads "$cmdp_threads" || exit 1
  done
done
for cmdp_threads in 1 4; do
  cmdp_phase twitter-sizing --application bfs --threads "$cmdp_threads" || exit 1
done
for cmdp_app in bfs pr; do
  for cmdp_threads in 1 4; do
    cmdp_phase twitter-observe --application "$cmdp_app" --threads "$cmdp_threads" || exit 1
  done
done
python3 "$cmdp_scripts/analyze-recovery-native.py" "$cmdp_run"
```

Every phase checks the same running build ID, boot, image hash, source HEAD,
userspace binary/source hashes, full graph hash and zero outstanding ownership.
It preserves host NUMA, THP and watchdog settings, checks kernel log continuity
and aborts on kernel warnings or cleanup failures. It inherits the tested
controller/process-group timeout and cancellation behavior of the existing
runner. The small phase runs legacy, monitor, dirty and recovery fixtures, then
eight verified generated BFS/PR trials for one/four threads. Native recovery
fixtures do not enable the guest-only automatic NUMA tuning suite.

## Fixed workload policy

All Twitter runs use the existing 61,578,415-vertex, 1,468,364,884-edge directed
graph. Q=10 seconds, sweep period=10 seconds, polling=1 second, target-local THP
disabled, OpenMP dynamic disabled, and singleton worker placement on CPUs 0–3
remain fixed. The controller observes process launch through exit and STOP,
including loading and algorithm initialization; the graph algorithms are stock.

Full Twitter verification is separate from observation. BFS uses 16/36 complete
pilot trials for one/four threads and selects the nearest positive whole trial
count to approximately 240 seconds total elapsed, accounting for measured
non-trial time. That count is stored in an exclusively created frozen-selection
file. PR uses one complete converged trial for one thread and four for four
threads, with `-i 1000 -t 1e-4 -l`. Actual durations may differ from four minutes.
No algorithm is stopped midway to make the timeline reach a convenient duration.

Failures keep their evidence and prevent advancing to another phase. Existing
phase directories and trial selections are never silently overwritten. A new
experiment base is required for a deliberate fresh attempt.

## Interpretation and reports

`analyze-recovery-native.py` requires all four completed observation cells and
validates new diagnostic accounting as well as existing generation conservation.
It emits JSON/Markdown and a PNG/PDF/SVG figure with live ARM, ARM/WP/invalidation
rates, source-specific invalidation rates and sampling-rejection rates. Rates use
actual sample intervals; logarithmic scaling above one event per second keeps
small WP counts visible beside large admission/invalidation counts. Cleanup
samples and the STOP boundary are marked separately.

Protected GiB-seconds comes from kernel transition accounting. ARM admissions
are generations, not unique physical pages. Invalidation labels identify the
callback path, not necessarily its ultimate cause. A zero-ARM span means zero
at consecutive samples; the report additionally records CPU tick increases in
fully enclosed sampling intervals. It cannot exclude intermediate ARM episodes
or identify whether CPU work was graph loading, algorithm computation or teardown.

No matched unmonitored baseline is collected by this protocol. It does not claim
end-to-end instrumentation overhead or compare unequal BFS/PR work as a speedup.
The CMD-P write-helper timing remains a component measurement, not total fault or
application overhead.
