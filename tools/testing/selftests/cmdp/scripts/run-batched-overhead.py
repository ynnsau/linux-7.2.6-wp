#!/usr/bin/env python3
"""Repeat the archived 144-run matched campaign on the validated batched kernel.

Reuse the exact old work and schedule; never resize based on new runtime. The
shared executor, timing, RSS sampling and per-run guards are unchanged. Adds a
native batching fixture and separate full-Twitter dirty-qualify correctness.
"""
import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import signal
import sys

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('batched_matched_helpers', str(SCRIPTS / 'run-matched-gapbs.py'))
matched = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matched)
matched.KERNEL_COMMIT = '2919ec3ef02fe690a51a1bfded34845d9ce1eb10'
matched.prepare.RELEASE = '7.2.6-cmdp-batch-production'
FROZEN_SHA256 = '51409f90a551bb349b45cbe355031628976a6c95215c39fbade8e18020a32b74'


class Campaign(matched.Campaign):
    def preflight(self):
        super().preflight()
        ref = self.a.reference
        frozen_path = ref / 'frozen-work.json'
        assert matched.sha(frozen_path) == FROZEN_SHA256
        previous = json.loads((ref / 'campaign-summary.json').read_text())
        assert previous['pass'] and previous['measured_runs'] == 144
        baseline = json.loads((ref / 'host-initial.json').read_text())
        for key in ('numa_balancing', 'taint', 'settings', 'governors'):
            assert self.before[key] == baseline[key], ('Prior host control changed', key)
        old_provenance = json.loads((ref / 'provenance.json').read_text())
        current = json.loads((self.base / 'provenance.json').read_text())
        for key in ('graph_sha256', 'gapbs_commit', 'binary_sha256'):
            assert current[key] == old_provenance[key], key
        assert current['source_sha256']['cmdp_monitor.c'] == old_provenance['source_sha256']['cmdp_monitor.c']
        # Preserve proof of both wrapper and reused helpers independently of
        # the unchanged base executor's provenance schema and resume checks.
        sources = [Path(__file__), SCRIPTS / 'prepare-batched-overhead.py']
        for source in sources:
            dest = self.base / 'source' / source.name
            if dest.exists():
                assert matched.sha(dest) == matched.sha(source)
            else:
                dest.write_bytes(source.read_bytes())
        provenance = dict(reference=str(ref), frozen_work_sha256=FROZEN_SHA256,
            source_sha256={p.name: matched.sha(p) for p in sources},
            controls_match_reference=True, shared_timing_executor_unchanged=True,
            work_and_schedule_reused=True, new_sizing=False,
            reference_monitor_sha256=old_provenance['monitor_sha256'],
            monitor_sha256=current['monitor_sha256'])
        path = self.base / 'batched-provenance.json'
        if path.exists():
            assert json.loads(path.read_text()) == provenance
        else:
            matched.save(path, provenance)

    def gates(self):
        super().gates()
        self.tap('batch', 'cmdp_batch_test', 261)
        # The old campaign inherited these checks from earlier runs on its
        # boot. Repeat explicitly for this newly booted implementation.
        for app, threads in matched.CELLS:
            self.workload('correctness/{}-t{}-C'.format(app, threads),
                          app, threads, 'C', 1, verify=True)

    def freeze(self):
        source = self.a.reference / 'frozen-work.json'
        assert matched.sha(source) == FROZEN_SHA256
        frozen = json.loads(source.read_text())
        assert len(frozen['schedule']) == 144 and frozen['counts_frozen_before_measurement']
        dest = self.base / 'frozen-work.json'
        if dest.exists():
            assert dest.read_bytes() == source.read_bytes()
        else:
            dest.write_bytes(source.read_bytes())
        matched.save(self.base / 'reused-work.json', dict(
            source=str(source), sha256=FROZEN_SHA256, new_pilots=False,
            prior_sizing_fields_are_historical=True, counts_and_order_unchanged=True))
        return frozen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-rearm-recovery'))
    p.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--prior', type=Path, required=True, help='Fresh batched-kernel postboot audit/tools')
    p.add_argument('--reference', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    p.add_argument('--resume', action='store_true')
    a = p.parse_args()
    assert a.base.is_dir() and sys.version_info >= (3, 6)
    with (a.base / 'campaign.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(signum, frame):
            raise RuntimeError('Driver interrupted by signal ' + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        campaign = Campaign(a)
        try:
            campaign.run()
        except BaseException as exc:
            campaign.failure(exc)
            raise


if __name__ == '__main__':
    main()
