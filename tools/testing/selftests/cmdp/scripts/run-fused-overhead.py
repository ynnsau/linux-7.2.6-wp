#!/usr/bin/env python3
"""BFS-only matched three-repeat overhead campaign after user activation.

One cap pair per campaign. Reuse the old Twitter work and launcher/measurement
path; do not resize work from new runtimes. No host setting changes.
"""
import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import signal

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fused_matched_helpers', str(SCRIPTS / 'run-matched-gapbs.py'))
matched = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matched)
matched.CELLS = [('bfs', 1), ('bfs', 4)]
matched.HISTORY_BYTES_PER_PAGE = 160
matched.prepare.RELEASE = '7.2.6-cmdp-fused-production'
FROZEN_SHA256 = '51409f90a551bb349b45cbe355031628976a6c95215c39fbade8e18020a32b74'
BUILD_RECORD = Path('/home/yans3/cmdp-v2/periodic/fused-scanner-20261022/records/build-verification.json')


def ceiling(value):
    n = int(value)
    if not 1 <= n <= 65536:
        raise argparse.ArgumentTypeError('batch ceiling must be 1..65536 pages')
    return n


def plan(previous, dirty, wp):
    assert 1 <= dirty <= 65536 and 1 <= wp <= 65536
    work = {cell: previous['cells'][cell] for cell in ('bfs-t1', 'bfs-t4')}
    assert work['bfs-t1']['trials'] == 38 and work['bfs-t4']['trials'] == 127
    schedule = []
    for block, order in enumerate(('ABC', 'BCA', 'CAB'), 1):
        cells = ('bfs-t1', 'bfs-t4') if block % 2 else ('bfs-t4', 'bfs-t1')
        for cell in cells:
            for mode in order:
                schedule.append(dict(block=block, cell=cell, mode=mode))
    return dict(cells=work, schedule=schedule, repetitions=3, counts_frozen_before_measurement=True,
                period_ms=10000, quiet_ms=10000, poll_ms=1000, max_pages=8388608, max_history=8388608,
                dirty_batch_pages=dirty, wp_batch_pages=wp, reference_work_sha256=FROZEN_SHA256,
                new_sizing=False, target_seconds_historical=120, whole_trials=True)


class Campaign(matched.Campaign):
    def controller_args(self, mode, generated=False):
        argv = super().controller_args(mode, generated)
        if mode != 'A':
            argv += ['--dirty-batch-pages', str(self.a.dirty_batch_pages),
                     '--wp-batch-pages', str(self.a.wp_batch_pages)]
        return argv

    def preflight(self):
        super().preflight()
        ref = self.a.reference
        assert matched.sha(ref / 'frozen-work.json') == FROZEN_SHA256
        reference = json.loads((ref / 'provenance.json').read_text())
        current = json.loads((self.base / 'provenance.json').read_text())
        for key in ('graph_sha256', 'gapbs_commit'):
            assert current[key] == reference[key], key
        assert current['binary_sha256']['bfs'] == reference['binary_sha256']['bfs']
        baseline = json.loads((ref / 'host-initial.json').read_text())
        for key in ('numa_balancing', 'taint', 'settings', 'governors'):
            assert self.before[key] == baseline[key], key
        sources = [Path(__file__), SCRIPTS / 'prepare-fused-overhead.py']
        provenance = dict(reference=str(ref), source_sha256={p.name: matched.sha(p) for p in sources},
            dirty_batch_pages=self.a.dirty_batch_pages, wp_batch_pages=self.a.wp_batch_pages,
            repetitions=3, measured_runs=18, unchanged_timing_executor=True)
        for source in sources:
            dest = self.base / 'source' / source.name
            if dest.exists():
                assert matched.sha(dest) == matched.sha(source)
            else:
                dest.write_bytes(source.read_bytes())
        path = self.base / 'fused-provenance.json'
        if path.exists():
            assert json.loads(path.read_text()) == provenance
        else:
            matched.save(path, provenance)

    def validate_controller(self, path, mode, before, require_arm, minimum=1):
        result, timing = super().validate_controller(path, mode, before, require_arm, minimum)
        records = matched.rows(path / 'monitor.jsonl')
        batches = [r for r in records if r.get('event') == 'batch_status']
        if mode == 'A':
            assert not batches
        else:
            assert batches, 'New batch diagnostics required'
            for b in batches:
                assert b['dirty_batch_pages'] == self.a.dirty_batch_pages
                assert b['wp_batch_pages'] == self.a.wp_batch_pages
                assert b['flush_calls'] == b['shared_flush_calls'] + b['dirty_only_flush_calls'] + b['wp_only_flush_calls']
                assert b['max_dirty_pages'] <= self.a.dirty_batch_pages
                assert b['max_wp_pages'] <= self.a.wp_batch_pages
                assert b['max_span_pages'] <= max(self.a.dirty_batch_pages, self.a.wp_batch_pages)
                assert b['history_allocated_bytes'] >= b['history_payload_bytes']
            assert batches[-1]['history_payload_bytes'] == result['dirty']['metadata_bytes']
            result['batch'] = batches[-1]
            matched.save(path / 'validation.json', result)
        return result, timing

    def gates(self):
        super().gates()
        self.tap('batch', 'cmdp_batch_test', 261)
        for app, threads in matched.CELLS:
            self.workload('correctness/{}-t{}-C'.format(app, threads), app, threads, 'C', 1, verify=True)

    def freeze(self):
        path = self.a.reference / 'frozen-work.json'
        assert matched.sha(path) == FROZEN_SHA256
        frozen = plan(json.loads(path.read_text()), self.a.dirty_batch_pages, self.a.wp_batch_pages)
        dest = self.base / 'frozen-work.json'
        if dest.exists():
            assert json.loads(dest.read_text()) == frozen
        else:
            matched.save(dest, frozen)
        return frozen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path('/home/yans3/cmdp-v2/linux-fused-scanner'))
    p.add_argument('--gapbs', type=Path, default=Path('/home/yans3/cmdp-v2/gapbs-study/gapbs'))
    p.add_argument('--prior', type=Path, required=True)
    p.add_argument('--reference', type=Path, default=Path('/home/yans3/cmdp-v2/periodic/matched-gapbs-20261022-v1'))
    p.add_argument('--dirty-batch-pages', type=ceiling, default=64)
    p.add_argument('--wp-batch-pages', type=ceiling, default=64)
    p.add_argument('--resume', action='store_true')
    a = p.parse_args()
    record = json.loads(BUILD_RECORD.read_text())
    assert record['pass'] and record['release'] == matched.prepare.RELEASE
    matched.KERNEL_COMMIT = record['source_freezes']['fixed']['commit']
    assert a.base.is_dir()
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
