#!/usr/bin/env python3
"""Pure, fail-closed Redis/CMD-P inventory evidence validation (Python 3.6+).

The independent coverage denominator comes from the full target smaps, not the
monitor's resident_pages (which counts only inspected, supported VMAs). The
caller owns process identity checks, collection, global ownership checks and
cleanup; this module never opens a process, starts a monitor or changes policy.
"""
import json
import math
import re


HEADER = re.compile(r'^([0-9a-fA-F]+)-([0-9a-fA-F]+)\s+([r-][w-][x-][ps])\s+'
                    r'([0-9a-fA-F]+)\s+([0-9a-fA-F]+:[0-9a-fA-F]+)\s+(\d+)'
                    r'(?:\s+(.*))?$')
SMAPS_FIELDS = ('Rss', 'Anonymous', 'AnonHugePages', 'Swap')
STATUS_EVENTS = ('status', 'boundary_status', 'observation_end', 'stopping_status', 'final')
COHORT_EVENTS = ('sweep', 'cohort_snapshot', 'cohort_observation_end', 'cohort_final')


def parse_status(text):
    if not isinstance(text, str):
        raise ValueError('/proc/status evidence must be text')
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if ':' not in line:
            raise ValueError('Malformed /proc/status line')
        key, value = line.split(':', 1)
        if key in result:
            raise ValueError('Duplicate /proc/status field: ' + key)
        result[key] = value.strip()
    for key in ('THP_enabled', 'VmSwap', 'Pid'):
        if key not in result:
            raise ValueError('Missing /proc/status field: ' + key)
    for key in ('THP_enabled', 'Pid'):
        if not re.match(r'^\d+$', result[key]):
            raise ValueError('Malformed /proc/status field: ' + key)
        result[key] = int(result[key])
    if not re.match(r'^\d+\s+kB$', result['VmSwap']):
        raise ValueError('Malformed /proc/status field: VmSwap')
    result['VmSwap_bytes'] = int(result['VmSwap'].split()[0]) * 1024
    return result


def parse_smaps(text):
    """Return all mappings and independently summed private anonymous RSS.

    A writable private anonymous mapping has 'w' and 'p' permissions, inode 0,
    and either no name or a kernel bracket name such as [heap], [stack] or
    [anon:...]. Its Anonymous bytes count resident anonymous backing, excluding
    anonymous COW pages inside file-backed mappings. Rss is retained separately.
    """
    if not isinstance(text, str):
        raise ValueError('Full smaps evidence must be text')
    mappings, current = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        header = HEADER.match(line)
        if header:
            if current is not None:
                mappings.append(current)
            start, end, permissions, offset, device, inode, name = header.groups()
            current = {'start': int(start, 16), 'end': int(end, 16),
                       'permissions': permissions, 'offset': int(offset, 16),
                       'device': device, 'inode': int(inode), 'name': name or '',
                       'fields': {}}
            if current['end'] <= current['start']:
                raise ValueError('Invalid smaps address range at line %d' % number)
            if mappings and current['start'] < mappings[-1]['end']:
                raise ValueError('Overlapping/out-of-order smaps mappings')
        else:
            if current is None or ':' not in line:
                raise ValueError('Malformed smaps line %d' % number)
            key, value = line.split(':', 1)
            if key in current['fields']:
                raise ValueError('Duplicate smaps field: ' + key)
            current['fields'][key] = value.strip()
    if current is not None:
        mappings.append(current)
    if not mappings:
        raise ValueError('Full smaps contains no mappings')
    totals = {'mapping_count': len(mappings), 'rss_bytes': 0,
              'anon_huge_pages_bytes': 0, 'swap_bytes': 0,
              'writable_private_anonymous_resident_bytes': 0,
              'writable_private_anonymous_mapping_rss_bytes': 0,
              'writable_private_anonymous_mapping_count': 0,
              'exclusions': {}, 'mappings': []}
    for mapping in mappings:
        fields = mapping.pop('fields')
        for key in SMAPS_FIELDS:
            if key not in fields or not re.match(r'^\d+\s+kB$', fields[key]):
                raise ValueError('Missing/malformed smaps field %s at %x' %
                                 (key, mapping['start']))
            mapping[key + '_bytes'] = int(fields[key].split()[0]) * 1024
        if 'VmFlags' not in fields:
            raise ValueError('Missing smaps VmFlags at %x' % mapping['start'])
        mapping['vm_flags'] = fields['VmFlags'].split()
        if mapping['Anonymous_bytes'] > mapping['Rss_bytes']:
            raise ValueError('Anonymous exceeds Rss in smaps')
        permissions, name = mapping['permissions'], mapping['name']
        if permissions[1] != 'w':
            exclusion = 'not_writable'
        elif permissions[3] != 'p':
            exclusion = 'not_private'
        elif mapping['inode'] != 0 or (name and not name.startswith('[')):
            exclusion = 'file_or_named_backing'
        else:
            exclusion = None
        mapping['included_in_denominator'] = exclusion is None
        totals['rss_bytes'] += mapping['Rss_bytes']
        totals['anon_huge_pages_bytes'] += mapping['AnonHugePages_bytes']
        totals['swap_bytes'] += mapping['Swap_bytes']
        if exclusion is None:
            totals['writable_private_anonymous_resident_bytes'] += mapping['Anonymous_bytes']
            totals['writable_private_anonymous_mapping_rss_bytes'] += mapping['Rss_bytes']
            totals['writable_private_anonymous_mapping_count'] += 1
        else:
            group = totals['exclusions'].setdefault(exclusion, {'mappings': 0, 'rss_bytes': 0})
            group['mappings'] += 1
            group['rss_bytes'] += mapping['Rss_bytes']
        totals['mappings'].append(mapping)
    totals['denominator_definition'] = ('Sum Anonymous kB from writable private inode-0 '
        'anonymous mappings in full smaps; not CMD-P supported-VMA resident_pages')
    return totals


def parse_inventory(text):
    if not isinstance(text, str):
        raise ValueError('Inventory evidence must be text')
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise ValueError('Invalid inventory JSON at line %d' % number)
        if not isinstance(row, dict) or not isinstance(row.get('event'), str):
            raise ValueError('Missing inventory event at line %d' % number)
        rows.append(row)
    if not rows:
        raise ValueError('Inventory JSONL is empty')
    return rows


def validate_eligibility(status_text, smaps_text, inventory_text, redis_used_memory,
                         page_size=4096, expected_pid=None, controller_exit_code=0,
                         minimum_fraction=0.90):
    """Return a serializable report; missing or failed evidence sets passed=False."""
    report = {'version': 1, 'passed': False, 'reasons': [], 'memory': {},
              'inventory': {}, 'thresholds': {'minimum_fraction': minimum_fraction,
              'page_size': page_size}, 'controller_exit_code': controller_exit_code}
    reasons = report['reasons']

    def fail(message):
        if message not in reasons:
            reasons.append(message)

    def integer(row, key, context):
        value = row.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail('%s missing/invalid %s' % (context, key))
            return None
        return value

    def zero(row, keys, context):
        for key in keys:
            value = integer(row, key, context)
            if value is not None and value != 0:
                fail('%s %s is nonzero (%d)' % (context, key, value))

    if not isinstance(redis_used_memory, int) or isinstance(redis_used_memory, bool) or redis_used_memory <= 0:
        fail('Redis used_memory must be a positive byte count')
    else:
        report['memory']['redis_used_memory_bytes'] = redis_used_memory
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0 or page_size & (page_size - 1):
        fail('Page size must be a positive power of two')
    if not isinstance(minimum_fraction, (int, float)) or not math.isfinite(minimum_fraction) or not 0 < minimum_fraction <= 1:
        fail('Minimum fraction must be in (0, 1]')
    if controller_exit_code != 0:
        fail('Inventory subprocess exit is not zero')
    try:
        status = parse_status(status_text)
        report['process'] = {'pid': status['Pid'], 'thp_enabled': status['THP_enabled'],
                             'swap_bytes': status['VmSwap_bytes']}
        if expected_pid is not None and status['Pid'] != expected_pid:
            fail('Status PID differs from expected target')
        if status['THP_enabled'] != 0:
            fail('Process THP_enabled is not zero')
        if status['VmSwap_bytes']:
            fail('Process VmSwap is not zero')
    except (ValueError, TypeError) as error:
        fail(str(error))
        status = None
    try:
        memory = parse_smaps(smaps_text)
        report['memory'].update(memory)
        if memory['anon_huge_pages_bytes']:
            fail('Full smaps AnonHugePages is not zero')
        if memory['swap_bytes']:
            fail('Full smaps Swap is not zero')
        if not memory['writable_private_anonymous_resident_bytes']:
            fail('Independent private-anonymous writable resident denominator is zero')
    except (ValueError, TypeError) as error:
        fail(str(error))
        memory = None
    try:
        rows = parse_inventory(inventory_text)
    except (ValueError, TypeError) as error:
        fail(str(error))
        return report
    grouped = {}
    for row in rows:
        grouped.setdefault(row['event'], []).append(row)
    report['inventory']['events'] = {key: len(values) for key, values in grouped.items()}
    for event in ('controller', 'final', 'controller_exit', 'stop_return'):
        if len(grouped.get(event, [])) != 1:
            fail('Inventory requires exactly one ' + event + ' record')
    for event in ('dirty_status', 'batch_status', 'diagnostics', 'progress', 'cpu_status'):
        if not grouped.get(event):
            fail('Inventory missing ' + event + ' evidence')
    if grouped.get('cohort_missing'):
        fail('Inventory has a missing cohort')
    for row in grouped.get('controller', []):
        if row.get('monitor_enabled') is not True:
            fail('Inventory monitor was not enabled')
        target_pid = expected_pid if expected_pid is not None else (status or {}).get('Pid')
        if target_pid is None or row.get('pid') != target_pid:
            fail('Inventory controller target PID mismatch')
    for row in grouped.get('controller_exit', []):
        zero(row, ('exit_code',), 'controller_exit')
    for row in grouped.get('stop_return', []):
        zero(row, ('errno',), 'stop_return')
    session_ids = set()
    for row in rows:
        event = row['event']
        if event in STATUS_EVENTS:
            session_id = integer(row, 'session_id', event)
            if session_id is not None:
                session_ids.add(session_id)
            if row.get('flags') != 1:
                fail('Inventory status lacks exclusive SCAN_ONLY flag')
            if status is None or row.get('target_pid') != status['Pid']:
                fail('Inventory status target PID mismatch')
            zero(row, ('first_arms', 'rearms', 'active', 'pre_stop_active',
                       'history_entries', 'history_resets', 'resource_failures',
                       'reports_dropped'), event)
            if not isinstance(row.get('terminals'), dict):
                fail(event + ' missing terminals')
            else:
                zero(row['terminals'], ('write', 'error', 'fork', 'exec', 'thp_change', 'scope_change'), event + '.terminals')
        elif event in COHORT_EVENTS:
            zero(row, ('first_arms', 'rearms', 'already_armed', 'active',
                       'write_revoked', 'active_first', 'active_rearm', 'error'), event)
        elif event == 'dirty_status':
            zero(row, ('mode', 'samples', 'resets', 'flushes', 'wp_resets',
                       'resource_skips', 'metadata_bytes'), event)
        elif event == 'batch_status':
            zero(row, ('flush_calls', 'shared_flush_calls', 'dirty_only_flush_calls',
                       'wp_only_flush_calls', 'dirty_pages', 'wp_pages',
                       'history_regions', 'history_allocated_bytes', 'history_payload_bytes'), event)
        elif event == 'progress':
            zero(row, ('write_calls', 'write_ns'), event)
        elif event == 'diagnostics':
            zero(row, ('trace_records_dropped',), event)
            if not isinstance(row.get('rejected'), dict):
                fail('Inventory diagnostics missing rejected counters')
            else:
                zero(row['rejected'], ('resource',), 'diagnostics.rejected')
        elif event == 'cpu_status':
            zero(row, ('error', 'wrong_cpu_work_items', 'offline_events'), event)
            if row.get('cpu') != 0:
                fail('Inventory scanner is not bound to CPU0')
    if len(session_ids) != 1:
        fail('Inventory does not identify exactly one session')
    finals = grouped.get('final', [])
    cohorts = grouped.get('cohort_final', [])
    if len(finals) == 1:
        final_index = rows.index(finals[0])
        if [row['event'] for row in rows[final_index + 1:final_index + 5]] != [
                'cpu_status', 'batch_status', 'dirty_status', 'diagnostics']:
            fail('Inventory missing complete final counter block')
        if final_index == 0 or rows[final_index - 1]['event'] != 'progress':
            fail('Inventory missing final progress evidence')
    report['inventory']['completed_cohorts'] = cohorts
    report['inventory']['pass_ids'] = [row.get('pass_id') for row in cohorts]
    if not cohorts:
        fail('Inventory has no final completed cohort')
    ids = [integer(row, 'pass_id', 'cohort_final') for row in cohorts]
    if any(value is None or value <= 0 for value in ids) or len(ids) != len(set(ids)):
        fail('Inventory final pass IDs invalid or duplicated')
    eligible = []
    for row in cohorts:
        if row.get('complete') != 1:
            fail('Inventory final cohort is incomplete')
        zero(row, ('error',), 'cohort_final')
        for key in ('writable_pages', 'examined_pages', 'resident_pages', 'eligible_pages', 'start_ns', 'end_ns'):
            integer(row, key, 'cohort_final')
        if (isinstance(row.get('start_ns'), int) and isinstance(row.get('end_ns'), int)
                and not 0 < row['start_ns'] <= row['end_ns']):
            fail('Inventory cohort timing is invalid')
        if not isinstance(row.get('skips'), dict):
            fail('Inventory cohort missing skips')
        else:
            zero(row['skips'], ('thp_policy', 'resource', 'swapped'), 'cohort_final.skips')
        if isinstance(row.get('eligible_pages'), int) and row['eligible_pages'] >= 0:
            eligible.append(row['eligible_pages'])
    for final in finals:
        if final.get('state') != 4 or final.get('stop_reason') != 'control_stop':
            fail('Inventory did not stop cleanly with control_stop')
        started = integer(final, 'passes_started', 'final')
        completed = integer(final, 'passes_completed', 'final')
        if started != completed or completed != len(cohorts) or not completed:
            fail('Inventory final pass counts do not match complete cohorts')
    if eligible and isinstance(page_size, int) and page_size > 0:
        eligible_bytes = min(eligible) * page_size
        report['inventory']['eligible_pages_min'] = min(eligible)
        report['inventory']['eligible_bytes_min'] = eligible_bytes
        denominators = {'redis_used_memory': redis_used_memory,
                        'independent_anonymous_resident': (memory or {}).get('writable_private_anonymous_resident_bytes')}
        fractions = {}
        for name, value in denominators.items():
            if isinstance(value, int) and value > 0:
                fractions[name] = eligible_bytes / value
                if isinstance(minimum_fraction, (int, float)) and fractions[name] < minimum_fraction:
                    fail('Eligible bytes below %.0f%% of %s' % (minimum_fraction * 100, name))
        report['inventory']['coverage_fractions'] = fractions
    report['passed'] = not reasons
    return report
