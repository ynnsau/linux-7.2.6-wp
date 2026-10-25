// SPDX-License-Identifier: GPL-2.0
/* CPU hotplug is permitted ONLY in the disposable cmdp_suite=cpu guest. */
#define BASE UINT64_C(0x1000000000)
#define main cmdp_batch_original_main
#include "cmdp_batch_test.c"
#undef main
#include <limits.h>

static struct cmdp_monitor_status mon_status(void)
{
    struct cmdp_monitor_status s; INIT(s);
    CHECK(!ioctl(fd, CMDP_MON_STATUS, &s), "read monitor state");
    return s;
}
static struct cmdp_monitor_cpu_status cpu_status(void)
{
    struct cmdp_monitor_cpu_status s; INIT(s);
    CHECK(!ioctl(fd, CMDP_MON_CPU_STATUS, &s), "read scanner CPU diagnostics");
    return s;
}
static int set_cpu(int cpu)
{
    struct cmdp_monitor_cpu_config c; INIT(c); c.cpu = cpu;
    return ioctl(fd, CMDP_MON_CPU_CONFIG, &c);
}
static bool online(unsigned int cpu, bool value)
{
    char path[96];
    snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%u/online", cpu);
    int f = open(path, O_WRONLY);
    if (f < 0) return false;
    bool ok = write(f, value ? "1\n" : "0\n", 2) == 2;
    close(f); return ok;
}
static bool wait_passes(unsigned int n)
{
    uint64_t until = now_ns() + UINT64_C(10000000000);
    do {
        struct cmdp_monitor_status s; INIT(s);
        if (ioctl(fd, CMDP_MON_STATUS, &s) || s.state >= CMDP_MON_STOPPING) return false;
        if (s.passes_completed >= n) return true;
        usleep(1000);
    } while (now_ns() < until);
    return false;
}
static bool wait_armed(void)
{
    uint64_t until = now_ns() + UINT64_C(10000000000);
    do {
        if (all_state(0, pages, CMDP_MON_PAGE_ARMED)) return true;
        usleep(1000);
    } while (now_ns() < until);
    return false;
}
static bool wait_stopped(void)
{
    uint64_t until = now_ns() + UINT64_C(10000000000);
    do {
        struct cmdp_monitor_status s; INIT(s);
        if (ioctl(fd, CMDP_MON_STATUS, &s)) return false;
        if (s.state == CMDP_MON_STOPPED) return !s.active && balanced();
        usleep(1000);
    } while (now_ns() < until);
    return false;
}
static void cpu_finish(void)
{
    if (fd >= 0) {
        CHECK(!ioctl(fd, CMDP_MON_STOP), "explicit STOP joins all scanner work");
        close(fd); fd = -1;
    }
    CHECK(balanced() && command(1) && command(2), "target still writes correct data after ownership drain");
    atomic_store(&ctl->quit, 1);
    int st;
    CHECK(waitpid(target_pid, &st, 0) == target_pid && WIFEXITED(st) && !WEXITSTATUS(st),
          "target exits normally after monitor termination");
    target_pid = -1; close(pidfd); pidfd = -1; munmap(ctl, PAGE);
}
static void validation(void)
{
    fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
    CHECK(fd >= 0 && set_cpu(1) == -1 && errno == EBUSY, "placement requires attachment");
    close(fd); fd = -1;
    start(129, 1, CMDP_MON_DIRTY_OBSERVE, 32768);
    struct cmdp_monitor_cpu_status s = cpu_status();
    CHECK(s.cpu == CMDP_MON_CPU_AUTO && s.last_cpu == -1 && !s.work_items && !s.error,
          "legacy default is AUTO with no observed worker");
    struct cmdp_monitor_cpu_config c;
    INIT(c); c.cpu = 1; c.size--;
    CHECK(ioctl(fd, CMDP_MON_CPU_CONFIG, &c) == -1 && errno == EINVAL, "reject bad configuration size");
    INIT(c); c.cpu = 1; c.version++;
    CHECK(ioctl(fd, CMDP_MON_CPU_CONFIG, &c) == -1 && errno == EINVAL, "reject bad configuration version");
    INIT(c); c.cpu = 1; c.reserved = 1;
    CHECK(ioctl(fd, CMDP_MON_CPU_CONFIG, &c) == -1 && errno == EINVAL, "reject nonzero reserved fields");
    CHECK(set_cpu(-2) == -1 && errno == EINVAL, "reject invalid negative CPU");
    CHECK(set_cpu(INT_MAX) == -1 && errno == EINVAL, "reject impossible CPU before mask lookup");
    CHECK(online(2, false), "offline spare guest CPU for admission tests");
    CHECK(set_cpu(2) == -1 && errno == ENODEV, "reject offline CPU configuration");
    CHECK(online(2, true) && !set_cpu(2), "accept restored online CPU");
    CHECK(online(2, false), "offline configured CPU before START");
    CHECK(ioctl(fd, CMDP_MON_START) == -1 && errno == ENODEV, "START rechecks CPU availability");
    CHECK(online(2, true) && !set_cpu(1), "reconfigure before any scan");
    CHECK(ioctl(fd, CMDP_MON_SCAN_ONCE) == -1 && errno == EOPNOTSUPP,
          "explicit CPU never silently falls back to a caller-context scan");
    CHECK(!set_cpu(CMDP_MON_CPU_AUTO) && scan(), "AUTO retains synchronous SCAN_ONCE");
    CHECK(set_cpu(1) == -1 && errno == EBUSY, "configuration is immutable after the first synchronous scan");
    cpu_finish();
}
static void placement(unsigned int cpu, unsigned int t, unsigned int mode)
{
    printf("CMDP_CPU_CASE cpu=%u threads=%u mode=%u kind=placement\n", cpu, t, mode);
    start(513, t, mode, 32768);
    CHECK(!set_cpu(cpu) && !ioctl(fd, CMDP_MON_START), "start periodic work on explicit CPU");
    CHECK(set_cpu(CMDP_MON_CPU_AUTO) == -1 && errno == EBUSY, "reject live reconfiguration");
    CHECK(wait_passes(8), "periodic requeue completes eight scans including idle gaps");
    struct cmdp_monitor_cpu_status s = cpu_status();
    CHECK(s.cpu == (int)cpu && s.last_cpu == (int)cpu && s.work_items >= 8 &&
          !s.wrong_cpu_work_items && !s.error && !s.offline_events,
          "every observed scanner entry honors the configured CPU");
    if (mode == CMDP_MON_DIRTY_QUALIFY) {
        CHECK(wait_armed(), "cold pages ARM under explicit CPU placement");
        struct cmdp_monitor_page before, after;
        CHECK(query(0, &before) && command(1) && command(2), "protected writes complete with correct data");
        CHECK(wait_armed() && query(0, &after) && before.arm_generation != after.arm_generation,
              "written pages re-ARM after a fresh quiet interval");
        CHECK(command(4) && command(5) && command(2) && wait_armed(),
              "mprotect and remap preserve recovery under bound periodic scanning");
    } else {
        struct cmdp_monitor_status st = mon_status();
        CHECK(!st.active && !st.first_arms && !st.rearms, "observe-only mode never ARM-protects pages");
    }
    CHECK(!ioctl(fd, CMDP_MON_STOP), "STOP cancels pending requeue");
    s = cpu_status(); usleep(150000);
    struct cmdp_monitor_cpu_status end = cpu_status();
    CHECK(s.work_items == end.work_items && balanced(), "no scanner work restarts after STOP");
    cpu_finish();
}
static void offline_case(unsigned int phase)
{
    printf("CMDP_CPU_CASE cpu=1 threads=1 kind=offline phase=%u\n", phase);
    start(129, 1, CMDP_MON_DIRTY_QUALIFY, 32768);
    CHECK(!set_cpu(1), "bind scanner before hotplug fixture");
    if (phase) injection(phase, 0);
    CHECK(!ioctl(fd, CMDP_MON_START), "start monitor for CPU removal");
    if (phase)
        CHECK(wait_phase(phase), "CPU removal overlaps actual protected/reset-before-flush window");
    else
        CHECK(wait_armed(), "live ARM ownership exists before CPU removal");
    CHECK(online(1, false), "offline scanner CPU without synchronously joining from hotplug callback");
    CHECK(wait_stopped(), "offline callback completes asynchronous STOP and drains all ownership");
    struct cmdp_monitor_status st = mon_status();
    struct cmdp_monitor_cpu_status s = cpu_status();
    CHECK(st.stop_reason == CMDP_MON_ERROR && s.error == -ENODEV && s.offline_events == 1 &&
          !s.wrong_cpu_work_items, "CPU removal is explicit failure, never silent placement fallback");
    CHECK(command(1) && command(2), "CPU removal leaves target alive and writable");
    CHECK(online(1, true), "restore guest CPU after offline test");
    usleep(150000);
    struct cmdp_monitor_cpu_status after = cpu_status();
    CHECK(after.work_items == s.work_items && mon_status().state == CMDP_MON_STOPPED,
          "bringing CPU back does not restart a terminated session");
    cpu_finish();
}
static atomic_int offline_result;
static void *offline_thread(void *unused)
{
    (void)unused;
    atomic_store(&offline_result, online(1, false) ? 0 : -1);
    return NULL;
}
static void stop_race(bool release)
{
    printf("CMDP_CPU_CASE cpu=1 kind=%s\n", release ? "close-offline-race" : "stop-offline-race");
    start(129, 1, CMDP_MON_DIRTY_QUALIFY, 32768);
    CHECK(!set_cpu(1), "configure CPU for teardown race");
    injection(CMDP_MON_TEST_BATCH_ARM, 0);
    CHECK(!ioctl(fd, CMDP_MON_START) && wait_phase(CMDP_MON_TEST_BATCH_ARM),
          "teardown races a real in-flight ARM batch");
    pthread_t th;
    CHECK(!pthread_create(&th, NULL, offline_thread, NULL), "start guest CPU offline concurrently");
    if (release) {
        CHECK(!close(fd), "close joins scanner and deferred cleanup despite concurrent hotplug");
        fd = -1;
    } else {
        CHECK(!ioctl(fd, CMDP_MON_STOP), "explicit STOP joins concurrent CPU-offline cancellation");
    }
    CHECK(!pthread_join(th, NULL) && !atomic_load(&offline_result) && balanced(),
          "hotplug and teardown both finish without leaked ownership");
    CHECK(online(1, true), "restore guest CPU after teardown race");
    cpu_finish();
}
static void exit_case(void)
{
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 32768);
    CHECK(!set_cpu(2) && !ioctl(fd, CMDP_MON_START) && wait_armed(), "bind four-thread fixture before target exit");
    atomic_store(&ctl->quit, 1);
    int st;
    CHECK(waitpid(target_pid, &st, 0) == target_pid && WIFEXITED(st) && !WEXITSTATUS(st),
          "target exits with ARM ownership active");
    target_pid = -1;
    CHECK(!ioctl(fd, CMDP_MON_STOP) && balanced(), "target exit plus STOP safely joins bound work");
    close(fd); fd = -1; close(pidfd); pidfd = -1; munmap(ctl, PAGE);
}
int main(void)
{
    char cmdline[4096] = {0};
    FILE *f = fopen("/proc/cmdline", "r");
    if (!f || !fgets(cmdline, sizeof(cmdline), f)) return 4;
    fclose(f);
    if (geteuid() || !strstr(cmdline, "cmdp_suite=cpu") || sysconf(_SC_NPROCESSORS_ONLN) != 4)
        return 4;
    puts("TAP version 13"); alarm(180);
    pin(0);
    validation();
    for (unsigned int t = 1; t <= 4; t += 3)
        for (unsigned int cpu = 1; cpu <= 2; cpu++)
            for (unsigned int mode = CMDP_MON_DIRTY_OBSERVE; mode <= CMDP_MON_DIRTY_QUALIFY; mode++)
                placement(cpu, t, mode);
    offline_case(0);
    offline_case(CMDP_MON_TEST_BATCH_DIRTY);
    offline_case(CMDP_MON_TEST_BATCH_ARM);
    for (unsigned int i = 0; i < 3; i++) {
        stop_race(false); stop_race(true);
    }
    exit_case();
    CHECK(balanced(), "final CPU-test ownership and paused-batch gauges are zero");
    printf("1..%d\n", checks); return 0;
}
