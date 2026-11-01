#!/usr/bin/env python3
"""Pure eligibility gate regressions; optional replay of an old real CMD-P trace."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path


def load():
    spec = importlib.util.spec_from_file_location('eligibility',
        str(Path(__file__).with_name('redis-ycsb-eligibility.py')))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def smaps_mapping(start, end, permissions, inode=0, name='', rss=0,
                  anonymous=0, huge=0, swap=0):
    return ('%x-%x %s 00000000 00:00 %d %s\n'
            'Rss: %d kB\nAnonymous: %d kB\nAnonHugePages: %d kB\n'
            'Swap: %d kB\nVmFlags: rd wr mr mw me ac sd\n' %
            (start, end, permissions, inode, name, rss, anonymous, huge, swap))


def good_evidence():
    size = 16 * 1024 ** 3
    pages = size // 4096
    status = 'Name:\tredis-server\nPid:\t42\nTHP_enabled:\t0\nVmSwap:\t0 kB\n'
    smaps = smaps_mapping(0x100000000, 0x500000000, 'rw-p', rss=size // 1024,
                          anonymous=size // 1024)
    smaps += smaps_mapping(0x600000000, 0x600001000, 'rw-p', name='[stack]', rss=4, anonymous=4)
    # These are real RSS but outside private-anonymous writable coverage.
    smaps += smaps_mapping(0x700000000, 0x700001000, 'r--p', rss=4, anonymous=4)
    smaps += smaps_mapping(0x800000000, 0x800001000, 'rw-s', name='/dev/zero', rss=4, anonymous=4)
    smaps += smaps_mapping(0x900000000, 0x900001000, 'rw-p', inode=1, name='/file', rss=4, anonymous=4)
    final = {'event': 'final', 'session_id': 1, 'target_pid': 42, 'state': 4,
             'stop_reason': 'control_stop', 'flags': 1, 'passes_started': 1,
             'passes_completed': 1, 'first_arms': 0, 'rearms': 0, 'active': 0,
             'pre_stop_active': 0, 'history_entries': 0, 'history_resets': 0,
             'resource_failures': 0, 'reports_dropped': 0,
             'terminals': {key: 0 for key in ('write', 'error', 'fork', 'exec',
                                            'thp_change', 'scope_change')}}
    cohort = {'event': 'cohort_final', 'pass_id': 1, 'start_ns': 10, 'end_ns': 20,
              'writable_pages': pages + 100, 'examined_pages': pages + 100,
              'resident_pages': pages + 1, 'eligible_pages': pages + 1,
              'complete': 1, 'error': 0,
              'skips': {'thp_policy': 0, 'resource': 0, 'swapped': 0}}
    cohort.update({key: 0 for key in ('first_arms', 'rearms', 'already_armed',
                                     'active', 'write_revoked', 'active_first', 'active_rearm')})
    cpu = {'event': 'cpu_status', 'cpu': 0, 'error': 0, 'wrong_cpu_work_items': 0, 'offline_events': 0}
    batch = {'event': 'batch_status'}
    batch.update({key: 0 for key in ('flush_calls', 'shared_flush_calls',
        'dirty_only_flush_calls', 'wp_only_flush_calls', 'dirty_pages', 'wp_pages',
        'history_regions', 'history_allocated_bytes', 'history_payload_bytes')})
    dirty = {'event': 'dirty_status'}
    dirty.update({key: 0 for key in ('mode', 'samples', 'resets', 'flushes',
                                   'wp_resets', 'resource_skips', 'metadata_bytes')})
    diagnostics = {'event': 'diagnostics', 'trace_records_dropped': 0, 'rejected': {'resource': 0}}
    rows = [{'event': 'controller', 'pid': 42, 'monitor_enabled': True},
            {'event': 'stop_return', 'errno': 0},
            {'event': 'progress', 'write_calls': 0, 'write_ns': 0},
            final, cpu, batch, dirty, diagnostics, cohort,
            {'event': 'controller_exit', 'exit_code': 0}]
    return status, smaps, rows, size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-trace', type=Path)
    args = parser.parse_args()
    module = load()
    status, smaps, rows, size = good_evidence()
    checks = []

    def validate(status_text=status, smaps_text=smaps, data=None, **kwargs):
        return module.validate_eligibility(status_text, smaps_text,
            '\n'.join(json.dumps(row) for row in (rows if data is None else data)),
            size, expected_pid=42, **kwargs)

    result = validate()
    assert result['passed'], result
    assert result['memory']['writable_private_anonymous_resident_bytes'] == size + 4096
    assert result['inventory']['coverage_fractions']['independent_anonymous_resident'] == 1
    assert set(result['memory']['exclusions']) == {'not_writable', 'not_private', 'file_or_named_backing'}
    checks.append('full smaps independent denominator excludes read-only/shared/file-backed COW mappings')
    encoded = '\n'.join(json.dumps(row) for row in rows)
    larger_redis = module.validate_eligibility(status, smaps, encoded, size * 2)
    assert not larger_redis['passed']
    assert any('of redis_used_memory' in reason for reason in larger_redis['reasons'])
    copied = copy.deepcopy(rows)
    next(row for row in copied if row['event'] == 'cohort_final')['eligible_pages'] //= 2
    smaller_redis = module.validate_eligibility(status, smaps,
        '\n'.join(json.dumps(row) for row in copied), size // 2)
    assert not smaller_redis['passed']
    assert any('of independent_anonymous_resident' in reason for reason in smaller_redis['reasons'])
    checks.append('Redis allocation and independent smaps coverage thresholds are each enforced separately')

    for bad_status in (status.replace('THP_enabled:\t0', 'THP_enabled:\t1'),
                       status.replace('THP_enabled:\t0\n', ''),
                       status.replace('VmSwap:\t0 kB', 'VmSwap:\t1 kB'),
                       status.replace('Pid:\t42', 'Pid:\t43'), None):
        assert not validate(status_text=bad_status)['passed']
    for bad_smaps in (smaps.replace('AnonHugePages: 0 kB', 'AnonHugePages: 4 kB', 1),
                      smaps.replace('Swap: 0 kB', 'Swap: 4 kB', 1),
                      smaps.replace('Anonymous: %d kB\n' % (size // 1024), '', 1),
                      smaps.replace('VmFlags:', 'MissingFlags:', 1), '', None):
        assert not validate(smaps_text=bad_smaps)['passed']
    checks.append('missing process flags/smaps fields, THP enablement, huge backing, swap and PID mismatch rejected')

    def changed(event, field, value, nested=None):
        copied = copy.deepcopy(rows)
        row = next(row for row in copied if row['event'] == event)
        target = row if nested is None else row[nested]
        if value is None:
            del target[field]
        else:
            target[field] = value
        result = validate(data=copied)
        assert not result['passed'], (event, field, value, result)

    failures = [
        ('cohort_final', 'eligible_pages', 7, None),
        ('cohort_final', 'thp_policy', 4978058, 'skips'),
        ('cohort_final', 'complete', 0, None),
        ('cohort_final', 'error', -12, None),
        ('cohort_final', 'resource', 1, 'skips'),
        ('cohort_final', 'swapped', 1, 'skips'),
        ('cohort_final', 'thp_policy', None, 'skips'),
        ('final', 'flags', 0, None),
        ('final', 'passes_started', 2, None),
        ('final', 'passes_completed', None, None),
        ('final', 'resource_failures', 1, None),
        ('final', 'reports_dropped', 1, None),
        ('final', 'active', 1, None),
        ('final', 'first_arms', 1, None),
        ('final', 'state', 3, None),
        ('final', 'stop_reason', 'scope_change', None),
        ('final', 'write', 1, 'terminals'),
        ('final', 'write', None, 'terminals'),
        ('controller_exit', 'exit_code', 143, None),
        ('stop_return', 'errno', 5, None),
        ('dirty_status', 'resets', 1, None),
        ('dirty_status', 'mode', 1, None),
        ('batch_status', 'flush_calls', 1, None),
        ('batch_status', 'wp_pages', 1, None),
        ('batch_status', 'flush_calls', None, None),
        ('progress', 'write_calls', 1, None),
        ('diagnostics', 'trace_records_dropped', 1, None),
        ('cpu_status', 'wrong_cpu_work_items', 1, None),
        ('cpu_status', 'cpu', 1, None)]
    for event, field, value, nested in failures:
        changed(event, field, value, nested)
    checks.append('%d negative field/counter regressions rejected, including seven-page inventory' % len(failures))

    for event in ('controller', 'final', 'controller_exit', 'stop_return', 'dirty_status',
                  'batch_status', 'diagnostics', 'progress', 'cpu_status', 'cohort_final'):
        assert not validate(data=[row for row in rows if row['event'] != event])['passed'], event
    copied = copy.deepcopy(rows)
    copied.insert(1, copy.deepcopy(next(row for row in copied if row['event'] == 'batch_status')))
    copied = [row for index, row in enumerate(copied) if not (index > 1 and row['event'] == 'batch_status')]
    assert not validate(data=copied)['passed'], 'initial batch data cannot substitute for final batch data'
    assert not validate(data=rows + [{'event': 'cohort_missing', 'pass_id': 1}])['passed']
    assert not validate(controller_exit_code=143)['passed']
    assert not validate(minimum_fraction=0)['passed']
    assert not validate(page_size=0)['passed']
    malformed = module.validate_eligibility(status, smaps, '{broken', size)
    assert not malformed['passed']
    checks.append('missing/truncated inventory blocks and subprocess failure rejected')

    if args.legacy_trace:
        legacy = module.validate_eligibility(status, smaps, args.legacy_trace.read_text(), size)
        assert not legacy['passed']
        assert any('thp_policy is nonzero' in reason for reason in legacy['reasons']), legacy
        assert any('Eligible bytes below' in reason for reason in legacy['reasons']), legacy
        checks.append('actual historical seven-page/THP-policy-skipped trace rejected against independent 16GiB denominator')
    print(json.dumps({'passed': True, 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
