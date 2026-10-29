#!/usr/bin/env python3
"""Pure runner checks plus replay of an optional real boundary-smoke trace."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--boundary-smoke', type=Path)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location('runner',
        str(Path(__file__).with_name('redis-ycsb-fixedfreq.py')))
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    results = []

    jobs = list(r.matrix())
    assert len(jobs) == len(set(x[0] for x in jobs)) == 27
    for workload in ('A', 'B', 'C'):
        for mode in ('off', 'sampling', 'arm'):
            assert sum(w == workload and m == mode for _, w, m in jobs) == 3
    results.append('27 unique balanced jobs')

    class Helper:
        @staticmethod
        def ycsb_argv(directory, workload, count, threads, cores, seconds):
            assert threads == cores == 8 and seconds == 625
            return ['java', '-Xms4g', '-Xmx4g', '-threads', str(threads), '-p',
                    'hdrhistogram.fileoutput=true'], {
                    'measurementtype': 'hdrhistogram', 'status.interval': 1,
                    'hdrhistogram.fileoutput': 'true', 'maxexecutiontime': seconds}
    r.B = Helper
    argv, props = r.ycsb_command(Path('/example/run'), 'B', 11500000, 20, 600)
    assert '-Xms12g' in argv and '-Xmx12g' in argv
    assert argv[argv.index('-threads') + 1] == '8'
    assert props['target'] == 70000 and props['measurement.interval'] == 'both'
    assert props['measurementtype'] == 'raw' and props['measurement.raw.no_summary'] == 'true'
    assert not any(k.startswith('hdrhistogram.') for k in props)
    results.append('8 workers, 70k target, raw both, 12GiB heap')

    good = '\n'.join([
        '[OVERALL], RunTime(ms), 625000',
        '[READ], Total Operations, 100', '[READ], Return=OK, 100',
        '[Intended-READ], Total Operations, 100',
        '[UPDATE], Total Operations, 10', '[UPDATE], Return=OK, 10',
        '[Intended-UPDATE], Total Operations, 10']) + '\n'
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / 'ycsb.log'
        log.write_text(good)
        assert r.export_report(log)['READ']['Total Operations'] == 100
        for bad in (good + '[READ], Return=ERROR, 1\n',
                    good.replace('[READ], Return=OK, 100', '[READ], Return=OK, 99'),
                    good.replace('[Intended-READ], Total Operations, 100',
                                 '[Intended-READ], Total Operations, 99'),
                    good.replace('[OVERALL], RunTime(ms), 625000\n', '')):
            log.write_text(bad)
            try:
                r.export_report(log)
            except AssertionError:
                pass
            else:
                raise AssertionError('bad export was accepted')
    results.append('export errors/count mismatch/incomplete export rejected')

    with tempfile.TemporaryDirectory() as td:
        directory = Path(td)
        raw = directory / 'latency.raw'
        raw.write_text('READ,1793245500000,10\n')
        receipt = {'passed': True, 'files_sha256': {'latency.raw': r.sha(raw)}}
        (directory / 'capture-receipt.json').write_text(json.dumps(receipt))
        assert r.verify_capture_receipt(directory)['passed']
        raw.write_text('READ,1793245500000,99\n')  # Same count/shape; changed latency.
        for function in (r.verify_capture_receipt, r.analyze_run):
            try:
                function(directory)
            except AssertionError as error:
                assert 'Changed capture artifact: latency.raw' in str(error)
            else:
                raise AssertionError('accepted an altered recorded latency')
        raw.write_text('READ,1793245500000,10\n')
        assert r.verify_capture_receipt(directory)['passed']
        raw.unlink()
        try:
            r.verify_capture_receipt(directory)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError('accepted a missing captured file')
    results.append('receipt verifies original; changed-value/missing raw evidence rejected before analysis')

    with tempfile.TemporaryDirectory() as td:
        originals = r.ROOT, r.os.geteuid, r.subprocess.check_call, r.emit, r.process_identity
        calls = []
        try:
            r.ROOT = Path(td)
            r.os.geteuid = lambda: 0
            r.emit = lambda *a, **k: None
            r.process_identity = lambda pid: {'pid': pid, 'start_ticks': 1, 'boot_id': 'test'}
            r.subprocess.check_call = lambda argv, **kw: calls.append(argv)
            (r.ROOT / 'config.json').write_text(json.dumps(r.CONFIG))
            r.suite()
            pilots = [x for x in calls if len(x) > 2 and x[2] == 'pilot']
            assert len(pilots) == 11
            assert sum('--no-frequency-telemetry' in x for x in pilots) == 3
            assert sorted(int(x[x.index('--seconds') + 1]) for x in pilots) == [40] * 4 + [120] * 6 + [600]
            assert calls[11][2] == 'resume' and calls[12][2:] == ['analyze', '--jobs', '8']
            assert calls[13][2] == 'campaign' and len(calls) == 14
            calls[:] = []
            def fail_third(argv, **kwargs):
                calls.append(argv)
                if len(calls) == 3:
                    raise subprocess.CalledProcessError(7, argv)
            r.subprocess.check_call = fail_third
            try:
                r.suite()
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError('suite ignored a failed pilot')
            assert len(calls) == 3 and all(x[2] == 'pilot' for x in calls)
        finally:
            r.ROOT, r.os.geteuid, r.subprocess.check_call, r.emit, r.process_identity = originals
    results.append('suite schedules 11 pilots then capture/analysis; pilot failure stops subsequent stages')

    with tempfile.TemporaryDirectory() as td:
        originals = (r.ROOT, r.STUDY, r.B, r.CONFIG, r.selected_sources,
                     r.emit, r.load_helper, r.sys.argv)
        emitted = []
        class PrepareHelper:
            @staticmethod
            def mkdir(path):
                Path(path).mkdir(parents=True, exist_ok=True)
            @staticmethod
            def command(argv):
                return 'test-source-commit'
        try:
            r.STUDY = Path(td)
            r.ROOT = r.STUDY / 'fixedfreq-v1'
            (r.ROOT / 'bin').mkdir(parents=True)
            golden = r.STUDY / 'artifacts' / 'golden'
            golden.mkdir(parents=True)
            (golden / 'dump.rdb').write_bytes(b'test-golden-database')
            (golden / 'manifest.json').write_text(json.dumps({
                'rdb_sha256': r.sha(golden / 'dump.rdb'), 'recordcount': 1}))
            r.B = PrepareHelper
            r.CONFIG = dict(originals[3])
            assert r.CONFIG['frequency_contract'] == 'locked-clock'
            r.selected_sources = lambda: {'test-source': 'original-hash'}
            r.emit = lambda event, **values: emitted.append((event, values))
            r.load_helper = lambda: None
            r.sys.argv = ['runner', 'prepare']
            r.main()
            config = json.loads((r.ROOT / 'config.json').read_text())
            assert config['frequency_contract'] == 'locked-clock'
            r.sys.argv = ['runner', 'prepare', '--frequency-contract', 'fixed-policy']
            r.main()
            config = json.loads((r.ROOT / 'config.json').read_text())
            assert config['frequency_contract'] == 'fixed-policy'
            archived = list((r.ROOT / 'config-manifests').glob('*.json'))
            assert len(archived) == 1
            assert json.loads(archived[0].read_text())['frequency_contract'] == 'locked-clock'
            r.CONFIG['frequency_contract'] = 'locked-clock'
            r.sys.argv = ['runner', 'status']
            r.main()
            assert emitted[-1][1]['frequency_contract'] == 'fixed-policy'
            assert r.CONFIG['frequency_contract'] == 'fixed-policy'
            r.sys.argv = ['runner', 'status', '--frequency-contract', 'locked-clock']
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    r.main()
                except SystemExit as error:
                    assert error.code == 2
                else:
                    raise AssertionError('contract override was accepted outside prepare')
            (r.ROOT / 'campaign-v1' / 'A-off-r1').mkdir()
            r.sys.argv = ['runner', 'prepare', '--frequency-contract', 'locked-clock']
            try:
                r.main()
            except AssertionError as error:
                assert 'Cannot change configuration over campaign evidence' in str(error)
            else:
                raise AssertionError('contract was changed over campaign evidence')
            assert json.loads((r.ROOT / 'config.json').read_text())['frequency_contract'] == 'fixed-policy'
            r.selected_sources = lambda: {'test-source': 'changed-hash'}
            r.sys.argv = ['runner', 'prepare', '--frequency-contract', 'fixed-policy']
            try:
                r.main()
            except AssertionError as error:
                assert 'Cannot refreeze modified sources over campaign evidence' in str(error)
            else:
                raise AssertionError('sources were refrozen over campaign evidence')
            assert json.loads((r.ROOT / 'source-manifest.json').read_text())['hashes'] == {'test-source': 'original-hash'}
        finally:
            (r.ROOT, r.STUDY, r.B, r.CONFIG, r.selected_sources,
             r.emit, r.load_helper, r.sys.argv) = originals
    results.append('strict contract default, explicit pre-campaign selection/archive, persisted selection, post-start changes refused')

    if args.boundary_smoke:
        rows = [json.loads(s) for s in (args.boundary_smoke / 'monitor.jsonl').read_text().splitlines()]
        def group(label):
            first = next(i for i, x in enumerate(rows) if x['event'] == 'boundary_begin' and x['label'] == label)
            last = next(i for i, x in enumerate(rows) if x['event'] == 'boundary_end' and x['label'] == label)
            selected = copy.deepcopy(rows[first:last + 1])
            return {'label': label, 'requested': selected[0], 'completed': selected[-1],
                    'controllers': {'cmdp': selected},
                    'tlb': {'cpus': {str(i): 1000 for i in range(52)},
                            'begin': selected[0], 'end': selected[-1]}}
        start, end = group('measurement_start'), group('measurement_end')
        observed = r.counter_summary(start, end, 'sampling')
        assert observed['arm_events'] == observed['wp_events'] == 0
        for i in range(52):
            end['tlb']['cpus'][str(i)] += i
        z = next(x for x in end['controllers']['cmdp'] if 'terminals' in x)
        a = next(x for x in start['controllers']['cmdp'] if 'terminals' in x)
        z['first_arms'], z['rearms'] = a['first_arms'] + 10, a['rearms'] + 3
        z['terminals']['write'] = a['terminals']['write'] + 7
        z['active'] = 6
        ab = next(x for x in start['controllers']['cmdp'] if x['event'] == 'batch_status')
        zb = next(x for x in end['controllers']['cmdp'] if x['event'] == 'batch_status')
        for key, value in [('flush_calls', 8), ('shared_flush_calls', 3),
                           ('dirty_only_flush_calls', 3), ('wp_only_flush_calls', 2)]:
            zb[key] = ab[key] + value
        observed = r.counter_summary(start, end, 'arm')
        assert observed['arm_events'] == 13 and observed['wp_events'] == 7
        assert observed['flush_calls'] == 8 and observed['shared_flush_calls'] == 3
        assert observed['tlb_shootdown_interrupts']['all_host'] == sum(range(52))
        assert observed['tlb_shootdown_interrupts']['redis_cpu1'] == 1
        try:
            r.counter_summary(start, end, 'sampling')
        except AssertionError:
            pass
        else:
            raise AssertionError('sampling accepted ARM events')
        results.append('real boundary schema, shared flush once, WP/ARM deltas, sampling invariant')

        frequency_rows = [json.loads(s) for s in
            (args.boundary_smoke / 'frequency.jsonl').read_text().splitlines()]
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            def write_traces(monitor, frequency):
                (directory / 'cmdp.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in monitor))
                (directory / 'frequency.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in frequency))
                (directory / 'cmdp.err').write_text('')
            write_traces(rows, frequency_rows)
            r.validate_control(directory)
            mutations = [
                ('final', 'active', 1), ('final', 'resource_failures', 1),
                ('final', 'reports_dropped', 1), ('final', 'state', 2),
                ('final', 'stop_reason', 'target_exit'),
                ('cpu_status', 'cpu', 1), ('cpu_status', 'last_cpu', 1),
                ('cpu_status', 'error', 1), ('cpu_status', 'wrong_cpu_work_items', 1),
                ('cpu_status', 'offline_events', 1), ('stop_return', 'errno', 5),
                ('controller_exit', 'exit_code', 0), ('controller_exit', 'exit_code', 1)]
            for event, key, value in mutations:
                bad = copy.deepcopy(rows)
                next(x for x in reversed(bad) if x['event'] == event)[key] = value
                write_traces(bad, frequency_rows)
                try:
                    r.validate_control(directory)
                except AssertionError:
                    pass
                else:
                    raise AssertionError('accepted bad %s.%s' % (event, key))
            for missing in ('stop_return', 'final', 'cpu_status', 'controller_exit'):
                write_traces([x for x in rows if x['event'] != missing], frequency_rows)
                try:
                    r.validate_control(directory)
                except AssertionError:
                    pass
                else:
                    raise AssertionError('accepted incomplete monitor trace: ' + missing)
            for bad in ([x for x in frequency_rows if x['event'] != 'frequency_exit'],
                        frequency_rows + [frequency_rows[-1]],
                        [dict(x, exit_code=1) if x['event'] == 'frequency_exit' else x
                         for x in frequency_rows]):
                write_traces(rows, bad)
                try:
                    r.validate_control(directory)
                except AssertionError:
                    pass
                else:
                    raise AssertionError('accepted failed/incomplete frequency trace')
            write_traces(rows + [next(x for x in rows if x['event'] == 'controller_exit')], frequency_rows)
            try:
                r.validate_control(directory)
            except AssertionError:
                pass
            else:
                raise AssertionError('accepted duplicate controller exit')
            write_traces(rows, frequency_rows)
            (directory / 'cmdp.err').write_text('progress during stop: Input/output error\n')
            try:
                r.validate_control(directory)
            except AssertionError:
                pass
            else:
                raise AssertionError('accepted masked controller stderr failure')
            results.append('clean final accepted; 22 failed/incomplete controller traces rejected')

        # A zero exit is still premature when a new boundary is requested.
        control = object.__new__(r.Control)
        control.name = 'test-exited-controller'
        control.p = subprocess.Popen(['/bin/true'])
        control.p.wait()
        control.write_fd = -1
        try:
            control.request('measurement_end')
        except AssertionError:
            pass
        else:
            raise AssertionError('accepted a prematurely exited controller')
        results.append('premature successful controller exit rejected')
    print(json.dumps({'pass': True, 'checks': results}))


if __name__ == '__main__':
    main()
