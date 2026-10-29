#!/usr/bin/env python3
"""Reproducible current-boot CPU frequency control and qualification (Python 3.6).

No BIOS, GRUB, kernel, voltage, package-power or C-state changes. Successful
qualification deliberately leaves the selected clock locked. Explicit restore
uses the immutable original snapshot; failures and interruption restore it.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback

SYS = Path('/sys/devices/system/cpu')
DEFAULT_ROOT = '/home/yans3/cmdp-v2/redis-ycsb-study/fixedfreq-v1'
ERROR_RE = re.compile(r'Hardware Error|Machine check|MCE:|BUG:|Oops:|soft lockup|hard LOCKUP|thermal.*critical|temperature above threshold', re.I)


def text(path):
    return Path(path).read_text().strip()


def cpulist(value):
    result = set()
    for item in value.replace(' ', ',').split(','):
        if not item:
            continue
        ends = item.split('-')
        result.update(range(int(ends[0]), int(ends[-1]) + 1))
    return sorted(result)


def stamp():
    return dict(epoch=time.time(), monotonic=time.monotonic(), utc=datetime.datetime.utcnow().isoformat() + 'Z')


def save(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise RuntimeError('Refusing to overwrite original snapshot: ' + str(path))
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    with open(str(tmp), 'w') as stream:
        os.chmod(str(tmp), 0o644)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(tmp), str(path))
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect():
    policies = []
    for path in sorted((SYS / 'cpufreq').glob('policy*'), key=lambda p: int(p.name[6:])):
        entry = dict(path=str(path), related_cpus=cpulist(text(path / 'related_cpus')))
        for name in ('scaling_driver', 'scaling_governor', 'scaling_min_freq', 'scaling_max_freq',
                     'scaling_available_frequencies', 'scaling_available_governors', 'bios_limit',
                     'cpuinfo_min_freq', 'cpuinfo_max_freq', 'boost'):
            if (path / name).exists():
                entry[name] = text(path / name)
        policies.append(entry)
    boosts = {}
    for path in [SYS / 'cpufreq' / 'boost'] + [Path(p['path']) / 'boost' for p in policies]:
        if path.exists():
            boosts[str(path)] = text(path)
    return dict(schema=1, captured=stamp(), hostname=os.uname().nodename,
                kernel=os.uname().release, boot_id=text('/proc/sys/kernel/random/boot_id'),
                online=cpulist(text(SYS / 'online')), policies=policies, boost_controls=boosts,
                limitation='acpi-cpufreq exposes nominal states and a turbo-request sentinel; '
                           'cpuinfo_max_freq is not a selectable stable clock. No raw ratio writes.')


def root_required():
    if os.geteuid() != 0:
        raise RuntimeError('Run this command through sudo; do not store a sudo password in a script.')


@contextlib.contextmanager
def locked(root):
    root_required()
    root.mkdir(parents=True, exist_ok=True)
    with open(str(root / 'experiment.lock'), 'a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another CPU-frequency operation holds the host lock.')
        yield


def load_state(root, create=True):
    path = root / 'state.json'
    if not path.exists():
        if not create:
            raise RuntimeError('Original snapshot is missing; refusing to invent a restore state.')
        save(path, inspect(), exclusive=True)
    state = json.loads(path.read_text())
    current = inspect()
    for key in ('boot_id', 'hostname', 'online'):
        if current[key] != state[key]:
            raise RuntimeError('Snapshot does not match current ' + key + '; refusing automatic writes.')
    if [p['path'] for p in current['policies']] != [p['path'] for p in state['policies']]:
        raise RuntimeError('CPU policy topology changed since snapshot.')
    for old, new in zip(state['policies'], current['policies']):
        if any(old[k] != new[k] for k in ('scaling_driver', 'related_cpus')):
            raise RuntimeError('CPU driver/topology changed since snapshot.')
    return state


def write(path, value):
    try:
        with open(str(path), 'w') as stream:
            stream.write(str(value) + '\n')
    except OSError as exc:
        raise OSError(exc.errno, '{} while writing {!r} to {}'.format(exc.strerror, value, path)) from exc


def set_bounds(path, minimum, maximum):
    oldmax = int(text(path / 'scaling_max_freq'))
    if minimum > oldmax:
        write(path / 'scaling_max_freq', maximum)
        write(path / 'scaling_min_freq', minimum)
    else:
        write(path / 'scaling_min_freq', minimum)
        write(path / 'scaling_max_freq', maximum)


def restore_state(root, state):
    errors = []
    # Restore boost first so previously permitted upper bounds can be restored.
    for path, value in state['boost_controls'].items():
        try:
            if text(path) != str(value):
                write(path, value)
        except Exception as exc:
            errors.append('{}: {}'.format(path, exc))
    for policy in state['policies']:
        try:
            path = Path(policy['path'])
            set_bounds(path, int(policy['scaling_min_freq']), int(policy['scaling_max_freq']))
            write(path / 'scaling_governor', policy['scaling_governor'])
        except Exception as exc:
            errors.append('{}: {}'.format(policy['path'], exc))
    current = inspect()
    expected_keys = ('scaling_min_freq', 'scaling_max_freq', 'scaling_governor')
    for old, new in zip(state['policies'], current['policies']):
        if any(old[k] != new[k] for k in expected_keys):
            errors.append('Restore readback mismatch: ' + old['path'])
    if current['boost_controls'] != state['boost_controls']:
        errors.append('Restore boost readback mismatch')
    save(root / 'restore-last.json', dict(time=stamp(), errors=errors, current=current))
    if errors:
        raise RuntimeError('RESTORE INCOMPLETE: ' + '; '.join(errors))


def check_locked(khz):
    state = inspect()
    errors = []
    if not state['policies']:
        errors.append('No cpufreq policies')
    for policy in state['policies']:
        for key, expected in (('scaling_governor', 'performance'), ('scaling_min_freq', str(khz)),
                              ('scaling_max_freq', str(khz))):
            if policy.get(key) != expected:
                errors.append('{} {}={}'.format(policy['path'], key, policy.get(key)))
    if not state['boost_controls'] or any(v != '0' for v in state['boost_controls'].values()):
        errors.append('Boost is not disabled in every exposed control')
    return errors, state


def apply(root, state, khz):
    for policy in state['policies']:
        if policy['scaling_driver'] != 'acpi-cpufreq':
            raise RuntimeError('This validated handoff supports acpi-cpufreq only.')
        if khz not in map(int, policy.get('scaling_available_frequencies', '').split()):
            raise RuntimeError('Frequency {} not advertised by {}'.format(khz, policy['path']))
        if khz > 2100000:
            raise RuntimeError('This current-boot handoff excludes the turbo sentinel and higher ratios.')
        if 'performance' not in policy['scaling_available_governors'].split():
            raise RuntimeError('Performance governor unavailable')
    if not state['boost_controls']:
        raise RuntimeError('No supported boost-disable interface')
    try:
        for path in state['boost_controls']:
            # Global disable propagates to policies. Local stores reject writes
            # while global boost is disabled, even when writing zero again.
            if text(path) != '0':
                write(path, 0)
        for policy in state['policies']:
            path = Path(policy['path'])
            set_bounds(path, khz, khz)
            write(path / 'scaling_governor', 'performance')
        errors, current = check_locked(khz)
        if errors:
            raise RuntimeError('; '.join(errors))
        save(root / 'applied.json', dict(status='locked_unqualified', selected_khz=khz,
                                       time=stamp(), original_state=str(root / 'state.json'), current=current))
    except BaseException:
        restore_state(root, state)
        raise


def health():
    counts = {}
    for path in SYS.glob('cpu*/thermal_throttle/*_throttle_count'):
        counts[str(path)] = int(text(path))
    for pattern in ('mc*/ce_count', 'mc*/ue_count'):
        for path in Path('/sys/devices/system/edac/mc').glob(pattern):
            counts[str(path)] = int(text(path))
    sensors = {}
    for hwmon in Path('/sys/class/hwmon').glob('hwmon*'):
        if not (hwmon / 'name').exists() or text(hwmon / 'name') != 'coretemp':
            continue
        for path in hwmon.glob('temp*_input'):
            prefix = str(path)[:-6]
            limits = [int(text(prefix + suffix)) for suffix in ('_max', '_crit') if Path(prefix + suffix).exists()]
            critical = int(text(prefix + '_crit')) - 5000 if Path(prefix + '_crit').exists() else 85000
            limit = min(limits + [critical])
            alarm = any(text(p) != '0' for p in Path(prefix).parent.glob(Path(prefix).name + '*alarm'))
            sensors[str(path)] = dict(value_millic=int(text(path)), limit_millic=limit, alarm=alarm)
    return dict(time=stamp(), counts=counts, sensors=sensors)


def health_errors(before, after):
    errors = []
    if not after['sensors']:
        errors.append('No coretemp sensors available')
    for path, sensor in after['sensors'].items():
        if sensor['alarm'] or sensor['value_millic'] >= sensor['limit_millic']:
            errors.append('CPU temperature alarm/limit: {} {}'.format(path, sensor))
    for path, count in after['counts'].items():
        if count > before['counts'].get(path, count):
            errors.append('Hardware throttle/error counter increased: ' + path)
    return errors


def kernel_log():
    return subprocess.check_output(['dmesg'], universal_newlines=True)


def new_kernel_errors(before, after):
    old = set(before.splitlines())
    return [line for line in after.splitlines() if line not in old and ERROR_RE.search(line)]


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def parse_turbostat(path, cpus, khz):
    data = {cpu: dict(samples=0, eligible=0, in_band=0, busy_sum=0.0, min_mhz=None, max_mhz=None) for cpu in cpus}
    header = None
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if 'CPU' in parts and 'Bzy_MHz' in parts:
            header = parts
            continue
        if not header or len(parts) < 3:
            continue
        row = dict(zip(header, parts))
        try:
            cpu, busy, mhz = int(row['CPU']), float(row['Busy%']), float(row['Bzy_MHz'])
        except (ValueError, KeyError):
            continue
        if cpu not in data:
            continue
        s = data[cpu]
        s['samples'] += 1
        s['busy_sum'] += busy
        if busy < 90:
            continue
        s['eligible'] += 1
        s['in_band'] += int(abs(mhz - khz / 1000.0) <= khz / 1000.0 * 0.01)
        s['min_mhz'] = mhz if s['min_mhz'] is None else min(s['min_mhz'], mhz)
        s['max_mhz'] = mhz if s['max_mhz'] is None else max(s['max_mhz'], mhz)
    for s in data.values():
        s['mean_busy_pct'] = s['busy_sum'] / s['samples'] if s['samples'] else 0
        s['eligible_fraction'] = s['eligible'] / s['samples'] if s['samples'] else 0
        s['in_band_fraction'] = s['in_band'] / s['eligible'] if s['eligible'] else 0
    return data


def qualify_candidate(root, state, khz, seconds, settle):
    apply(root, state, khz)
    out = root / 'qualification' / ('{}-{}'.format(datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'), khz))
    out.mkdir(parents=True)
    before = health()
    before_log = kernel_log()
    save(out / 'health-before.json', before)
    (out / 'dmesg-before.txt').write_text(before_log)
    problems = health_errors(before, before)
    if problems:
        raise RuntimeError('; '.join(problems))
    loader = monitor = None
    load_file = open(str(out / 'load.log'), 'w')
    monitor_file = open(str(out / 'turbostat-stderr.log'), 'w')
    result = dict(selected_khz=khz, started=stamp(), seconds=seconds, settle_seconds=settle,
                  criteria=dict(tolerance_fraction=0.01, minimum_in_band_fraction=0.99,
                                eligible_busy_pct=90, minimum_mean_busy_pct=95, minimum_eligible_fraction=0.95),
                  directory=str(out), status='running')
    save(out / 'result.json', result)
    try:
        loader = subprocess.Popen([str(root / 'bin/frequency_load'), str(seconds + settle + 120)] +
                                  [str(c) for c in state['online']], stdout=load_file, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        settle_end = time.monotonic() + settle
        while time.monotonic() < settle_end:
            if loader.poll() is not None:
                raise RuntimeError('Scalar loader exited during settling')
            current_health = health()
            errors = health_errors(before, current_health)
            if errors:
                raise RuntimeError('; '.join(errors))
            time.sleep(min(2, max(0, settle_end - time.monotonic())))
        load_file.flush()
        ready = set(map(int, re.findall(r'READY cpu=(\d+)', (out / 'load.log').read_text())))
        if ready != set(state['online']):
            raise RuntimeError('Not every online CPU has a pinned load worker')
        argv = [str(root / 'bin/turbostat'), '--quiet', '--no-perf', '--show',
                'CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp', '--interval', '1', '--num_iterations', str(seconds),
                '--out', str(out / 'turbostat.tsv')]
        result['turbostat_argv'] = argv
        result['measurement_started'] = stamp()
        save(out / 'result.json', result)
        monitor = subprocess.Popen(argv, stdout=monitor_file, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + seconds + max(90, seconds * 0.1)
        with open(str(out / 'health-samples.jsonl'), 'w') as sample_file:
            while monitor.poll() is None:
                if loader.poll() is not None:
                    raise RuntimeError('Scalar loader exited before qualification completed')
                if time.monotonic() > deadline:
                    raise RuntimeError('turbostat qualification deadline exceeded')
                current_health = health()
                sample_file.write(json.dumps(current_health, sort_keys=True) + '\n')
                sample_file.flush()
                errors = health_errors(before, current_health)
                policy_errors, current_policy = check_locked(khz)
                errors.extend(policy_errors)
                if current_policy['online'] != state['online']:
                    errors.append('Online CPU set changed during qualification')
                errors.extend(new_kernel_errors(before_log, kernel_log()))
                if errors:
                    raise RuntimeError('; '.join(errors))
                time.sleep(5)
        if monitor.returncode:
            raise RuntimeError('turbostat exited {}; inspect {}'.format(monitor.returncode, out))
        after_log = kernel_log()
        (out / 'dmesg-after.txt').write_text(after_log)
        after = health()
        save(out / 'health-after.json', after)
        problems = health_errors(before, after) + new_kernel_errors(before_log, after_log)
        if problems:
            raise RuntimeError('; '.join(problems))
        stats = parse_turbostat(out / 'turbostat.tsv', state['online'], khz)
        result['per_cpu'] = stats
        insufficient = [cpu for cpu, s in stats.items() if s['samples'] < math.floor(seconds * 0.95) or
                        s['mean_busy_pct'] < 95 or s['eligible_fraction'] < 0.95]
        if insufficient:
            raise RuntimeError('Insufficient all-core load or samples on CPUs: ' + str(insufficient))
        failed = [cpu for cpu, s in stats.items() if s['in_band_fraction'] < 0.99]
        result['status'] = 'frequency_failed' if failed else 'passed'
        result['failed_cpus'] = failed
    except BaseException as exc:
        result['status'] = 'aborted'
        result['error'] = '{}: {}'.format(type(exc).__name__, exc)
        raise
    finally:
        stop_process(monitor)
        stop_process(loader)
        load_file.close()
        monitor_file.close()
        result['ended'] = stamp()
        save(out / 'result.json', result)
    return result


def qualify(root, state, args):
    if not (root / 'build/manifest.json').exists():
        raise RuntimeError('Run build-tools first')
    if not Path('/dev/cpu/0/msr').exists():
        raise RuntimeError('MSR module is not loaded. Run sudo modprobe msr once before qualification.')
    candidates = sorted(set.intersection(*[set(map(int, p['scaling_available_frequencies'].split()))
                                          for p in state['policies']]), reverse=True)
    candidates = [k for k in candidates if args.minimum_khz <= k <= args.start_khz and k <= 2100000 and k % 100000 == 0]
    if not candidates:
        raise RuntimeError('No common non-turbo frequency candidates')
    profile = dict(schema=1, status='qualifying', boot_id=state['boot_id'], hostname=state['hostname'],
                   kernel=state['kernel'], original_state=str(root / 'state.json'), started=stamp(),
                   controller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   candidates_khz=candidates, attempts=[], tools=json.loads((root / 'build/manifest.json').read_text()),
                   restore_command='sudo python3 {} restore --root {}'.format(Path(__file__).resolve(), root),
                   persistence='current boot only; no GRUB/service changes; success leaves clocks locked')
    save(root / 'profile.json', profile)
    try:
        for khz in candidates:
            print('Qualifying {} kHz on {} CPUs for {} seconds'.format(khz, len(state['online']), args.seconds), flush=True)
            result = qualify_candidate(root, state, khz, args.seconds, args.settle_seconds)
            profile['attempts'].append(result)
            if result['status'] == 'passed':
                profile.update(status='qualified_locked', selected_khz=khz, ended=stamp())
                save(root / 'profile.json', profile)
                print(json.dumps(dict(status=profile['status'], selected_khz=khz, profile=str(root / 'profile.json')), sort_keys=True), flush=True)
                return
            save(root / 'profile.json', profile)
        raise RuntimeError('No candidate passed qualification')
    except BaseException as exc:
        profile.update(status='failed_restoring', error='{}: {}'.format(type(exc).__name__, exc), ended=stamp())
        save(root / 'profile.json', profile)
        try:
            restore_state(root, state)
            profile['status'] = 'failed_restored'
        except BaseException as restore_exc:
            profile['status'] = 'failed_restore_incomplete'
            profile['restore_error'] = str(restore_exc)
            raise
        finally:
            save(root / 'profile.json', profile)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'build-tools', 'snapshot', 'apply', 'status', 'qualify', 'restore'])
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--khz', type=int, default=2100000)
    parser.add_argument('--start-khz', type=int, default=2100000)
    parser.add_argument('--minimum-khz', type=int, default=1000000)
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--settle-seconds', type=int, default=20)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.seconds <= 0 or args.settle_seconds < 0:
        parser.error('Invalid qualification duration')
    if args.command == 'inspect':
        print(json.dumps(inspect(), indent=2, sort_keys=True))
        return
    if args.command == 'build-tools':
        subprocess.check_call([str(Path(__file__).with_name('build-frequency-tools.sh')), str(root)])
        return
    if args.command == 'status':
        profile_path = root / 'profile.json'
        profile = json.loads(profile_path.read_text()) if profile_path.exists() else None
        khz = profile.get('selected_khz', args.khz) if profile else args.khz
        errors, current = check_locked(khz)
        if profile and profile['boot_id'] != current['boot_id']:
            errors.append('Profile boot ID mismatch')
        print(json.dumps(dict(profile=profile, lock_verified=not errors, errors=errors, current=current), indent=2, sort_keys=True))
        if errors:
            sys.exit(1)
        return
    with locked(root):
        root.mkdir(parents=True, exist_ok=True)
        state = load_state(root, create=args.command != 'restore')
        if args.command == 'snapshot':
            print(str(root / 'state.json'))
        elif args.command == 'apply':
            apply(root, state, args.khz)
            print(json.dumps(dict(status='locked_unqualified', selected_khz=args.khz)))
        elif args.command == 'restore':
            restore_state(root, state)
            if (root / 'profile.json').exists():
                profile = json.loads((root / 'profile.json').read_text())
                profile.update(status='restored', restored=stamp())
                save(root / 'profile.json', profile)
            print('Original current-boot policy restored.')
        else:
            qualify(root, state, args)


def interrupted(signum, frame):
    raise KeyboardInterrupt('signal ' + str(signum))


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        traceback.print_exc()
        sys.exit(1)
