#!/bin/bash
set -euo pipefail
study=/home/yans3/cmdp-v2/redis-ycsb-study
export LC_ALL=C
mkdir -p "$study/src" "$study/tools" "$study/build-logs" "$study/artifacts"
cmdp_tools=$(cd "$(dirname "$0")" && pwd)
if [ "$cmdp_tools" != "$study/tools" ]; then
  cp "$cmdp_tools/redis-ycsb-study.py" "$cmdp_tools/YcsbHdrWindow.java" "$cmdp_tools/TestYcsbHdrWindow.java" "$study/tools/"
fi
exec >"$study/build-logs/setup.log" 2>&1
trap 'rc=$?; printf "%s\n" "$rc" > "$study/build-logs/setup.exit"' EXIT
cd "$study/src"
if [ ! -d redis/.git ]; then git clone --depth 1 --branch 8.10.3 https://github.com/redis/redis.git redis; fi
if [ ! -d YCSB/.git ]; then
  git init YCSB
  git -C YCSB remote add origin https://github.com/brianfrankcooper/YCSB.git
  git -C YCSB fetch --depth 1 origin ae02b31fa7784b3892f121c06638aa0cd972fa06
  git -C YCSB checkout --detach FETCH_HEAD
fi
test "$(git -C redis rev-parse HEAD)" = 1a2574ce1551d3de6e0b4a56756c0fdbc6659e4f
test "$(git -C YCSB rev-parse HEAD)" = ae02b31fa7784b3892f121c06638aa0cd972fa06
git -C redis diff --exit-code
git -C YCSB diff --exit-code
git -C redis rev-parse HEAD
git -C YCSB rev-parse HEAD
if [ ! -x "$study/tools/apache-maven-3.10.0/bin/mvn" ]; then
  cd "$study/tools"
  curl --fail --location --retry 3 -o apache-maven-3.10.0-bin.tar.gz https://archive.apache.org/dist/maven/maven-3/3.10.0/binaries/apache-maven-3.10.0-bin.tar.gz
  curl --fail --location --retry 3 -o apache-maven-3.10.0-bin.tar.gz.sha512 https://archive.apache.org/dist/maven/maven-3/3.10.0/binaries/apache-maven-3.10.0-bin.tar.gz.sha512
  python3 - <<'PY'
import hashlib,pathlib
p=pathlib.Path('apache-maven-3.10.0-bin.tar.gz')
s=pathlib.Path(str(p)+'.sha512').read_text().split()[0]
assert hashlib.sha512(p.read_bytes()).hexdigest()==s
print('Maven archive SHA512 verified')
PY
  tar -xzf apache-maven-3.10.0-bin.tar.gz
fi
cd "$study/src/redis"
make -j8 build redis > "$study/build-logs/redis-build.log" 2>&1
src/redis-server --version
cd "$study/src/YCSB"
# Current Maven defaults to Surefire 3.6, which rejects upstream TestNG 6.1.1.
# Compile/install the reactor artifacts first, then execute the unchanged tests with a compatible runner.
"$study/tools/apache-maven-3.10.0/bin/mvn" -B -Psource-run -pl site.ycsb:redis-binding -am clean install -DskipTests > "$study/build-logs/ycsb-build.log" 2>&1
"$study/tools/apache-maven-3.10.0/bin/mvn" -B -Psource-run -pl site.ycsb:redis-binding -am org.apache.maven.plugins:maven-surefire-plugin:2.22.2:test > "$study/build-logs/ycsb-tests.log" 2>&1
javac -cp "$study/src/YCSB/core/target/dependency/HdrHistogram-2.1.12.jar" -d "$study/tools" "$study/tools/YcsbHdrWindow.java" "$study/tools/TestYcsbHdrWindow.java"
python3 - <<'PYMETA'
from pathlib import Path
import hashlib,json,subprocess,datetime
p=Path('/home/yans3/cmdp-v2/redis-ycsb-study')
cp=str(p/'tools')+':'+str(p/'src/YCSB/core/target/dependency/HdrHistogram-2.1.12.jar')
d=p/'artifacts/hdr-test';d.mkdir(exist_ok=True)
out=subprocess.check_output(['java','-cp',cp,'TestYcsbHdrWindow',str(d)],universal_newlines=True)
x=json.loads(out)['READ']
assert x['count']==30 and x['intervals']==3 and x['p99_us']==10
(d/'result.json').write_text(out)
files=[p/'src/redis/src/redis-server',p/'src/redis/src/redis-cli',p/'tools/YcsbHdrWindow.class']
for sub in ['core/target','core/target/dependency','redis/target','redis/target/dependency']:
    files+=list((p/'src/YCSB'/sub).glob('*.jar'))
manifest={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files_sha256':{str(q.relative_to(p)):hashlib.sha256(q.read_bytes()).hexdigest() for q in files},'versions':{}}
for name,args in {'redis':['src/redis/src/redis-server','--version'],'java':['java','-version'],'javac':['javac','-version'],'maven':['tools/apache-maven-3.10.0/bin/mvn','-version'],'redis-source':['git','-C','src/redis','status','--porcelain'],'ycsb-source':['git','-C','src/YCSB','status','--porcelain']}.items():
    manifest['versions'][name]=subprocess.check_output(args,cwd=str(p),stderr=subprocess.STDOUT,universal_newlines=True)
(p/'artifacts/software-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
PYMETA
printf 'Setup finished %s\n' "$(date -u +%FT%TZ)"
