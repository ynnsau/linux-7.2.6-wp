#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Small-process checks; no root, global THP, or CMD-P attachment required."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def run(argv, env=None):
    return subprocess.run([str(x) for x in argv], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=30)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


PROBE = r'''
import ctypes, json, os, sys
libc = ctypes.CDLL(None, use_errno=True)
policy = libc.prctl(42, 0, 0, 0, 0)
status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
child = os.fork()
if child == 0:
    os._exit(0 if libc.prctl(42, 0, 0, 0, 0) == 1 else 1)
_, child_status = os.waitpid(child, 0)
print(json.dumps({'policy': policy, 'thp_enabled': status.get('THP_enabled', '').strip(),
                  'args': sys.argv[1:], 'env': os.environ['CMDP_EXEC_TEST_VALUE'],
                  'pid': os.getpid(), 'fork_status': child_status,
                  'cpus': sorted(os.sched_getaffinity(0))}))
'''

WRAP = r'''
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
int __real_prctl(int, ...);
int __wrap_prctl(int option, ...)
{
    const char *mode = getenv("CMDP_EXEC_TEST_FAIL");
    unsigned long a, b, c, d;
    va_list args;
    va_start(args, option);
    a = va_arg(args, unsigned long); b = va_arg(args, unsigned long);
    c = va_arg(args, unsigned long); d = va_arg(args, unsigned long);
    va_end(args);
    if (mode && ((!strcmp(mode, "set") && option == PR_SET_THP_DISABLE) ||
                 (!strcmp(mode, "get") && option == PR_GET_THP_DISABLE))) {
        errno = EPERM;
        return -1;
    }
    if (mode && option == PR_GET_THP_DISABLE) {
        if (!strcmp(mode, "zero")) return 0;
        if (!strcmp(mode, "except-advised")) return 3;
    }
    return __real_prctl(option, a, b, c, d);
}
'''


def main():
    default_source = Path(__file__).resolve().parent.parent / 'cmdp_exec.c'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default=str(default_source.with_suffix('')))
    parser.add_argument('--source', default=str(default_source))
    args = parser.parse_args()
    binary = Path(args.binary).resolve()
    source = Path(args.source).resolve()
    env = os.environ.copy()
    env['CMDP_EXEC_TEST_VALUE'] = 'environment preserved: spaces, $ and quotes'
    forwarded = ['', '--option', 'with spaces', 'quote\'"', 'dollar$backtick`', 'unicode-\u03bb']
    process = subprocess.Popen([str(binary), '--', sys.executable, '-c', PROBE] + forwarded,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
    stdout, stderr = process.communicate(timeout=30)
    require(process.returncode == 0, 'inheritance probe failed: ' + stderr)
    probe = json.loads(stdout)
    require(probe['policy'] == 1 and probe['thp_enabled'] == '0', 'THP disable not inherited through exec')
    require(probe['fork_status'] == 0, 'THP disable not inherited through fork')
    require(probe['args'] == forwarded, 'argv changed')
    require(probe['env'] == env['CMDP_EXEC_TEST_VALUE'], 'environment changed')
    require(probe['pid'] == process.pid, 'launcher did not directly exec')
    require(probe['cpus'] == sorted(os.sched_getaffinity(0)), 'affinity changed')

    for bad in ([], ['--'], ['/bin/true'], ['--', '']):
        result = run([binary] + bad)
        require(result.returncode == 2 and not result.stdout, 'invalid invocation accepted')
    result = run([binary, '--', '/definitely/nonexistent/cmdp-test'])
    require(result.returncode == 127, 'missing command exit code')
    require(run([binary, '--', '/bin/sh', '-c', 'exit 19']).returncode == 19,
            'target exit status changed')

    with tempfile.TemporaryDirectory(prefix='cmdp-exec-test-') as temporary:
        directory = Path(temporary)
        blocked = directory / 'not-executable'
        blocked.write_text('not executable\n')
        require(run([binary, '--', blocked]).returncode == 126, 'non-executable exit code')
        wrap = directory / 'wrap.c'
        wrap.write_text(WRAP)
        injected = directory / 'cmdp_exec_injected'
        built = run(shlex.split(os.environ.get('CC', 'cc')) +
                    ['-O2', '-Wall', '-Wextra', '-Werror', str(source), str(wrap),
                     '-Wl,--wrap=prctl', '-o', str(injected)])
        require(built.returncode == 0, 'fault-injection build failed: ' + built.stderr)
        for mode in ('set', 'get', 'zero', 'except-advised'):
            sentinel = directory / ('executed-' + mode)
            injected_env = env.copy()
            injected_env['CMDP_EXEC_TEST_FAIL'] = mode
            result = run([injected, '--', '/usr/bin/touch', sentinel], env=injected_env)
            require(result.returncode == 125, mode + ': incorrect failure status')
            require(not sentinel.exists(), mode + ': target ran despite failed THP policy')
            require(not result.stdout, mode + ': unexpected stdout')
    print('PASS: exec/fork inheritance, argv/env/PID/affinity, exit status, and four fail-closed cases')


if __name__ == '__main__':
    main()
