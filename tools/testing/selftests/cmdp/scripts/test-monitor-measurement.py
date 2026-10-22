#!/usr/bin/env python3
"""Exercise monitor-off child lifetime/resource accounting without CMD-P access."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('monitor', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    results = []

    def run(name, command, code=0):
        r = subprocess.run([str(a.monitor), '--monitor-off', '--'] + command,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=10)
        rows = [json.loads(s) for s in r.stdout.splitlines()]
        assert r.returncode == code, (name, r.returncode, r.stderr)
        assert [x['event'] for x in rows] == ['controller', 'target_exit', 'controller_exit'], rows
        c, t, end = rows
        assert c['monitor_enabled'] is False and c['thp_disabled_before_exec']
        assert t['exit_code'] == code and t['wait4_errno'] == 0
        assert c['controller_start_ns'] <= c['launch_ns'] <= c['exec_ack_ns'] <= t['exit_observed_ns'] <= end['now_ns']
        results.append({'name': name, 'pass': True, 'rows': rows, 'stderr': r.stderr})
        return r, t

    for i in range(20):
        run('fast-exit-' + str(i), ['/bin/true'])
    run('nonzero-exit', ['/bin/sh', '-c', 'exit 7'], 7)
    run('signal-exit', ['/bin/sh', '-c', 'kill -TERM $$'], 143)
    r, measured = run('target-resource-accounting', ['/usr/bin/python3', '-c',
        'import time; a=bytearray(64*1024*1024); print(open("/proc/self/status").read()); time.sleep(.05)'])
    assert measured['maxrss_kib'] >= 64 * 1024
    assert 'THP_enabled:\t0' in r.stderr
    r = subprocess.run([str(a.monitor), '--monitor-off', '--dirty-observe', '--', '/bin/true'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert r.returncode == 2 and not r.stdout
    results.append({'name': 'reject-conflicting-mode', 'pass': True})
    r = subprocess.run([str(a.monitor), '--monitor-off', '--', '/no/such/cmdp-target'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert r.returncode != 0
    results.append({'name': 'exec-failure', 'pass': True})
    child = subprocess.Popen([str(a.monitor), '--monitor-off', '--', '/bin/sleep', '30'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    first = json.loads(child.stdout.readline())
    start = time.monotonic()
    child.send_signal(signal.SIGTERM)
    out, err = child.communicate(timeout=4)
    rows = [first] + [json.loads(s) for s in out.splitlines()]
    assert child.returncode == 143 and rows[-2]['exit_code'] == 143, (rows, err)
    assert not Path('/proc/' + str(first['pid'])).exists()
    results.append({'name': 'interrupt-and-reap', 'pass': True, 'rows': rows,
                    'elapsed_seconds': time.monotonic() - start})
    a.output.write_text(json.dumps({'pass': True, 'checks': len(results), 'results': results}, indent=2) + '\n')
    print(json.dumps({'pass': True, 'checks': len(results)}))


if __name__ == '__main__':
    main()
