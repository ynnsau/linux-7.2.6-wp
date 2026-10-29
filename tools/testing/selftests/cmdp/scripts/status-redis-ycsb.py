#!/usr/bin/env python3
"""Read durable Redis/YCSB campaign progress without contacting Redis."""
import argparse
import datetime
import json
import pathlib
import re


def tail(path, size=65536):
    if not path.exists():
        return []
    with path.open('rb') as stream:
        start = max(0, path.stat().st_size - size)
        stream.seek(start)
        lines = stream.read().splitlines()
    return [s.decode('utf-8', errors='replace') for s in (lines[1:] if start else lines)]


def status(study):
    root = study / 'artifacts/baseline-v1'
    state = root / 'campaign-state.json'
    out = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if not state.exists():
        return dict(out, status='not-started')
    saved = json.loads(state.read_text())
    out.update({k: saved[k] for k in ('status', 'completed', 'active') if k in saved})
    exit_file = study / 'build-logs/campaign.exit'
    if exit_file.exists():
        out['exit'] = int(exit_file.read_text().strip())
        if out['exit']:
            out['status'] = 'failed'
            out['error_tail'] = tail(study / 'build-logs/campaign.log')[-8:]
    completed = []
    for line in tail(study / 'build-logs/campaign.log'):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if 'run' in item:
            completed.append(item)
    if completed:
        out['last_completed'] = completed[-1]
    if saved.get('active') and 'exit' not in out:
        run = root / 'runs' / saved['active']
        statuses = []
        for line in tail(run / 'ycsb.log'):
            match = re.search(r' (\d+) sec: (\d+) operations; ([0-9.]+) current ops/sec;', line)
            if match:
                statuses.append(match)
        out['phase'] = 'restore-or-startup'
        if statuses:
            match = statuses[-1]
            elapsed = int(match.group(1))
            out.update(elapsed_s=elapsed, current_qps=float(match.group(3)),
                       phase='warm-up' if elapsed < 30 else 'measurement' if elapsed <= 150 else 'finalization')
        obs = []
        for line in tail(run / 'observations.jsonl'):
            try:
                obs.append(json.loads(line))
            except ValueError:
                pass
        if obs:
            x = obs[-1]
            out.update(redis_alloc_GiB=x['info']['used_memory']/1024**3,
                       redis_rss_GiB=x['redis']['rss_bytes']/1024**3,
                       redis_freq_MHz=x['redis_freq_khz']/1000,
                       redis_affinity=x['redis']['allowed'], client_affinity=x['client']['allowed'],
                       swap_kib=x['redis']['vm_swap_kib']+x['client']['vm_swap_kib'],
                       cmdp_entries=x['cmdp']['entries'])
        if len(obs) >= 2:
            a, b = obs[-2:]
            seconds = b['monotonic'] - a['monotonic']
            for process in ('redis', 'client'):
                out[process + '_cpu_pct_recent'] = 100 * (b[process]['cpu_s'] - a[process]['cpu_s']) / seconds
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=pathlib.Path,
                        default=pathlib.Path('/home/yans3/cmdp-v2/redis-ycsb-study'))
    print(json.dumps(status(parser.parse_args().study), sort_keys=True))
