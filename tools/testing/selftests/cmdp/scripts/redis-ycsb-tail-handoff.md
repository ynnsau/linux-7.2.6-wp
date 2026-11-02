# Redis/YCSB matched tail-latency campaign

Run root: `/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1`.
Service: `cmdp-redis-tail-v1.service`. It runs on BMP independently of SSH and
is not enabled at boot. No kernel, GRUB, global THP or frequency-policy changes.

## Frozen experiment

- A: 66,000; B: 59,000; C: 58,000 aggregate target operations/second.
- Eight YCSB workers. OFF, sampling, dirty→ARM. Three repetitions: 27 accepted runs.
- 600 seconds measured; common warmup begins at 60 seconds.
- Redis CPU1/node0; clients CPUs26–33/node1; scanner CPU0; observer CPU24.
- Fixed requested 2.1 GHz policy on all 52 cores, boost off. Effective frequency
  variation remains reported; it is not a claim of constant effective clocks.
- Fresh golden ~16 GiB database each run; complete process THP disable and the
  previously verified full-dataset eligibility gate in every mode.
- Ten-second sampling period/quiet interval; dirty and ARM batches 512/512.
- Actual and intended-start raw latency, GC, RSS, placement, policy/frequency,
  ARM exposure, WP and monitor flush counters plus CPU TLB IPIs are retained.

## Warmup and validity

An ARM run must have a successful, completed ARM-bearing sweep before the
scheduled measurement boundary. Partial sweeps or completion after that boundary
do not qualify. If ARM is not ready, retain the entire attempt and repeat all
three modes for that workload/repetition with common warmup 120, 240, then 480
seconds. Failure at the cap stops the campaign. Kernel/controller errors do not
trigger an automatic warmup retry. No QPS target is changed automatically.

Only complete matched blocks enter the accepted `campaign-v1` view. Mode and
workload order rotate between repetitions. Interrupted/incomplete attempts remain
preserved; resume validates configuration, source hashes, receipts and process
identities before starting afresh. Analysis follows capture with no live workload.

Throughput below 99% of its workload's target is a diagnostic flag. A target met
on average does not establish absence of queueing: inspect intended-start latency
and GC alongside actual latency. Saturated comparisons do not isolate direct
sampling/ARM latency cost.

## Operator commands on BMP

```sh
# Check service and durable progress without starting work.
systemctl --no-pager --full status cmdp-redis-tail-v1.service
python3 /home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1/source/scripts/redis-ycsb-tail-campaign.py status

# Follow the durable log; Ctrl-C only exits this viewer.
tail -f /home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1/launch.log

# Stop gracefully; allow monitor detach and owned-process cleanup.
sudo systemctl stop cmdp-redis-tail-v1.service

# Start or resume using frozen sources and existing evidence.
sudo systemctl start cmdp-redis-tail-v1.service
```

Do not run a second benchmark, restore Redis manually, change frequency/NUMA/THP
policies, or edit the frozen source/configuration during the campaign. The shared
experiment lock prevents concurrent cooperating study controllers. The service
does not restart automatically after an error: inspect the state and logs first.
After stopping, verify `entries`, `managed`, `isolated` and `waiters` are zero in
`sudo cat /sys/kernel/debug/cmdp/stats` and check run `cleanup.json`.

For initial installation only, run the repository's
`redis-ycsb-tail-service.sh install` with sudo, then `... start`. Installation
refuses to overwrite an existing root or unit. The service timeout allows five
minutes for orderly cleanup before systemd kills remaining owned processes.
No automatic restart after reboot is configured.
