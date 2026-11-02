#!/usr/bin/env python3
"""Matched Redis tail-latency campaign; Python 3.6+, root, no reboot changes.

Run this from the frozen source/scripts directory. SIGTERM gracefully unwinds
the current capture. Resume preserves interrupted attempts and checks ownership.
"""
import argparse
import fcntl
import importlib.util
import json
import os
import pathlib
import signal
import socket
import sys
import traceback

P = pathlib.Path
DEFAULT_ROOT = P('/home/yans3/cmdp-v2/redis-ycsb-study/tail-latency-v1')
DEFAULT_PROFILE = P('/home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1')
TARGETS = {'A': 66000, 'B': 59000, 'C': 58000}
MODES = ['off', 'sampling', 'arm']
WARMUPS = [60, 120, 240, 480]


def read(path):
    return json.loads(P(path).read_text())


def schedule():
    workloads = ['A', 'B', 'C']
    for rep in range(3):
        for workload in workloads[rep:] + workloads[:rep]:
            yield {'id': '%s-r%d' % (workload, rep + 1), 'workload': workload,
                   'repetition': rep + 1, 'modes': MODES[rep:] + MODES[:rep]}


def configure(root, profile):
    directory = P(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('tail_fixedfreq', str(directory / 'redis-ycsb-fixedfreq.py'))
    f = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = f
    spec.loader.exec_module(f)
    f.ROOT = P(root).resolve()
    f.FREQUENCY_ROOT = P(profile).resolve()
    assert f.ROOT != f.LEGACY_ROOT and f.ROOT != f.FREQUENCY_ROOT
    f.SCRIPTS = directory
    f.ELIGIBILITY_REQUIRED = True
    f.HELPER = f.ROOT / 'tools/redis-ycsb-study.py'
    f.CONFIG.update(version=3, tail_latency_campaign=True, eligibility_required=True,
                    frequency_contract='fixed-policy', target_qps=TARGETS['A'],
                    target_qps_by_workload=TARGETS, warmup_seconds=WARMUPS[0],
                    warmup_ladder_seconds=WARMUPS, measure_seconds=600,
                    repetitions=3, readiness_required_for_arm=True,
                    matched_block='workload/repetition', retry_whole_matched_block=True)
    original_sources = f.selected_sources

    def sources():
        values = original_sources()
        # Hash the entire frozen Python source bundle, including this driver.
        values.update({str(p.resolve()): f.sha(p) for p in directory.glob('*.py')})
        return values
    f.selected_sources = sources
    return f


def state(f, status, **values):
    values.update(at=f.now(), status=status, supervisor=f.process_identity(os.getpid()))
    f.save(f.ROOT / 'tail-state.json', values)
    f.emit('tail_state', **values)


def machine_identity(f):
    return {'hostname': socket.gethostname(), 'kernel': os.uname().release,
            'boot_id': P('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'online_cpus': P('/sys/devices/system/cpu/online').read_text().strip(),
            'selected_khz': f.frequency_policy()}


def require_dead(f, identities):
    for identity in identities:
        try:
            current = f.process_identity(identity['pid'])
        except (FileNotFoundError, ProcessLookupError):
            continue
        assert current != identity, 'Prior attempt still owns live process: %s' % identity


def check_attempt_cleanup(f, attempt):
    for path in P(attempt).glob('*/processes.json'):
        ids = read(path)
        require_dead(f, [ids[k] for k in ('redis', 'ycsb') if k in ids]
                     + list(ids.get('controls', {}).values()))
        cleanup = path.parent / 'cleanup.json'
        # A hard interruption may prevent a receipt; dead identities and idle
        # global ownership are still mandatory before a fresh retry.
        if cleanup.exists():
            assert read(cleanup)['passed'], 'Failed cleanup requires inspection: ' + str(cleanup)
    f.idle_cmdp()


def require_idle_processes(f):
    f.idle_cmdp()
    found = []
    for path in P('/proc').glob('[0-9]*/cmdline'):
        try:
            words = path.read_bytes().split(b'\0')
            name = P(os.fsdecode(words[0])).name if words and words[0] else ''
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if name in ('redis-server', 'java', 'cmdp_monitor', 'frequency_collect') or name.startswith('redis-server '):
            found.append({'pid': int(path.parent.name), 'name': name})
    assert not found, 'Campaign requires no existing Redis/Java/CMDP/frequency collectors: %s' % found


def validate_frozen(f):
    assert read(f.ROOT / 'config.json') == f.CONFIG, 'Frozen campaign configuration changed'
    assert read(f.ROOT / 'source-manifest.json')['hashes'] == f.selected_sources(), 'Frozen campaign sources changed'
    assert read(f.ROOT / 'machine.json') == machine_identity(f), 'Machine identity or frequency policy changed'
    assert os.uname().release == f.CONFIG['kernel']


def validate_run(f, directory, block, mode, warm):
    f.verify_capture_receipt(directory)
    run = read(directory / 'run.json')
    expected = {'workload': block['workload'], 'mode': mode, 'warmup_seconds': warm,
                'measure_seconds': 600, 'workers': 8,
                'target_qps': TARGETS[block['workload']], 'frequency_contract': 'fixed-policy'}
    assert all(run.get(k) == v for k, v in expected.items()), 'Capture does not match the frozen block'
    assert read(directory / 'eligibility.json')['passed'], 'Eligibility did not pass'
    assert read(directory / 'cleanup.json')['passed'], 'Cleanup did not pass'
    if mode == 'arm':
        ready = read(directory / 'readiness.json')
        assert ready['required'] and ready['passed'] and not ready.get('retryable', False), 'ARM readiness did not pass'


def selected(f):
    path = f.ROOT / 'selection.json'
    return read(path) if path.exists() else {'version': 1, 'blocks': {}}


def promote(f, block, attempt, warm):
    entries = {}
    for mode in block['modes']:
        name = '%s-%s-r%d' % (block['workload'], mode, block['repetition'])
        directory = attempt / name
        validate_run(f, directory, block, mode, warm)
        entries[mode] = name
    # Validate the whole matched block before exposing any canonical run.
    for mode, name in entries.items():
        directory = attempt / name
        dest = f.ROOT / 'campaign-v1' / name
        if dest.exists() or dest.is_symlink():
            assert dest.is_symlink() and dest.resolve() == directory.resolve(), 'Conflicting accepted run: ' + str(dest)
        else:
            dest.symlink_to(os.path.relpath(str(directory), str(dest.parent)), target_is_directory=True)
    result = selected(f)
    result['blocks'][block['id']] = dict(block, attempt=str(attempt.relative_to(f.ROOT)),
                                        warmup_seconds=warm, runs=entries)
    f.save(f.ROOT / 'selection.json', result)
    metadata = read(attempt / 'attempt.json')
    metadata.update(status='accepted', completed=f.now())
    f.save(attempt / 'attempt.json', metadata)
    return result['blocks'][block['id']]


def verify_selection(f, block, entry):
    assert entry['workload'] == block['workload'] and entry['repetition'] == block['repetition']
    assert entry['warmup_seconds'] in WARMUPS and set(entry['runs']) == set(MODES)
    attempt = f.ROOT / entry['attempt']
    assert attempt.resolve().parent == (f.ROOT / 'attempts' / block['id']).resolve()
    for mode in block['modes']:
        name = '%s-%s-r%d' % (block['workload'], mode, block['repetition'])
        assert entry['runs'][mode] == name
        dest = f.ROOT / 'campaign-v1' / name
        assert dest.is_symlink() and dest.resolve() == (attempt / name).resolve()
        validate_run(f, dest, block, mode, entry['warmup_seconds'])
    check_attempt_cleanup(f, attempt)


def run_block(f, block):
    existing = selected(f)['blocks'].get(block['id'])
    if existing:
        verify_selection(f, block, existing)
        return
    parent = f.ROOT / 'attempts' / block['id']
    parent.mkdir(parents=True, exist_ok=True)
    attempts = sorted(parent.glob('a*-w*'))
    warm = WARMUPS[0]
    if attempts:
        latest = attempts[-1]
        check_attempt_cleanup(f, latest)
        metadata = read(latest / 'attempt.json')
        warm = metadata['warmup_seconds']
        assert warm in WARMUPS
        if metadata['status'] in ('captured', 'accepted'):
            promote(f, block, latest, warm)
            return
        if metadata['status'] == 'warmup_insufficient':
            index = WARMUPS.index(warm) + 1
            assert index < len(WARMUPS), 'ARM readiness failed at maximum common warmup; inspect before any new campaign'
            warm = WARMUPS[index]
    number = len(attempts) + 1
    while True:
        attempt = parent / ('a%02d-w%03d' % (number, warm))
        attempt.mkdir()
        metadata = dict(block, status='running', warmup_seconds=warm, started=f.now())
        f.save(attempt / 'attempt.json', metadata)
        try:
            for mode in block['modes']:
                name = '%s-%s-r%d' % (block['workload'], mode, block['repetition'])
                state(f, 'capturing', block=block['id'], run=name,
                      attempt=str(attempt.relative_to(f.ROOT)), warmup_seconds=warm,
                      accepted_runs=3 * len(selected(f)['blocks']))
                f.capture(attempt / name, block['workload'], mode, warm=warm,
                          duration=600, telemetry=True, target_qps=TARGETS[block['workload']],
                          require_arm_ready=(mode == 'arm'))
            metadata.update(status='captured', completed=f.now())
            f.save(attempt / 'attempt.json', metadata)
            promote(f, block, attempt, warm)
            return
        except f.WarmupNotReady as exc:
            check_attempt_cleanup(f, attempt)
            metadata.update(status='warmup_insufficient', completed=f.now(), readiness=exc.report)
            f.save(attempt / 'attempt.json', metadata)
            f.emit('matched_block_rejected', block=block['id'], warmup_seconds=warm,
                   attempt=str(attempt), reason='Initial ARM sweep not complete before measurement')
            index = WARMUPS.index(warm) + 1
            assert index < len(WARMUPS), 'ARM readiness failed at maximum common warmup'
            warm = WARMUPS[index]
            number += 1
        except BaseException as exc:
            # Promotion can be interrupted between symlinks and selection.json.
            # Once captures are complete, resume must finish that same promotion.
            if metadata['status'] != 'captured':
                metadata['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            metadata.update(completed=f.now(), error=str(exc))
            f.save(attempt / 'attempt.json', metadata)
            raise


def analyze(f):
    # Analyze only selected matched runs, after all live benchmarks have ended.
    require_idle_processes(f)
    os.sched_setaffinity(0, set(range(26, 34)))
    for block in schedule():
        entry = selected(f)['blocks'][block['id']]
        for mode in block['modes']:
            directory = f.ROOT / 'campaign-v1' / entry['runs'][mode]
            if (directory / 'receipt.json').exists():
                f.B.verify_receipt(directory)
            else:
                state(f, 'analyzing', run=directory.name, accepted_runs=27)
                f.analyze_run(directory)
    f.subprocess.check_call([sys.executable, str(f.SCRIPTS / 'report-redis-ycsb-fixedfreq.py'),
                            'campaign', '--directory', str(f.ROOT / 'campaign-v1'),
                            '--output-dir', str(f.ROOT / 'analysis')])


def status(root):
    path = P(root)
    value = {'root': str(path), 'expected_runs': 27,
             'state': read(path / 'tail-state.json') if (path / 'tail-state.json').exists() else None,
             'selection': read(path / 'selection.json') if (path / 'selection.json').exists() else None,
             'analyzed_runs': len(list((path / 'campaign-v1').glob('*/receipt.json')))}
    value['accepted_runs'] = 3 * len((value['selection'] or {}).get('blocks', {}))
    print(json.dumps(value, sort_keys=True, indent=2), flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt('signal %d; current attempt preserved' % signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'resume', 'status'])
    parser.add_argument('--output-root', type=P, default=DEFAULT_ROOT)
    parser.add_argument('--frequency-profile-root', type=P, default=DEFAULT_PROFILE)
    args = parser.parse_args()
    if args.action == 'status':
        status(args.output_root)
        return
    assert os.geteuid() == 0, 'Root supervision required'
    f = configure(args.output_root, args.frequency_profile_root)
    f.ROOT.mkdir(parents=True, exist_ok=True)
    with open(str(f.frequency_root() / 'experiment.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'prepare':
            require_idle_processes(f)
            f.bootstrap_tools()
            f.load_helper()
            f.prepare()
            identity = machine_identity(f)
            path = f.ROOT / 'machine.json'
            if path.exists():
                assert read(path) == identity, 'Machine identity changed'
            else:
                f.save(path, identity)
            # Replace generic wrapper with the dedicated matched campaign entry point.
            import shlex
            wrapper = f.ROOT / 'bin/redis-ycsb-tail-campaign'
            argv = ['python3', str(P(__file__).resolve()), '--output-root', str(f.ROOT),
                    '--frequency-profile-root', str(f.frequency_root())]
            wrapper.write_text('#!/bin/sh\nexec ' + ' '.join(shlex.quote(x) for x in argv) + ' "$@"\n')
            wrapper.chmod(0o755)
            if not (f.ROOT / 'tail-state.json').exists():
                state(f, 'prepared', accepted_runs=0)
            return
        try:
            f.HELPER_SHA = read(f.ROOT / 'source-manifest.json')['hashes'][str(f.HELPER)]
            f.load_helper()
            validate_frozen(f)
            prior = read(f.ROOT / 'tail-state.json') if (f.ROOT / 'tail-state.json').exists() else {}
            if prior.get('supervisor') and prior['supervisor'] != f.process_identity(os.getpid()):
                require_dead(f, [prior['supervisor']])
            require_idle_processes(f)
            os.sched_setaffinity(0, {24})
            for block in schedule():
                run_block(f, block)
            state(f, 'captured', accepted_runs=27)
            analyze(f)
            require_idle_processes(f)
            validate_frozen(f)
            state(f, 'complete', accepted_runs=27, analyzed_runs=27)
        except BaseException as exc:
            state(f, 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                  error=str(exc), traceback=traceback.format_exc(),
                  accepted_runs=3 * len(selected(f)['blocks']))
            raise


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    main()
