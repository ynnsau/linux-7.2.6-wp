# Redis/YCSB baseline on BMP

This experiment measures sustained logical YCSB operations/s on a single Redis core, with clients on the other socket and CMD-P sampling/ARM inactive. It preserves standard A/B/C record and access semantics and uses three measured repetitions per configuration.

## Reproduce

The target host is BMP (`yans3`, `/home/yans3/cmdp-v2/redis-ycsb-study`). It needs Java 11, GCC, make, curl, numactl and Tcl 8.6. Run the setup script as yans3; it builds Redis 8.10.3 core with jemalloc and YCSB commit `ae02b31fa7784b3892f121c06638aa0cd972fa06` using Maven 3.10.0. Runtime sources remain unchanged. YCSB's old TestNG 6.1.1 requires Surefire 2.22.2: packaging installs the reactor with execution skipped, then the explicitly pinned runner executes all tests.

```sh
bash tools/testing/selftests/cmdp/scripts/setup-redis-ycsb.sh
cd /home/yans3/cmdp-v2/redis-ycsb-study/src/redis
./runtest --single unit/type/hash --single unit/type/zset --single integration/rdb --clients 4
sudo python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py smoke
sudo python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py prepare
sudo python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py campaign
```

The supervisor is root for read-only CMD-P/host checks and launches Redis/YCSB under yans3. It never opens a CMD-P device or writes debugfs. Run the campaign through a durable launcher and save its exit status; it verifies completed receipts on resume and refuses incomplete run directories. The prepared software manifest and validation evidence must accompany the campaign.

## Data and isolation

Redis listens only at 127.0.0.1:6380. Every Redis thread uses CPU1 with memory node0. The JVM uses CPUs26–33 and memory node1; the controller/observer uses CPU24. Redis has `io-threads 1`, `jemalloc-bg-thread no`, active defrag off, no eviction or TTLs, and no ongoing persistence. Existing host frequency governor, NUMA balancing and THP policies stay fixed. Java uses a 4 GiB G1 heap, explicit processor-count matching the CPU pool, and no huge pages.

Keys retain YCSB's variable-length `user` plus hashed numeric suffix. Each record has ten 100-byte fields. Reads retrieve all fields; updates change one. A=50/50 read/update, B=95/5, C=100/0, all Zipfian. Keep the binding's `_indices` sorted set.

Preparation calibrates the record count to 16 GiB ±5% restored Redis `used_memory`, including records/index/Redis-accounted overhead; RSS is separate. A synchronous `SAVE` outside measurements creates a golden RDB. Validate it with `redis-check-rdb` and preserve its SHA256 and sampled full-record fingerprints. Every run restarts from that same file. `save ""`, `appendonly no` and `SHUTDOWN NOSAVE` prevent A/B updates from replacing it. Restore time is excluded from QPS; filesystem caches are not dropped.

Later, reload the database without YCSB:

```sh
sudo python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py restore
# Redis now runs on CPU1 at 127.0.0.1:6380, without a CMD-P monitor.
sudo python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/redis-ycsb-study.py stop
```

## Timing and selection

Each JVM runs 153 s, with a selected 30–150 s window and a 3 s end margin. The first 30 s remain in the same JVM for JIT/cache warm-up. No rate target or added pipelining. Use status-count differences and timestamp differences for successful logical ops/s; require all returns OK. Merge only matching one-second HDR intervals for latency, allowing 100 ms to align histogram rotation after status-count sampling. Independently compare histogram counts with the status-count window. Do not average percentiles. The synthetic HDR fixture verifies warm-up exclusion.

Sweep 1, 2, 4, 8, 16, 32, 64 workers with three repetitions and rotated workloads/concurrency. Extend 128/256 unless the last two doublings each improve the best median by<3%. If Redis remains<90% of its core while client-pool utilization exceeds80%, expand the pool to 16 then 24 cores on node1 and repeat the winning/adjacent worker counts. Report highest median, all repetitions, and the lowest worker count within 3% of peak. A server ceiling requires demonstrated saturation; otherwise call it maximum observed for this setup.

Admission requires correct affinity for every thread, zero errors/eviction/expiration/swapping/persistence, unchanged CMD-P counters and unchanged host policy. Record per-second CPU, memory, frequency, operation counts and latency histograms; read NUMA maps/smaps outside the selected window. Receipts hash finalized logs after Redis shutdown.

## Analysis and artifacts

Large RDB remains on BMP under `artifacts/golden`; its manifest travels with the evidence. Campaign data is under `artifacts/baseline-v1`. Analyze a complete copy with:

```sh
python3 tools/testing/selftests/cmdp/scripts/analyze-redis-ycsb.py CAMPAIGN_DIRECTORY OUTPUT_DIRECTORY --plots
```

The analyzer independently verifies receipts, operation arithmetic, errors, monitoring inactivity and 3 repetitions; it produces JSON summaries and throughput, CPU/latency and timeline figures. Plans/setup go in the study's experiments folder; measured findings and evidence go in results. No kernel/GRUB/reboot change is part of this experiment.

## Completed baseline

The 2026-10-28 campaign completed all 63 measured runs. See [the measured results](redis-ycsb-baseline-results.md) for peak QPS, repetitions, saturation, latency, GC, and snapshot restoration. Eight YCSB workers on eight client cores were within 0.25% of all observed peaks.

Read durable campaign progress without contacting Redis:

```sh
python3 /home/yans3/cmdp-v2/redis-ycsb-study/tools/status-redis-ycsb.py
```

The status helper reports the current phase, latest completed run, CPU/affinity/memory observations and launcher exit status. It does not modify the campaign or start database activity.
