# Redis/YCSB study support scripts

These are exact copies of the standalone BMP helpers used to validate and archive
Redis eligibility and launch the matched tail-latency campaign. Their original
paths, sizes and SHA-256 hashes are recorded in `origin-manifest.json`.
The active campaign runs its separately frozen source bundle; this archive does
not alter its scripts, configuration, evidence or service.

| Script | Purpose |
| --- | --- |
| `archive-eligibility-evidence-20261101.py` | Archive completed/failed eligibility-v1 metadata, excluding raw latency, and create a file manifest. |
| `archive-eligibility-evidence-v2-20261101.py` | Archive a specified completed/failed eligibility root and create a file manifest. |
| `collect-eligibility-final-state-20261101.py` | Capture eligibility-v2 machine, cleanup, frequency and source state. |
| `validate-tail-20261102.py` | Run the eight userspace test suites and syntax/diff checks used before tail-campaign launch. |
| `collect-tail-launch-20261102.py` | Capture the live tail-campaign service, source hashes, policies and first-run metadata on observer CPU24. |

These files preserve the original dated output paths and BMP assumptions. They
are historical reproduction helpers, not safe idempotent status commands:
collectors overwrite their snapshot JSON, validation writes its dated test logs,
and archivers produce tarballs/manifests. Choose new output paths before reusing
one against preserved evidence. Run validation and archive work after live
benchmark capture, rather than adding load during measurements. Collectors
reading debugfs, MSRs or kernel logs require the corresponding privileges.

For normal status, stop and resume commands, use the
[tail campaign handoff](../../tools/testing/selftests/cmdp/scripts/redis-ycsb-tail-handoff.md).
The [eligibility handoff](../../tools/testing/selftests/cmdp/scripts/redis-ycsb-eligibility.md)
describes the preceding bounded verification.

Validation for this archival commit: all five files parsed with BMP's Python3.6
and matched their original bytes. No helper or benchmark was executed.
