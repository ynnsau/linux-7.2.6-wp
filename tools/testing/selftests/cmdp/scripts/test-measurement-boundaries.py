#!/usr/bin/env python3
"""Privileged, small-memory integration check; no frequency policy changes."""
import argparse
import ctypes
import json
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


def no_thp():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(41, 1, 0, 0, 0):  # PR_SET_THP_DISABLE
        raise OSError(ctypes.get_errno(), "PR_SET_THP_DISABLE")


class Reader:
    def __init__(self, process):
        self.rows = []
        self.queue = queue.Queue()
        self.process = process
        self.thread = threading.Thread(target=self.read)
        self.thread.daemon = True
        self.thread.start()

    def read(self):
        for line in self.process.stdout:
            try:
                row = json.loads(line)
            except Exception as error:
                self.queue.put(error)
                return
            self.rows.append(row)
            self.queue.put(row)
        self.queue.put(None)

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            row = self.queue.get(timeout=max(0.001, deadline - time.monotonic()))
            assert row is not None, "early process exit"
            if isinstance(row, Exception):
                raise row
            if predicate(row):
                return row


def verify_boundaries(rows, label, expected_event):
    begin = next(i for i, r in enumerate(rows) if r['event'] == 'boundary_begin' and r['label'] == label)
    end = next(i for i, r in enumerate(rows) if r['event'] == 'boundary_end' and r['label'] == label)
    assert end > begin and rows[end]['ok']
    payload = rows[begin + 1:end]
    assert any(r['event'] == expected_event for r in payload)
    for row in payload:
        stamp = row.get('now_ns', row.get('monotonic_ns'))
        assert rows[begin]['monotonic_ns'] <= stamp <= rows[end]['anchor_end_ns']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bin-dir', type=Path, required=True)
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    fixture = subprocess.Popen(['taskset', '-c', '1', str(a.fixture), '--mode', 'never',
                                '--seconds', '30', '--threads', '8'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, preexec_fn=no_thp)
    results = []
    try:
        assert fixture.stdout.readline().startswith('fixture pid=')
        commands = [
            ('monitor', [str(a.bin_dir / 'cmdp_monitor'), '--pid', str(fixture.pid),
                         '--dirty-observe', '--scanner-cpu', '0', '--poll-ms', '60000',
                         '--control-fd', '0'], 'status', 'boundary_status'),
            ('frequency', [str(a.bin_dir / 'frequency_collect'), '--interval-ms', '60000',
                           '--control-fd', '0'], 'boundary_end', 'frequency')]
        for name, command, ready_event, payload in commands:
            err = (a.output / (name + '.stderr')).open('w')
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, universal_newlines=True, bufsize=1)
            reader = Reader(proc)
            try:
                reader.until(lambda r: r['event'] == ready_event)
                proc.stdin.write('snap')
                proc.stdin.flush()
                time.sleep(0.02)
                begin = time.monotonic()
                proc.stdin.write('shot measurement_start\nsnapshot measurement_end\n')
                proc.stdin.flush()
                reader.until(lambda r: r['event'] == 'boundary_end' and r['label'] == 'measurement_end')
                elapsed = time.monotonic() - begin
                assert elapsed < 1.0, (name, elapsed)
                proc.stdin.close()
                time.sleep(0.1)
                assert proc.poll() is None, 'EOF incorrectly ended observation'
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                reader.thread.join(timeout=2)
                assert proc.returncode == (143 if name == 'monitor' else 0)
                verify_boundaries(reader.rows, 'measurement_start', payload)
                verify_boundaries(reader.rows, 'measurement_end', payload)
                if name == 'monitor':
                    final = next(r for r in reader.rows if r['event'] == 'final')
                    assert final['active'] == final['resource_failures'] == final['reports_dropped'] == 0
                    assert fixture.poll() is None, 'attach-mode termination signaled target'
                else:
                    cpus = [r['cpu'] for r in reader.rows if r['event'] == 'frequency' and r['label'] == 'measurement_start']
                    config = next(r for r in reader.rows if r['event'] == 'frequency_config')
                    assert len(cpus) == len(set(cpus)) == config['cpu_count']
                (a.output / (name + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in reader.rows))
                results.append({'name': name, 'pass': True, 'two_boundaries_seconds': elapsed})
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                err.close()
        report = {'pass': True, 'results': results}
        (a.output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    finally:
        if fixture.poll() is None:
            fixture.terminate()
        fixture.wait(timeout=5)


if __name__ == '__main__':
    main()
