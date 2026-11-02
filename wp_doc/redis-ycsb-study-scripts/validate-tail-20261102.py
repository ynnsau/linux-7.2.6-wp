import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

repo = pathlib.Path('/home/yans3/cmdp-v2/linux-fused-scanner')
scripts = repo / 'tools/testing/selftests/cmdp/scripts'
out = pathlib.Path('/home/yans3/cmdp-v2/redis-ycsb-study/tail-build-validation-20261102')
out.mkdir(exist_ok=True)
names = ['test-redis-ycsb-fixedfreq.py', 'test-redis-ycsb-eligibility.py',
         'test-redis-ycsb-eligibility-integration.py', 'test-redis-ycsb-verify.py',
         'test-redis-ycsb-readiness.py', 'test-redis-ycsb-tail-campaign.py',
         'test-redis-ycsb-fixedfreq-report.py', 'test-redis-ycsb-raw.py']
def test(name):
    command = [sys.executable, str(scripts / name)]
    if name == 'test-redis-ycsb-readiness.py':
        command += ['--trace-directory', '/home/yans3/cmdp-v2/redis-ycsb-study/eligibility-v2/smoke-v1/B-arm-v1']
    with (out / (name + '.log')).open('w') as f:
        code = subprocess.call(command, stdout=f, stderr=subprocess.STDOUT, cwd=str(scripts))
    return dict(test=name, exit_code=code, command=command)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(test, names))
for command in [['git', '-C', str(repo), 'diff', '--check'],
                ['sh', '-n', str(scripts / 'redis-ycsb-tail-service.sh')]]:
    results.append(dict(command=command, exit_code=subprocess.call(command)))
hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in scripts.glob('*')
          if p.is_file() and p.suffix in ('.py', '.sh', '.md')}
summary = dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               passed=all(r['exit_code'] == 0 for r in results), results=results, source_sha256=hashes)
(out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps(dict(passed=summary['passed'], results=results), indent=2))
sys.exit(0 if summary['passed'] else 1)
