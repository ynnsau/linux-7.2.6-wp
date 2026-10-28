#!/usr/bin/env python3
"""Standalone Redis/YCSB baseline; root supervises unprivileged workloads.

Python 3.6 compatible. No CMD-P control ioctl or debugfs writes are made.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import pathlib
import pwd
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
import traceback

P = pathlib.Path
GIB = 1024 ** 3
HZ = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')
STUDY = P('/home/yans3/cmdp-v2/redis-ycsb-study')
OWNER = pwd.getpwnam('yans3')
SOURCE = STUDY / 'src'
REDIS = SOURCE / 'redis/src/redis-server'
YCSB = SOURCE / 'YCSB'
ART = STUDY / 'artifacts'
JAVA = '/usr/lib/jvm/java-11-openjdk-amd64/bin/java'
PORT = 6380
EXPECTED_KERNEL = '7.2.6-cmdp-cpu-production'


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def mkdir(p):
    p = P(p)
    p.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(str(p), OWNER.pw_uid, OWNER.pw_gid)
    return p


def jsave(p, value):
    p = P(p)
    mkdir(p.parent)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    tmp.replace(p)


def digest(p):
    h = hashlib.sha256()
    with open(str(p), 'rb') as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def command(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], universal_newlines=True, **kwargs).strip()


def demote():
    os.setgroups([])
    os.setgid(OWNER.pw_gid)
    os.setuid(OWNER.pw_uid)


def spawn(args, directory, log, cpus, node):
    argv = ['numactl', '--physcpubind=' + cpus, '--membind=' + str(node)] + [str(x) for x in args]
    env = dict(os.environ, LC_ALL='C', TZ='UTC')
    f = open(str(log), 'wb')
    allowed = set()
    for group in cpus.split(','):
        bounds = [int(x) for x in group.split('-')]
        allowed.update(range(bounds[0], bounds[-1] + 1))
    def child_setup():
        os.sched_setaffinity(0, allowed)
        if os.geteuid() == 0:
            demote()
    p = subprocess.Popen(argv, cwd=str(directory), env=env, stdout=f, stderr=subprocess.STDOUT,
                         preexec_fn=child_setup, start_new_session=True)
    f.close()
    return p


class Redis:
    def __init__(self, timeout=10):
        self.sock = socket.create_connection(('127.0.0.1', PORT), timeout)
        self.f = self.sock.makefile('rb')

    def close(self):
        self.f.close()
        self.sock.close()

    def read(self):
        line = self.f.readline()
        if not line:
            raise EOFError('Redis connection closed')
        kind, body = line[:1], line[1:-2]
        if kind == b'+':
            return body.decode()
        if kind == b'-':
            raise RuntimeError(body.decode())
        if kind == b':':
            return int(body)
        if kind == b'$':
            n = int(body)
            if n < 0:
                return None
            b = self.f.read(n)
            assert self.f.read(2) == b'\r\n'
            return b
        if kind == b'*':
            return [self.read() for _ in range(int(body))]
        raise RuntimeError('Unexpected RESP response: ' + repr(line))

    def call(self, *args):
        parts = [a if isinstance(a, bytes) else str(a).encode() for a in args]
        data = b'*%d\r\n' % len(parts)
        for a in parts:
            data += b'$%d\r\n' % len(a) + a + b'\r\n'
        self.sock.sendall(data)
        return self.read()

    def info(self, *sections):
        raw = self.call('INFO', *(sections or ('ALL',))).decode()
        d = {}
        for line in raw.splitlines():
            if ':' in line and not line.startswith('#'):
                k, v = line.split(':', 1)
                try:
                    v = float(v) if '.' in v and re.match(r'^-?[\d.]+$', v) else int(v)
                except ValueError:
                    pass
                d[k] = v
        return d


def cmdp_stats():
    d = {}
    for line in P('/sys/kernel/debug/cmdp/stats').read_text().splitlines():
        k, v = line.split()
        d[k] = int(v)
    assert all(d[k] == 0 for k in ('entries', 'managed', 'isolated', 'waiters'))
    return d


def policies():
    def read(p):
        return P(p).read_text().strip()
    return {'kernel': os.uname().release,
            'boot_id': read('/proc/sys/kernel/random/boot_id'),
            'numa_balancing': read('/proc/sys/kernel/numa_balancing'),
            'thp': read('/sys/kernel/mm/transparent_hugepage/enabled'),
            'overcommit_memory': read('/proc/sys/vm/overcommit_memory'),
            'governors': {str(i): read('/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor' % i)
                          for i in range(52)}}


def proc(pid):
    stat = P('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
    status = {}
    for line in P('/proc/%d/status' % pid).read_text().splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            status[k] = v.strip()
    return {'cpu_s': (int(stat[11]) + int(stat[12])) / HZ,
            'rss_bytes': int(stat[21]) * PAGE,
            'minor_faults': int(stat[7]), 'major_faults': int(stat[9]),
            'vm_swap_kib': int(status.get('VmSwap', '0 kB').split()[0]),
            'threads': int(status['Threads']), 'allowed': status['Cpus_allowed_list']}


def affinity(pid, expected):
    seen = 0
    for p in P('/proc/%d/task' % pid).iterdir():
        try:
            mask = os.sched_getaffinity(int(p.name))
        except ProcessLookupError:
            continue
        if mask != set(expected):
            raise RuntimeError('Unexpected affinity for %s: %s (expected %s)' % (p, sorted(mask), expected))
        seen += 1
    assert seen > 0
    return seen


def cpu_set(n):
    return list(range(26, 26 + n))


def ycsb_cp():
    jars = list((YCSB / 'core/target').glob('*.jar')) + list((YCSB / 'core/target/dependency').glob('*.jar'))
    jars += list((YCSB / 'redis/target').glob('*.jar')) + list((YCSB / 'redis/target/dependency').glob('*.jar'))
    jars = [p for p in jars if not any(x in p.name for x in ('sources', 'javadoc', 'tests'))]
    assert any('jedis' in p.name for p in jars), 'Build YCSB with -Psource-run'
    assert any('HdrHistogram' in p.name for p in jars)
    return ':'.join(str(p) for p in sorted(jars))


def ycsb_argv(directory, workload, count, threads, cores, seconds=0, load=False, insertstart=0, insertcount=None):
    props = {'redis.host': '127.0.0.1', 'redis.port': PORT, 'redis.timeout': 10000,
             'recordcount': count, 'operationcount': 0, 'maxexecutiontime': seconds,
             'fieldcount': 10, 'fieldlength': 100, 'fieldlengthdistribution': 'constant',
             'readallfields': 'true', 'writeallfields': 'false', 'insertorder': 'hashed', 'zeropadding': 1,
             'measurementtype': 'hdrhistogram', 'hdrhistogram.fileoutput': 'true',
             'hdrhistogram.output.path': str(directory) + '/', 'status.interval': 1}
    if load:
        props.update({'insertstart': insertstart, 'insertcount': insertcount if insertcount is not None else count})
    args = [JAVA, '-Xms4g', '-Xmx4g', '-XX:+UseG1GC', '-XX:ActiveProcessorCount=%d' % cores,
            '-XX:-UseLargePages', '-XX:-UseTransparentHugePages', '-Duser.timezone=UTC',
            '-Xlog:gc*:file=%s:time,uptime,tags' % (directory / 'gc.log'),
            '-cp', ycsb_cp(), 'site.ycsb.Client', '-load' if load else '-t', '-db', 'site.ycsb.db.RedisClient',
            '-s', '-threads', str(threads), '-P', str(YCSB / ('workloads/workload' + workload.lower()))]
    for k, v in sorted(props.items()):
        args.extend(['-p', '%s=%s' % (k, v)])
    return args, props


def key(i):
    h = 0xcbf29ce484222325
    for _ in range(8):
        h = ((h ^ (i & 255)) * 1099511628211) & ((1 << 64) - 1)
        i >>= 8
    if h & (1 << 63):
        h -= 1 << 64
    return 'user' + str(abs(h))


def sample_records(r, count):
    ids = sorted(set([0, 1, count - 1, count // 2] + [(i * 104729) % count for i in range(1, 33)]))
    samples = {}
    for i in ids:
        k = key(i)
        a = r.call('HGETALL', k)
        fields = dict(zip(a[::2], a[1::2]))
        assert set(fields) == set(('field%d' % j).encode() for j in range(10)), (k, fields.keys())
        assert all(len(v) == 100 for v in fields.values()), k
        samples[k] = hashlib.sha256(b''.join(k + b'\0' + fields[k] for k in sorted(fields))).hexdigest()
    return samples


def validate_db(r, count, samples=None):
    assert r.call('DBSIZE') == count + 1
    assert r.call('ZCARD', '_indices') == count
    current = sample_records(r, count)
    if samples is not None:
        assert current == samples, 'Restored record fingerprints differ'
    info = r.info()
    assert info['loading'] == 0 and info['aof_enabled'] == 0
    assert info['rdb_bgsave_in_progress'] == 0 and info['evicted_keys'] == 0
    assert info['expired_keys'] == 0
    return {'info': info, 'samples': current}


def start_redis(directory, database):
    mkdir(directory)
    mkdir(database)
    config = '\n'.join(['bind 127.0.0.1', 'protected-mode yes', 'port %d' % PORT,
                         'daemonize no', 'save ""', 'appendonly no', 'maxmemory 0',
                         'maxmemory-policy noeviction', 'io-threads 1', 'activedefrag no',
                         'jemalloc-bg-thread no', 'disable-thp yes', 'server-cpulist 1', 'bio-cpulist 1',
                         'dir ' + str(database), 'dbfilename dump.rdb', 'loglevel notice',
                         'logfile ""', 'pidfile ' + str(directory / 'redis.pid')]) + '\n'
    (directory / 'redis.conf').write_text(config)
    try:
        s = socket.create_connection(('127.0.0.1', PORT), 1)
        s.close()
        raise RuntimeError('Port 6380 is occupied; refusing to touch existing service')
    except ConnectionRefusedError:
        pass
    t0 = time.monotonic()
    p = spawn([REDIS, directory / 'redis.conf'], directory, directory / 'redis.log', '1', 0)
    for _ in range(1800):
        if p.poll() is not None:
            raise RuntimeError('Redis failed: ' + (directory / 'redis.log').read_text()[-2000:])
        try:
            r = Redis(5)
            info = r.info('server', 'persistence')
            if info['loading'] == 0:
                assert info['process_id'] == p.pid
                assert r.call('PING') == 'PONG'
                affinity(p.pid, [1])
                return p, r, time.monotonic() - t0
            r.close()
        except (ConnectionRefusedError, RuntimeError, socket.timeout):
            pass
        time.sleep(0.2)
    p.terminate()
    p.wait(timeout=30)
    raise RuntimeError('Redis restore exceeded six minutes')


def stop_redis(p, r):
    if p.poll() is None:
        try:
            info = r.info('server')
            assert info['process_id'] == p.pid
            r.call('SHUTDOWN', 'NOSAVE')
        except EOFError:
            pass
        p.wait(timeout=30)
    r.close()


def export_parse(log):
    out = {}
    for line in P(log).read_text(errors='replace').splitlines():
        m = re.match(r'^\[([^]]+)\], ([^,]+), (.+)$', line)
        if m:
            group, name, value = m.groups()
            try:
                value = float(value)
            except ValueError:
                pass
            out.setdefault(group, {})[name] = value
    assert out and 'OVERALL' in out, 'No YCSB completion report'
    for group, fields in out.items():
        for k, v in fields.items():
            if k.startswith('Return=') and k != 'Return=OK':
                assert v == 0, (group, k, v)
        if group in ('INSERT', 'READ', 'UPDATE'):
            assert fields['Operations'] == fields.get('Return=OK'), (group, fields)
    return out


def load_records(directory, r, total, start=0, n=None):
    mkdir(directory)
    args, props = ycsb_argv(directory, 'A', total, 32, 8, load=True, insertstart=start, insertcount=n)
    jsave(directory / 'command.json', {'argv': args, 'properties': props, 'at': now()})
    p = spawn(args, directory, directory / 'ycsb.log', '26-33', 1)
    t0 = time.monotonic()
    samples = []
    while p.poll() is None:
        try:
            samples.append({'at': now(), 'client': proc(p.pid), 'memory': r.info('memory'),
                            'cmdp': cmdp_stats()})
        except ProcessLookupError:
            pass
        if time.monotonic() - t0 > 3600:
            p.terminate()
            raise RuntimeError('YCSB load exceeded one hour')
        time.sleep(5)
    jsave(directory / 'load-observations.json', samples)
    assert p.returncode == 0
    result = export_parse(directory / 'ycsb.log')
    assert result['INSERT']['Operations'] == (total if n is None else n)
    jsave(directory / 'load-result.json', result)
    return result


def prepare():
    root = mkdir(ART / 'prepare')
    assert not (ART / 'golden/manifest.json').exists(), 'Golden dataset already exists'
    baseline = cmdp_stats()
    p, r, _ = start_redis(root, mkdir(root / 'data'))
    try:
        empty = r.info('memory')['used_memory']
        count = 1000000
        load_records(root / 'pilot-1m', r, count)
        info = validate_db(r, count)['info']
        estimated = int((16 * GIB - empty) / ((info['used_memory'] - empty) / count))
        estimated = int(round(estimated / 10000.0) * 10000)
        jsave(root / 'calibration.json', {'empty_used_memory': empty, 'pilot': info,
                                        'estimated_records': estimated})
        load_records(root / 'full-load', r, estimated, start=count, n=estimated - count)
        count = estimated
        validation = validate_db(r, count)
        jsave(root / 'loaded.json', {'recordcount': count, 'validation': validation})
        r.sock.settimeout(600)
        assert r.call('SAVE') == 'OK'
        r.sock.settimeout(10)
    finally:
        stop_redis(p, r)
    golden = mkdir(ART / 'golden')
    (root / 'data/dump.rdb').replace(golden / 'dump.rdb')
    check = command([SOURCE / 'redis/src/redis-check-rdb', golden / 'dump.rdb'])
    (root / 'rdb-check.log').write_text(check + '\n')
    p, r, restore_s = start_redis(mkdir(root / 'restore-check'), golden)
    try:
        restored = validate_db(r, count, validation['samples'])
        used = restored['info']['used_memory']
        assert 0.95 * 16 * GIB <= used <= 1.05 * 16 * GIB, 'Restored size outside target: %r' % used
        manifest = {'at': now(), 'recordcount': count, 'target_used_memory_bytes': 16 * GIB,
                    'restored_used_memory_bytes': used, 'restored_rss_bytes': restored['info']['used_memory_rss'],
                    'restore_seconds': restore_s, 'samples': validation['samples'],
                    'rdb_sha256': digest(golden / 'dump.rdb'), 'rdb_bytes': (golden / 'dump.rdb').stat().st_size,
                    'redis_commit': command(['git', '-C', SOURCE / 'redis', 'rev-parse', 'HEAD']),
                    'ycsb_commit': command(['git', '-C', YCSB, 'rev-parse', 'HEAD']),
                    'redis_binary_sha256': digest(REDIS), 'cmdp_before': baseline, 'cmdp_after': cmdp_stats(),
                    'loaded_info': validation['info'], 'restored_info': restored['info']}
        assert manifest['cmdp_before'] == manifest['cmdp_after']
        jsave(golden / 'manifest.json', manifest)
        os.chmod(str(golden / 'dump.rdb'), 0o444)
        os.chmod(str(golden), 0o555)
    finally:
        stop_redis(p, r)
    print(json.dumps({'prepared': True, 'recordcount': count, 'used_GiB': used / GIB, 'restore_s': restore_s}), flush=True)


STATUS_RE = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d:\d{3}) (\d+) sec: (\d+) operations;')


def statuses(path):
    rows = []
    for line in P(path).read_text(errors='replace').splitlines():
        m = STATUS_RE.search(line)
        if m:
            dt = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S:%f').replace(tzinfo=datetime.timezone.utc)
            rows.append({'epoch': dt.timestamp(), 'elapsed_s': int(m.group(2)), 'operations': int(m.group(3))})
    return rows


def receipt(directory, summary):
    jsave(directory / 'summary.json', summary)
    hashes = {str(p.relative_to(directory)): digest(p) for p in sorted(directory.rglob('*'))
              if p.is_file() and p.name != 'receipt.json'}
    jsave(directory / 'receipt.json', {'at': now(), 'passed': True, 'files_sha256': hashes})


def verify_receipt(directory):
    rec = json.loads((directory / 'receipt.json').read_text())
    assert rec['passed']
    for name, h in rec['files_sha256'].items():
        assert digest(directory / name) == h, name
    return json.loads((directory / 'summary.json').read_text())


def run_one(directory, workload, threads, cores, warm=30, duration=120, golden=None, enforce_size=True):
    directory = P(directory)
    if (directory / 'receipt.json').exists():
        return verify_receipt(directory)
    assert not directory.exists(), 'Incomplete run exists; preserve it and use a new run ID: %s' % directory
    mkdir(directory)
    golden = golden or ART / 'golden'
    manifest = json.loads((golden / 'manifest.json').read_text())
    expected_policy = policies()
    assert expected_policy['kernel'] == EXPECTED_KERNEL
    before_cmdp = cmdp_stats()
    p, r, restore_s = start_redis(directory, golden)
    client = None
    observations = []
    try:
        validation = validate_db(r, manifest['recordcount'], manifest['samples'])
        if enforce_size:
            assert abs(validation['info']['used_memory'] / (16 * GIB) - 1) <= 0.05
        (directory / 'redis-numa-before.txt').write_text(P('/proc/%d/numa_maps' % p.pid).read_text())
        (directory / 'redis-smaps-before.txt').write_text(P('/proc/%d/smaps_rollup' % p.pid).read_text())
        args, props = ycsb_argv(directory, workload, manifest['recordcount'], threads, cores,
                                seconds=warm + duration + 3)
        jsave(directory / 'command.json', {'argv': args, 'properties': props, 'client_cores': cpu_set(cores),
                                          'redis_cpu': 1, 'redis_node': 0, 'client_node': 1,
                                          'warmup_seconds': warm, 'measure_seconds': duration})
        before_info = r.info()
        client = spawn(args, directory, directory / 'ycsb.log', '26-%d' % (25 + cores), 1)
        jsave(directory / 'pids.json', {'redis': p.pid, 'ycsb': client.pid, 'at': now()})
        t0 = time.monotonic()
        deadline = t0
        with open(str(directory / 'observations.jsonl'), 'w') as stream:
            while client.poll() is None:
                try:
                    row = {'epoch': time.time(), 'monotonic': time.monotonic(), 'redis': proc(p.pid),
                           'client': proc(client.pid), 'redis_threads_checked': affinity(p.pid, [1]),
                           'client_threads_checked': affinity(client.pid, cpu_set(cores)),
                           'info': r.info('memory', 'stats', 'cpu', 'persistence'), 'cmdp': cmdp_stats(),
                           'redis_freq_khz': int(P('/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq').read_text())}
                    assert row['redis']['vm_swap_kib'] == row['client']['vm_swap_kib'] == 0
                    assert row['cmdp'] == before_cmdp, 'Unexpected CMD-P activity'
                    assert row['info']['evicted_keys'] == row['info']['expired_keys'] == 0
                    assert row['info']['rdb_bgsave_in_progress'] == row['info']['aof_enabled'] == 0
                    stream.write(json.dumps(row, sort_keys=True) + '\n')
                    stream.flush()
                    observations.append(row)
                except (FileNotFoundError, ProcessLookupError):
                    if client.poll() is not None:
                        break
                    raise
                if time.monotonic() - t0 > warm + duration + 120:
                    raise RuntimeError('YCSB exceeded expected runtime')
                deadline += 1
                time.sleep(max(0, deadline - time.monotonic()))
        assert client.returncode == 0, 'YCSB failed with code %s' % client.returncode
        exported = export_parse(directory / 'ycsb.log')
        rows = statuses(directory / 'ycsb.log')
        start = next(x for x in rows if x['elapsed_s'] >= warm)
        end = next(x for x in rows if x['elapsed_s'] >= start['elapsed_s'] + duration)
        elapsed = end['epoch'] - start['epoch']
        assert abs(elapsed - duration) <= 0.25, 'Irregular measurement window'
        ops = end['operations'] - start['operations']
        assert ops > 0
        window = [x for x in observations if start['epoch'] <= x['epoch'] <= end['epoch']]
        assert len(window) >= duration - 2, 'Missing observer samples'
        first, last = window[0], window[-1]
        span = last['monotonic'] - first['monotonic']
        redis_cpu = 100 * (last['redis']['cpu_s'] - first['redis']['cpu_s']) / span
        client_cpu = 100 * (last['client']['cpu_s'] - first['client']['cpu_s']) / span
        after_info = r.info()
        assert after_info['rdb_saves'] == before_info['rdb_saves'], 'Unexpected snapshot'
        assert policies() == expected_policy
        assert cmdp_stats() == before_cmdp
        assert r.call('DBSIZE') == manifest['recordcount'] + 1
        assert r.call('ZCARD', '_indices') == manifest['recordcount']
        (directory / 'redis-numa-after.txt').write_text(P('/proc/%d/numa_maps' % p.pid).read_text())
        (directory / 'redis-smaps-after.txt').write_text(P('/proc/%d/smaps_rollup' % p.pid).read_text())
        hdr = command([JAVA, '-cp', str(STUDY / 'tools') + ':' + ycsb_cp(), 'YcsbHdrWindow',
                       directory, str(round(start['epoch'] * 1000)), str(round(end['epoch'] * 1000))])
        latency = json.loads(hdr)
        assert all(v['intervals'] >= duration - 1 for v in latency.values())
        hist_ops = sum(v['count'] for v in latency.values())
        assert abs(hist_ops - ops) <= max(2 * threads, ops * 0.001), (hist_ops, ops)
        summary = {'at': now(), 'workload': workload, 'threads': threads, 'client_cores': cores,
                   'client_cpu_list': cpu_set(cores), 'recordcount': manifest['recordcount'],
                   'golden_rdb_sha256': manifest['rdb_sha256'], 'restore_seconds': restore_s,
                   'window_start': start, 'window_end': end, 'measured_seconds': elapsed,
                   'successful_operations': ops, 'qps': ops / elapsed, 'latency': latency,
                   'redis_cpu_pct': redis_cpu, 'client_cpu_pct': client_cpu,
                   'client_pool_util_pct': client_cpu / cores,
                   'redis_used_memory_mean': statistics.mean(x['info']['used_memory'] for x in window),
                   'redis_rss_peak': max(x['redis']['rss_bytes'] for x in window),
                   'client_rss_peak': max(x['client']['rss_bytes'] for x in window),
                   'redis_freq_khz_median': statistics.median(x['redis_freq_khz'] for x in window),
                   'policy': expected_policy, 'cmdp_before': before_cmdp, 'cmdp_after': cmdp_stats(),
                   'before_info': before_info, 'after_info': after_info, 'exported': exported,
                   'observation_count': len(observations), 'admitted': True}
        stop_redis(p, r)
        receipt(directory, summary)
        print(json.dumps({'run': directory.name, 'qps': summary['qps'], 'redis_cpu': redis_cpu,
                          'client_pool_util_pct': client_cpu / cores, 'restore_s': restore_s}), flush=True)
        return summary
    finally:
        if client is not None and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=15)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
        stop_redis(p, r)


def smoke():
    root = mkdir(ART / 'smoke')
    baseline = cmdp_stats()
    database = mkdir(root / 'golden')
    p, r, _ = start_redis(mkdir(root / 'load-server'), database)
    try:
        load_records(root / 'load', r, 10000)
        validation = validate_db(r, 10000)
        assert r.call('SAVE') == 'OK'
        samples = validation['samples']
        r.call('HSET', key(0), 'field0', b'x' * 100)
        assert sample_records(r, 10000) != samples
        jsave(database / 'manifest.json', {'recordcount': 10000, 'samples': samples,
                                          'rdb_sha256': digest(database / 'dump.rdb')})
    finally:
        stop_redis(p, r)
    reports = []
    for w in 'ABC':
        reports.append(run_one(root / ('run-' + w), w, 4, 8, warm=3, duration=10, golden=database,
                               enforce_size=False))
    assert baseline == cmdp_stats()
    jsave(root / 'smoke-result.json', {'passed': True, 'at': now(), 'reports': reports,
                                     'restored_updates_discarded': True})


def aggregate(results):
    groups = {}
    for r in results:
        groups.setdefault((r['workload'], r['client_cores'], r['threads']), []).append(r)
    output = []
    for (w, cores, threads), values in sorted(groups.items()):
        output.append({'workload': w, 'client_cores': cores, 'threads': threads, 'n': len(values),
                       'qps_median': statistics.median(x['qps'] for x in values),
                       'qps_min': min(x['qps'] for x in values), 'qps_max': max(x['qps'] for x in values),
                       'redis_cpu_median': statistics.median(x['redis_cpu_pct'] for x in values),
                       'client_pool_util_median': statistics.median(x['client_pool_util_pct'] for x in values)})
    return output


def plateau(groups):
    groups = sorted(groups, key=lambda x: x['threads'])
    best, gains = 0, []
    for g in groups:
        q = g['qps_median']
        gains.append(max(0, q / best - 1) if best else 1)
        best = max(best, q)
    return len(gains) >= 3 and all(x < 0.03 for x in gains[-2:])


def campaign():
    root = mkdir(ART / 'baseline-v1')
    script_hash = digest(P(__file__))
    manifest = json.loads((ART / 'golden/manifest.json').read_text())
    assert digest(ART / 'golden/dump.rdb') == manifest['rdb_sha256']
    assert json.loads((ART / 'smoke/smoke-result.json').read_text())['passed']
    software = json.loads((ART / 'software-manifest.json').read_text())
    for name, expected in software['files_sha256'].items():
        assert digest(STUDY / name) == expected, 'Software changed: ' + name
    fixed = {'script_sha256': script_hash, 'rdb_sha256': manifest['rdb_sha256'],
             'redis_binary_sha256': digest(REDIS), 'kernel_policy': policies(),
             'software_manifest_sha256': digest(ART / 'software-manifest.json'),
             'client_cores_initial': 8, 'warmup_seconds': 30, 'measure_seconds': 120, 'repetitions': 3,
             'concurrency': [1, 2, 4, 8, 16, 32, 64], 'max_concurrency': 256}
    frozen = root / 'campaign-config.json'
    if frozen.exists():
        assert json.loads(frozen.read_text()) == fixed, 'Campaign inputs changed'
    else:
        jsave(frozen, fixed)
        (root / 'dmesg-before.txt').write_text(command(['dmesg', '--color=never']))
    results = []

    def one(w, t, c, rep):
        assert digest(P(__file__)) == script_hash, 'Runner changed during campaign'
        name = '%s-c%02d-t%03d-r%d' % (w, c, t, rep + 1)
        jsave(root / 'campaign-state.json', {'status': 'running', 'active': name, 'completed': len(results), 'at': now()})
        r = run_one(root / 'runs' / name, w, t, c)
        results.append(r)
        jsave(root / 'aggregate.json', aggregate(results))

    concurrency = fixed['concurrency']
    for rep in range(3):
        rotate = (rep * 2) % len(concurrency)
        for t in concurrency[rotate:] + concurrency[:rotate]:
            for w in ('ABC'[rep:] + 'ABC'[:rep]):
                one(w, t, 8, rep)
    for w in 'ABC':
        for t in (128, 256):
            g = [x for x in aggregate(results) if x['workload'] == w and x['client_cores'] == 8]
            if plateau(g):
                break
            for rep in range(3):
                one(w, t, 8, rep)
    for w in 'ABC':
        for cores in (16, 24):
            g = [x for x in aggregate(results) if x['workload'] == w]
            best = max(g, key=lambda x: x['qps_median'])
            if not (best['redis_cpu_median'] < 90 and best['client_pool_util_median'] > 80):
                break
            ts = sorted(set([max(1, best['threads'] // 2), best['threads'], min(256, best['threads'] * 2)]))
            for rep in range(3):
                for t in ts:
                    one(w, t, cores, rep)
    assert policies() == fixed['kernel_policy']
    assert digest(ART / 'golden/dump.rdb') == manifest['rdb_sha256']
    (root / 'dmesg-after.txt').write_text(command(['dmesg', '--color=never']))
    jsave(root / 'campaign-state.json', {'status': 'complete', 'completed': len(results), 'at': now(),
                                       'aggregate': aggregate(results), 'cmdp_final': cmdp_stats()})
    print('CAMPAIGN COMPLETE: %d valid runs' % len(results), flush=True)


def restore():
    """Leave a verified, unmonitored Redis instance running for later use."""
    golden = ART / 'golden'
    manifest = json.loads((golden / 'manifest.json').read_text())
    assert digest(golden / 'dump.rdb') == manifest['rdb_sha256']
    directory = mkdir(ART / ('manual-restore-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')))
    p, r, elapsed = start_redis(directory, golden)
    try:
        validation = validate_db(r, manifest['recordcount'], manifest['samples'])
        info = {'pid': p.pid, 'boot_id': policies()['boot_id'], 'directory': str(directory),
                'at': now(), 'restore_seconds': elapsed, 'recordcount': manifest['recordcount'],
                'used_memory': validation['info']['used_memory'], 'rdb_sha256': manifest['rdb_sha256']}
        jsave(ART / 'manual-server.json', info)
        jsave(directory / 'restored.json', info)
        print(json.dumps(info), flush=True)
    except BaseException:
        stop_redis(p, r)
        raise
    finally:
        r.close()


def stop_manual():
    state = json.loads((ART / 'manual-server.json').read_text())
    pid = state['pid']
    assert state['boot_id'] == policies()['boot_id']
    assert P('/proc/%d/exe' % pid).resolve() == REDIS.resolve()
    r = Redis()
    try:
        assert r.info('server')['process_id'] == pid
        r.call('SHUTDOWN', 'NOSAVE')
    except EOFError:
        pass
    finally:
        r.close()
    for _ in range(100):
        if not P('/proc/%d' % pid).exists():
            jsave(ART / 'manual-server.json', dict(state, stopped_at=now()))
            print('Manual Redis stopped without saving')
            return
        time.sleep(0.1)
    raise RuntimeError('Manual Redis did not finish shutdown')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['smoke', 'prepare', 'campaign', 'restore', 'stop'])
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Run as root to observe kernel counters and set workload identities'
    os.sched_setaffinity(0, {24})
    mkdir(ART)
    mem = dict((k.rstrip(':'), int(v.split()[0])) for k, v in (line.split(None, 1) for line in P('/proc/meminfo').read_text().splitlines()))
    assert mem['MemAvailable'] > 48 * 1024 * 1024, 'Insufficient host headroom'
    jsave(ART / ('last-' + args.action + '-invocation.json'), {'at': now(), 'pid': os.getpid(), 'script_sha256': digest(P(__file__))})
    {'smoke': smoke, 'prepare': prepare, 'campaign': campaign, 'restore': restore, 'stop': stop_manual}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except BaseException as e:
        traceback.print_exc()
        sys.exit(1)
