# Redis dataset eligibility verification

The production CMD-P kernel is unchanged. `cmdp_exec -- COMMAND...` disables THP
completely before exec and checks the setting. The runner uses it identically
for OFF, sampling and dirty→ARM. Redis's own `disable-thp yes` did not set the
process flag under BMP's global `madvise` policy.

Build and run launcher tests:

```sh
make -C tools/testing/selftests/cmdp cmdp_exec
python3 tools/testing/selftests/cmdp/scripts/test-cmdp-exec.py
python3 tools/testing/selftests/cmdp/scripts/test-redis-ycsb-eligibility.py
python3 tools/testing/selftests/cmdp/scripts/test-redis-ycsb-eligibility-integration.py
python3 tools/testing/selftests/cmdp/scripts/test-redis-ycsb-verify.py
```

From the repository, with the existing qualified 2.1 GHz policy still applied:

```sh
sudo python3 tools/testing/selftests/cmdp/scripts/redis-ycsb-fixedfreq.py verify-eligibility \
  --output-root /home/yans3/cmdp-v2/redis-ycsb-study/eligibility-v1 \
  --frequency-profile-root /home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1 \
  --frequency-contract fixed-policy --target-qps 50000
```

Use a new output root for a new attempt. Frozen helpers, binaries and source
hashes prevent a changed implementation from silently resuming existing evidence.
The old fixedfreq-v1 campaign remains read-only. The shared experiment lock is
in the frequency-profile root, serializing verification with frequency control.
No setting is changed by this command: it verifies the qualified boot, all 52
policies, and zero initial CMD-P ownership.

Before each run, restore the golden 11.5M-record RDB, validate its fingerprints,
then collect status, full smaps and one completed inventory sweep on CPU0.
Eligibility requires THP_enabled=0, zero AnonHugePages/Swap, no THP-policy skips,
and at least 90% of both Redis used_memory and independently summed writable
private anonymous resident bytes. Inventory must perform no dirty resets, ARM,
WP or protection flush, and stop with zero residual ownership. The 90% rule is
a dataset eligibility sanity check, not an ARM coverage target.

The command first demonstrates at least 1 GiB of idle live ARM, updates 1,024
existing fields and checks positive WP activity plus readback. It then performs
one B/OFF, B/sampling and B/ARM check, each with a fresh restore, eight workers,
50k target, 20 seconds warmup and 120 seconds observation. Redis is on CPU1/node0,
clients on CPUs26–33/node1, scanner on CPU0 and supervisor on CPU24. Sampling
period/quiet interval are 10s; dirty/WP batches are 512/512. All raw actual and
intended latency records, GC logs, inventory and monitor evidence are retained.
No minimum live ARM coverage is imposed on the active B workload.

Review `verify-state.json`, `verify-summary.json`, `idle-arm-v1/`, and `smoke-v1/`.
A failure stops the sequence and preserves its evidence. This is one functional
check per mode, not a three-repetition latency comparison. No kernel rebuild,
GRUB edit, reboot, global THP update or frequency-policy update is involved.
