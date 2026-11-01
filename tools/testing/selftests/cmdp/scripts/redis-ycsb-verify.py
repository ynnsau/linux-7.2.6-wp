#!/usr/bin/env python3
"""Bounded, full-dataset Redis eligibility verification (Python 3.6+).

The fixed-frequency runner owns bootstrap, locking, source freezing and the
eligibility gate. This module performs one idle functional check and three
short B checks; it deliberately makes no performance-comparison claim.
"""
import hashlib
import json
import os
import pathlib
import time
import traceback

P = pathlib.Path
GIB = 1024 ** 3
PAGE = os.sysconf('SC_PAGE_SIZE')
MODES = ('off', 'sampling', 'arm')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_rows(path):
    return [json.loads(line) for line in P(path).read_text().splitlines() if line]


def checked_snapshot(rows):
    statuses = [row for row in rows if 'terminals' in row]
    require(len(statuses) == 1, 'Missing or ambiguous CMD-P status snapshot')
    status = statuses[0]
    require(status['state'] == 2, 'CMD-P is not running: %s' % status)
    require(status['resource_failures'] == status['reports_dropped'] == 0,
            'CMD-P resource failure or dropped report')
    cpus = [row for row in rows if row.get('event') == 'cpu_status']
    require(len(cpus) == 1, 'Missing scanner CPU status')
    cpu = cpus[0]
    require(cpu['cpu'] == 0 and cpu['error'] == cpu['wrong_cpu_work_items'] ==
            cpu['offline_events'] == 0, 'Scanner CPU check failed: %s' % cpu)
    if cpu['work_items']:
        require(cpu['last_cpu'] == 0, 'Scanner ran away from CPU0')
    return status


def monitoring_summary(rows, mode, used_memory):
    """Validate actual scan scope, independently of a successful gate."""
    statuses = [row for row in rows if 'terminals' in row]
    require(statuses, 'Missing monitor status evidence')
    require(all(row['resource_failures'] == row['reports_dropped'] == 0
                for row in statuses), 'Monitor lost resources or reports')
    cohorts = [row for row in rows if row.get('event') in ('sweep', 'cohort_snapshot')]
    require(cohorts, 'Missing monitor cohort evidence')
    require(all(row['error'] == 0 for row in cohorts), 'Monitor reported a scan error')
    require(all(row['skips']['thp_policy'] == 0 for row in cohorts),
            'THP policy exclusions returned during workload')
    require(all(row['skips'].get('resource', 0) == 0 for row in cohorts),
            'Monitor cohort reported resource exclusions')
    completed = [row for row in cohorts if row['complete']]
    require(completed, 'No complete workload scan')
    eligible_bytes = max(row['eligible_pages'] for row in completed) * PAGE
    require(eligible_bytes >= .9 * used_memory,
            'Workload scan did not cover substantial dataset memory')
    dirty = [row for row in rows if row.get('event') == 'dirty_status']
    require(dirty, 'Missing dirty-sampling evidence')
    require(all(row['resource_skips'] == 0 for row in dirty),
            'Dirty sampling encountered resource skips')
    sampled_pages = max(row['samples'] for row in dirty)
    require(sampled_pages * PAGE >= .9 * used_memory,
            'Dirty sampling did not traverse substantial dataset memory')
    first_arms = max(row['first_arms'] for row in statuses)
    rearms = max(row['rearms'] for row in statuses)
    wp_events = max(row['terminals']['write'] for row in statuses)
    live_peak_pages = max(row['active'] for row in statuses)
    final = [row for row in statuses if row.get('event') == 'final']
    require(len(final) == 1 and final[0]['active'] == 0,
            'Missing clean final status')
    if mode == 'sampling':
        require(first_arms == rearms == wp_events == live_peak_pages == 0,
                'Sampling-only check performed ARM or WP')
        require(all(row['wp_pages'] == 0 for row in rows
                    if row.get('event') == 'batch_status'),
                'Sampling-only check issued WP protection')
    batches = [row for row in rows if row.get('event') == 'batch_status']
    require(batches, 'Missing batch/flush evidence')
    return {'scope': 'whole monitor session, including warmup and STOP',
            'eligible_bytes_max_complete_scan': eligible_bytes,
            'sampled_pages_total': sampled_pages,
            'first_arms_total': first_arms, 'rearms_total': rearms,
            'wp_events_total': wp_events,
            'live_arm_peak_bytes_observed': live_peak_pages * PAGE,
            'pre_stop_live_arm_bytes': final[0]['pre_stop_active'] * PAGE,
            'flush_calls_total': max(row['flush_calls'] for row in batches),
            'dirty_pages_reset_total': max(row['dirty_pages'] for row in batches),
            'wp_pages_protected_total': max(row['wp_pages'] for row in batches),
            'complete_pass_ids': sorted(set(row['pass_id'] for row in completed)),
            'arm_coverage_requirement': 'reported; no workload minimum'}


def idle_check(runner, directory):
    require(not directory.exists(), 'Preserve previous idle attempt: ' + str(directory))
    require(os.uname().release == runner.CONFIG['kernel'],
            'Idle verification requires the configured production kernel')
    runner.B.mkdir(directory)
    policies = runner.B.policies()
    khz = runner.frequency_policy()
    before = runner.idle_cmdp()
    manifest = json.loads((runner.STUDY / 'artifacts/golden/manifest.json').read_text())
    p = redis = control = None
    identity = None
    failure = None
    cleanup_errors = []
    report = {'at': runner.now(), 'passed': False, 'kind': 'idle-arm-functional',
              'minimum_live_arm_bytes': GIB, 'qualification_timeout_seconds': 120,
              'policy_before': policies, 'selected_khz': khz, 'cmdp_before': before,
              'source_hashes': runner.selected_sources()}
    runner.save(directory / 'run.json', report)
    try:
        runner.emit('eligibility_idle_restore_start', directory=str(directory))
        p, redis, restore_s = runner.B.start_redis(
            directory, runner.STUDY / 'artifacts/golden',
            exec_prefix=[runner.ROOT / 'bin/cmdp_exec', '--'])
        identity = runner.process_identity(p.pid)
        runner.save(directory / 'processes.json', {'redis': identity})
        validation = runner.B.validate_db(redis, manifest['recordcount'], manifest['samples'])
        require(abs(validation['info']['used_memory'] / (16 * GIB) - 1) <= .05,
                'Restored Redis allocation differs from the 16 GiB dataset')
        runner.save(directory / 'restore.json',
                    {'seconds': restore_s, 'validation': validation, 'redis': identity,
                     'rdb_sha256': manifest['rdb_sha256']})
        report['eligibility'] = runner.eligibility_gate(directory, p, validation)
        report['restore_seconds'] = restore_s
        argv = [runner.ROOT / 'bin/cmdp_monitor', '--pid', p.pid,
                '--scanner-cpu', 0, '--period-ms', runner.CONFIG['period_ms'],
                '--quiet-ms', runner.CONFIG['quiet_ms'],
                '--dirty-batch-pages', runner.CONFIG['dirty_batch_pages'],
                '--wp-batch-pages', runner.CONFIG['wp_batch_pages'], '--dirty-qualify']
        control = runner.Control('cmdp', argv, directory)
        runner.save(directory / 'processes.json',
                    {'redis': identity, 'controls': {'cmdp': runner.process_identity(control.p.pid)}})
        deadline = time.monotonic() + 120
        observations = []
        while True:
            require(runner.process_identity(p.pid) == identity, 'Redis identity changed')
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Idle ARM did not reach 1 GiB within 120 seconds')
            label = 'idle_%d' % len(observations)
            control.request(label)
            rows = control.wait_snapshot(label, timeout=min(30, remaining))
            status = checked_snapshot(rows)
            observations.append({'at': runner.now(), 'label': label, 'rows': rows})
            runner.save(directory / 'idle-observations.json', observations)
            runner.emit('eligibility_idle_arm', active_bytes=status['active'] * PAGE,
                        completed_passes=status['passes_completed'])
            if status['active'] * PAGE >= GIB:
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        report['qualified_live_arm_bytes'] = status['active'] * PAGE
        report['qualified_status'] = status
        wp_before = status['terminals']['write']
        # Existing fields and fixed-size values preserve the dataset schema.
        # Spread updates across the full key range; this is a disposable restore.
        updates = []
        readback = []
        for i in range(1024):
            index = (i * manifest['recordcount']) // 1024
            key = runner.B.key(index)
            value = ('cmdp-eligibility-%08d:' % i).encode().ljust(100, b'x')
            reply = redis.call('HSET', key, 'field0', value)
            require(reply == 0, 'Controlled HSET inserted an unexpected field: ' + key)
            readback.append((key, value))
            updates.append({'key': key, 'field': 'field0', 'bytes': len(value),
                            'hset_response': reply, 'sha256': hashlib.sha256(value).hexdigest()})
        control.request('after_controlled_writes')
        after_write_rows = control.wait_snapshot('after_controlled_writes')
        after_write = checked_snapshot(after_write_rows)
        wp_delta = after_write['terminals']['write'] - wp_before
        runner.save(directory / 'controlled-writes.json',
                    {'updates': updates, 'before_status': status, 'after_rows': after_write_rows,
                     'wp_delta': wp_delta, 'readback_verified': False,
                     'scope': '1024 HSET operations on existing fields; readback after counter snapshot'})
        require(wp_delta > 0, 'Controlled writes did not trigger any WP event')
        for key, value in readback:
            require(redis.call('HGET', key, 'field0') == value,
                    'Controlled HSET readback mismatch: ' + key)
        runner.save(directory / 'controlled-writes.json',
                    {'updates': updates, 'before_status': status, 'after_rows': after_write_rows,
                     'wp_delta': wp_delta, 'readback_verified': True,
                     'scope': '1024 HSET operations on existing fields; readback after counter snapshot'})
        report['controlled_updates'] = len(updates)
        report['controlled_write_wp_delta'] = wp_delta
        require(redis.call('DBSIZE') == manifest['recordcount'] + 1, 'DBSIZE changed')
        require(redis.call('ZCARD', '_indices') == manifest['recordcount'], 'Index size changed')
        require(runner.B.proc(p.pid)['vm_swap_kib'] == 0, 'Redis swapped during idle check')
        runner.B.affinity(p.pid, [runner.CONFIG['redis_cpu']])
        require(runner.B.policies() == policies, 'System policy changed during idle check')
        require(runner.frequency_policy() == khz, 'Frequency policy changed during idle check')
    except BaseException as exc:
        failure = exc
        report['failure'] = {'error': str(exc), 'traceback': traceback.format_exc()}
        runner.save(directory / 'failure.json', report['failure'])
    finally:
        # Always attempt each cleanup action, even if an earlier one fails.
        if control is not None:
            try:
                control.stop()
                runner.validate_control(directory)
                if failure is None:
                    report['monitoring'] = monitoring_summary(
                        read_rows(directory / 'cmdp.jsonl'), 'arm',
                        validation['info']['used_memory'])
            except BaseException as exc:
                cleanup_errors.append('CMD-P stop/validation: ' + str(exc))
        if p is not None and redis is not None:
            try:
                if p.poll() is None:
                    require(runner.process_identity(p.pid) == identity,
                            'Refusing cleanup of a changed Redis identity')
                runner.B.stop_redis(p, redis)
            except BaseException as exc:
                cleanup_errors.append('Redis stop: ' + str(exc))
        try:
            report['cmdp_after'] = runner.idle_cmdp()
        except BaseException as exc:
            cleanup_errors.append('Global ownership cleanup: ' + str(exc))
        report['cleanup_errors'] = cleanup_errors
        report['passed'] = failure is None and not cleanup_errors
        report['finished_at'] = runner.now()
        runner.save(directory / 'idle-verification.json', report)
    if failure is not None:
        raise failure
    require(not cleanup_errors, 'Idle verification cleanup failed: ' + '; '.join(cleanup_errors))
    return report


def run(runner):
    """Called once under the runner's experiment lock and frozen configuration."""
    require(os.geteuid() == 0, 'Root supervision required')
    require(runner.CONFIG['workers'] == 8, 'Verification requires eight workers')
    require(runner.CONFIG['target_qps'] == 50000, 'Verification target must be 50k QPS')
    require(runner.CONFIG['period_ms'] == runner.CONFIG['quiet_ms'] == 10000,
            'Verification requires 10-second period and quiet interval')
    require(runner.CONFIG['dirty_batch_pages'] == runner.CONFIG['wp_batch_pages'] == 512,
            'Verification requires the existing 512/512 batch limits')
    os.sched_setaffinity(0, {runner.CONFIG['observer_cpu']})
    target = runner.ROOT / 'verify-summary.json'
    require(not target.exists(), 'Preserve previous verification: ' + str(target))
    stage_paths = [runner.ROOT / 'idle-arm-v1'] + [runner.ROOT / 'smoke-v1' / ('B-' + mode + '-v1')
                                                for mode in MODES]
    require(not any(p.exists() for p in stage_paths),
            'Preserve previous verification stages; choose a fresh output root')
    result = {'at': runner.now(), 'passed': False, 'status': 'running',
              'purpose': 'eligibility and functional verification; not a latency campaign',
              'workload': 'B', 'workers': 8, 'target_qps': 50000,
              'warmup_seconds': 20, 'measure_seconds': 120, 'repetitions_per_mode': 1,
              'smoke_checks': [], 'source_hashes': runner.selected_sources()}
    runner.save(target, result)
    try:
        runner.save(runner.ROOT / 'verify-state.json',
                    {'at': runner.now(), 'stage': 'idle-arm', 'status': 'running',
                     'supervisor': runner.process_identity(os.getpid())})
        result['idle_arm'] = idle_check(runner, stage_paths[0])
        runner.save(target, result)
        for mode, directory in zip(MODES, stage_paths[1:]):
            runner.save(runner.ROOT / 'verify-state.json',
                        {'at': runner.now(), 'stage': 'B-' + mode, 'status': 'running',
                         'supervisor': runner.process_identity(os.getpid())})
            capture = runner.capture(directory, 'B', mode, warm=20, duration=120)
            runner.analyze_run(directory)
            check = {'mode': mode, 'directory': str(directory), 'passed': True,
                     'eligibility': json.loads((directory / 'eligibility.json').read_text()),
                     'post_warmup_counters': capture['counters'],
                     'raw_analysis': str(directory / 'latency.json'),
                     'cleanup': runner.idle_cmdp()}
            if mode != 'off':
                check['monitoring'] = monitoring_summary(read_rows(directory / 'cmdp.jsonl'),
                                                         mode, capture['redis_used_memory_mean'])
            else:
                require(not (directory / 'cmdp.jsonl').exists(),
                        'OFF unexpectedly launched a monitor after its inventory gate')
            result['smoke_checks'].append(check)
            runner.save(target, result)
        result.update({'passed': True, 'status': 'complete', 'finished_at': runner.now(),
                       'conclusion': 'Full-dataset eligibility and bounded functional checks passed; '
                                     'tail-latency comparison still requires a matched campaign.'})
    except BaseException as exc:
        result.update({'status': 'failed', 'finished_at': runner.now(),
                       'error': str(exc), 'traceback': traceback.format_exc()})
        runner.save(target, result)
        runner.save(runner.ROOT / 'verify-state.json',
                    {'at': runner.now(), 'status': 'failed', 'error': str(exc)})
        raise
    runner.save(target, result)
    runner.save(runner.ROOT / 'verify-state.json', {'at': runner.now(), 'status': 'complete'})
    runner.emit('eligibility_verification_complete', summary=str(target))
    return result
