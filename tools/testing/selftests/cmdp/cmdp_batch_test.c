// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <linux/cmdp.h>
#ifndef SYS_pidfd_open
#define SYS_pidfd_open 434
#endif
#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif
#define PAGE 4096UL
#define BASE UINT64_C(0x100000)
#define Q_MS 100
#define INIT(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
    (x).version = CMDP_MONITOR_VERSION; } while (0)
struct control {
    atomic_uint ready, command, ack, hot, quit, errors;
    atomic_ullong iterations[4];
};
struct worker_arg { unsigned int id; };
static struct control *ctl;
static unsigned char *area;
static unsigned int pages, threads;
static int checks, fd = -1, pidfd = -1;
static pid_t target_pid = -1;
static uint64_t now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void cleanup(void)
{
    if (fd >= 0) { ioctl(fd, CMDP_MON_STOP); close(fd); fd = -1; }
    if (target_pid > 0) { kill(target_pid, SIGKILL); waitpid(target_pid, NULL, 0); target_pid = -1; }
    if (pidfd >= 0) { close(pidfd); pidfd = -1; }
}
static void check(bool ok, const char *description)
{
    printf("%s %d - %s\n", ok ? "ok" : "not ok", ++checks, description);
    fflush(stdout);
    if (!ok) { cleanup(); printf("1..%d\n", checks); exit(1); }
}
#define CHECK(x, s) check((x), (s))
static uint64_t marker(unsigned int i) { return UINT64_C(0x735a9210ef000000) ^ i; }
static void store(unsigned int i)
{
    *(volatile uint64_t *)(area + i * PAGE) = marker(i);
    *(volatile uint64_t *)(area + (i + 1) * PAGE - 8) = ~marker(i);
}
static bool verify(void)
{
    for (unsigned int i = 0; i < pages; i++)
        if (*(volatile uint64_t *)(area + i * PAGE) != marker(i) ||
            *(volatile uint64_t *)(area + (i + 1) * PAGE - 8) != ~marker(i)) return false;
    return true;
}
static void write_slice(unsigned int id)
{
    for (unsigned int i = id; i < pages; i += threads) store(i);
    atomic_fetch_add(&ctl->iterations[id], 1);
}
static void pin(unsigned int id)
{
    cpu_set_t mask;
    CPU_ZERO(&mask); CPU_SET(id, &mask);
    if (sched_setaffinity(0, sizeof(mask), &mask)) _exit(10);
}
static void *worker(void *opaque)
{
    unsigned int id = ((struct worker_arg *)opaque)->id;
    pin(id);
    atomic_fetch_add(&ctl->ready, 1);
    while (!atomic_load(&ctl->quit)) {
        if (atomic_load(&ctl->hot)) write_slice(id);
        else usleep(100);
    }
    return NULL;
}
static void child(void)
{
    pin(0);
    if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)) _exit(11);
    area = mmap((void *)(uintptr_t)BASE, pages * PAGE, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (area == MAP_FAILED) _exit(12);
    for (unsigned int i = 0; i < pages; i++) store(i);
    pthread_t th[3]; struct worker_arg args[3];
    for (unsigned int i = 1; i < threads; i++) {
        args[i - 1].id = i;
        if (pthread_create(&th[i - 1], NULL, worker, &args[i - 1])) _exit(13);
    }
    atomic_fetch_add(&ctl->ready, 1);
    while (!atomic_load(&ctl->quit)) {
        unsigned int op = atomic_load(&ctl->command);
        if (op != atomic_load(&ctl->ack)) {
            switch (op % 4) {
            case 1: for (unsigned int i = 0; i < pages; i++) store(i); break;
            case 2: if (!verify()) atomic_fetch_add(&ctl->errors, 1); break;
            case 3: break;
            default: _exit(14);
            }
            atomic_store(&ctl->ack, op);
        }
        if (atomic_load(&ctl->hot)) write_slice(0);
        else usleep(100);
    }
    for (unsigned int i = 1; i < threads; i++) pthread_join(th[i - 1], NULL);
    _exit(verify() ? 0 : 15);
}
static bool wait_value(atomic_uint *value, unsigned int target)
{
    uint64_t until = now_ns() + UINT64_C(5000000000);
    while (now_ns() < until) {
        if (atomic_load(value) == target) return true;
        usleep(100);
    }
    return false;
}
static bool command(unsigned int op)
{
    unsigned int next = (atomic_load(&ctl->command) / 4 + 1) * 4 + op;
    atomic_store(&ctl->command, next);
    return wait_value(&ctl->ack, next) && !atomic_load(&ctl->errors);
}
static bool stat_value(const char *key, uint64_t *value)
{
    FILE *f = fopen("/sys/kernel/debug/cmdp/stats", "r");
    char name[80]; unsigned long long n; bool found = false;
    if (!f) return false;
    while (fscanf(f, "%79s %llu", name, &n) == 2)
        if (!strcmp(name, key)) { *value = n; found = true; break; }
    fclose(f); return found;
}
static uint64_t stat_get(const char *key)
{
    uint64_t v = 0;
    if (!stat_value(key, &v)) { fprintf(stderr, "Missing stat %s\n", key); cleanup(); exit(1); }
    return v;
}
static bool balanced(void)
{
    return !stat_get("entries") && !stat_get("managed") && !stat_get("isolated") &&
           !stat_get("test_batch_waiting");
}
static bool scan(void) { return ioctl(fd, CMDP_MON_SCAN_ONCE) == 0; }
static bool query(unsigned int i, struct cmdp_monitor_page *p)
{
    INIT(*p); p->addr = BASE + i * PAGE;
    return !ioctl(fd, CMDP_MON_PAGE, p);
}
static bool dirty_page(unsigned int i, struct cmdp_monitor_dirty_page *p)
{
    INIT(*p); p->addr = BASE + i * PAGE;
    return !ioctl(fd, CMDP_MON_DIRTY_PAGE, p);
}
static bool all_state(unsigned int first, unsigned int count, unsigned int state)
{
    struct cmdp_monitor_page p;
    for (unsigned int i = first; i < first + count; i++)
        if (!query(i, &p) || p.state != state) return false;
    return true;
}
static void start(unsigned int n, unsigned int t, unsigned int mode, unsigned int cap)
{
    pages = n; threads = t;
    ctl = mmap(NULL, PAGE, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(ctl != MAP_FAILED, "shared fixture control (excluded mapping)");
    memset(ctl, 0, sizeof(*ctl));
    target_pid = fork();
    if (!target_pid) child();
    CHECK(target_pid > 0 && wait_value(&ctl->ready, threads), "target pages and requested threads ready");
    pidfd = syscall(SYS_pidfd_open, target_pid, 0);
    fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
    struct cmdp_monitor_attach a;
    INIT(a); a.pidfd = pidfd; a.period_ms = 50; a.max_pages = cap; a.max_history = 32768;
    CHECK(pidfd >= 0 && fd >= 0 && !ioctl(fd, CMDP_MON_ATTACH, &a), "attach external monitor");
    struct cmdp_monitor_dirty_config d;
    INIT(d); d.mode = mode; d.quiet_ms = Q_MS;
    CHECK(!ioctl(fd, CMDP_MON_DIRTY_CONFIG, &d), "configure dirty policy");
}
static void finish(void)
{
    CHECK(!ioctl(fd, CMDP_MON_STOP) && balanced(), "STOP drains every batch and all ownership");
    close(fd); fd = -1;
    CHECK(command(2), "target contents unchanged");
    atomic_store(&ctl->quit, 1);
    int status;
    CHECK(waitpid(target_pid, &status, 0) == target_pid && WIFEXITED(status) && !WEXITSTATUS(status),
          "target and all threads exit cleanly");
    target_pid = -1; close(pidfd); pidfd = -1;
    munmap(ctl, PAGE);
}
static bool reset_groups(uint64_t earliest)
{
    struct cmdp_monitor_dirty_page d;
    uint64_t last = 0; unsigned int run = 0, groups = 0;
    for (unsigned int i = 0; i < pages; i++) {
        if (!dirty_page(i, &d) || !d.valid || d.qualified_ns || d.quiet_since_ns < earliest) return false;
        if (d.quiet_since_ns != last) { last = d.quiet_since_ns; run = 0; groups++; }
        if (++run > 64) return false;
        if (i % 64 && run != i % 64 + 1) return false;
    }
    return groups == (pages + 63) / 64;
}
static void boundary(unsigned int n, unsigned int t)
{
    printf("CMDP_BATCH_CASE pages=%u threads=%u kind=boundary\n", n, t);
    start(n, t, CMDP_MON_DIRTY_QUALIFY, 32768);
    uint64_t db = stat_get("dirty_flush_batches"), dp = stat_get("dirty_flush_pages");
    uint64_t begin = now_ns();
    CHECK(scan() && reset_groups(begin), "first visit shares post-flush epochs in groups of at most 64");
    uint64_t batches = stat_get("dirty_flush_batches") - db, resets = stat_get("dirty_flush_pages") - dp;
    CHECK(batches && resets >= n && resets <= batches * 64 && (n < 63 || batches < resets),
          "actual reset flush calls cover multiple pages within the bound");
    printf("CMDP_BATCH_METRIC phase=dirty pages=%" PRIu64 " batches=%" PRIu64 "\n", resets, batches);
    usleep((Q_MS + 30) * 1000);
    uint64_t ab = stat_get("arm_flush_batches"), ap = stat_get("arm_flush_pages");
    CHECK(scan() && all_state(0, n, CMDP_MON_PAGE_ARMED), "all cold fixture pages ARM after Q");
    batches = stat_get("arm_flush_batches") - ab; resets = stat_get("arm_flush_pages") - ap;
    CHECK(batches && resets >= n && resets <= batches * 64 && (n < 63 || batches < resets),
          "ARM permission changes share bounded flush calls");
    printf("CMDP_BATCH_METRIC phase=arm pages=%" PRIu64 " batches=%" PRIu64 "\n", resets, batches);
    struct cmdp_monitor_page before, after;
    CHECK(query(0, &before), "record first active generation");
    uint64_t skips = stat_get("scan_active_skips");
    CHECK(scan() && all_state(0, n, CMDP_MON_PAGE_ARMED) && query(0, &after) &&
          before.arm_generation == after.arm_generation && stat_get("scan_active_skips") - skips >= n,
          "survivors skip page lookup and preserve their generation");
    CHECK(command(1) && all_state(0, n, CMDP_MON_PAGE_OFF_WRITE), "stores revoke every protected fixture page");
    CHECK(scan(), "WP pages establish fresh epochs");
    usleep((Q_MS + 30) * 1000);
    CHECK(scan() && all_state(0, n, CMDP_MON_PAGE_ARMED), "WP pages regain protection only after fresh Q");
    finish();
}
static atomic_int scan_result;
static void *scan_thread(void *unused)
{
    (void)unused; atomic_store(&scan_result, scan() ? 0 : errno); return NULL;
}
static uint64_t wait_phase(unsigned int phase)
{
    uint64_t until = now_ns() + UINT64_C(3000000000);
    while (now_ns() < until) {
        if (stat_get("test_batch_waiting") == phase) return now_ns();
        usleep(100);
    }
    return 0;
}
static void injection(unsigned int phase, unsigned int abort_after)
{
    struct cmdp_monitor_test_batch b;
    INIT(b); b.phase = phase; b.delay_ms = 500; b.addr = BASE; b.abort_after = abort_after;
    CHECK(!ioctl(fd, CMDP_MON_TEST_BATCH, &b), "configure one-shot pre-flush injection");
}
static void reset_window(void)
{
    puts("CMDP_BATCH_CASE pages=129 threads=4 kind=reset-window");
    start(129, 4, CMDP_MON_DIRTY_OBSERVE, 32768);
    injection(CMDP_MON_TEST_BATCH_DIRTY, 0);
    pthread_t scanth;
    CHECK(!pthread_create(&scanth, NULL, scan_thread, NULL), "start reset-window scan");
    uint64_t paused = wait_phase(CMDP_MON_TEST_BATCH_DIRTY);
    CHECK(paused, "observe the actual reset-before-flush window");
    struct cmdp_monitor_dirty_page pending;
    CHECK(dirty_page(0, &pending) && !pending.valid && !pending.quiet_since_ns,
          "pending reset has no valid quiet certificate");
    atomic_store(&ctl->hot, 1);
    uint64_t until = now_ns() + UINT64_C(200000000);
    while (now_ns() < until) {
        bool ready = true;
        for (unsigned int i = 0; i < 4; i++) ready &= atomic_load(&ctl->iterations[i]) > 2;
        if (ready) break;
        usleep(100);
    }
    bool ran = true;
    for (unsigned int i = 0; i < 4; i++) ran &= atomic_load(&ctl->iterations[i]) > 2;
    CHECK(ran && stat_get("test_batch_waiting") == CMDP_MON_TEST_BATCH_DIRTY,
          "all four CPUs store while the reset batch awaits its flush");
    CHECK(!pthread_join(scanth, NULL) && !atomic_load(&scan_result), "reset-window scan finishes");
    atomic_store(&ctl->hot, 0);
    CHECK(command(2) && reset_groups(paused), "quiet epoch begins after the exposed window; data survives");
    CHECK(command(1) && scan(), "write after completed flush, then sample again");
    bool dirty = true; struct cmdp_monitor_dirty_page p;
    for (unsigned int i = 0; i < pages; i++) dirty &= dirty_page(i, &p) && p.last_dirty;
    CHECK(dirty, "post-flush writes are detected on every fixture page");
    struct cmdp_monitor_status st; INIT(st);
    CHECK(!ioctl(fd, CMDP_MON_STATUS, &st) && !st.first_arms && !st.rearms && !st.active,
          "sampling-only remains free of ARM generations");
    finish();
}
static void arm_abort(void)
{
    puts("CMDP_BATCH_CASE pages=129 threads=4 kind=partial-abort");
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 32768);
    CHECK(scan(), "prime abort fixture");
    usleep((Q_MS + 30) * 1000);
    injection(CMDP_MON_TEST_BATCH_ARM, 7);
    uint64_t published = stat_get("publishes");
    pthread_t scanth;
    CHECK(!pthread_create(&scanth, NULL, scan_thread, NULL), "start partial ARM scan");
    CHECK(wait_phase(CMDP_MON_TEST_BATCH_ARM), "observe protected-but-unflushed batch");
    CHECK(stat_get("publishes") == published, "no backend publication precedes the batch flush");
    CHECK(!pthread_join(scanth, NULL) && !atomic_load(&scan_result), "partial abort returns normally");
    bool no_arm = true; struct cmdp_monitor_page p;
    for (unsigned int i = 0; i < 64; i++) no_arm &= query(i, &p) && p.state != CMDP_MON_PAGE_ARMED;
    CHECK(no_arm && all_state(64, 65, CMDP_MON_PAGE_ARMED),
          "aborted chunk has no active ownership; later chunks still complete");
    CHECK(command(1) && command(2), "writes resume through partially protected abort with correct data");
    CHECK(scan(), "aborted pages start fresh observation");
    usleep((Q_MS + 30) * 1000);
    CHECK(scan() && all_state(0, 129, CMDP_MON_PAGE_ARMED), "all aborted pages recover after fresh Q");
    finish();
}
static void arm_window(bool stop)
{
    printf("CMDP_BATCH_CASE pages=129 threads=4 kind=%s\n", stop ? "stop-pending" : "arm-writers");
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 32768);
    CHECK(scan(), "prime ARM-window fixture");
    usleep((Q_MS + 30) * 1000);
    injection(CMDP_MON_TEST_BATCH_ARM, 0);
    uint64_t published = stat_get("publishes");
    CHECK(!ioctl(fd, CMDP_MON_START) && wait_phase(CMDP_MON_TEST_BATCH_ARM),
          "asynchronous worker reaches ARM pre-flush window");
    CHECK(stat_get("publishes") == published, "no premature publication during ARM pause");
    if (stop) {
        uint64_t begin = now_ns();
        CHECK(!ioctl(fd, CMDP_MON_STOP) && now_ns() - begin < UINT64_C(2000000000) && balanced(),
              "STOP flushes and aborts pending ARM without leaking or deadlocking");
    } else {
        atomic_store(&ctl->hot, 1);
        uint64_t until = now_ns() + UINT64_C(3000000000);
        bool ran = false;
        while (now_ns() < until) {
            ran = true;
            for (unsigned int i = 0; i < 4; i++) ran &= atomic_load(&ctl->iterations[i]) > 2;
            if (ran) break;
            usleep(100);
        }
        CHECK(ran, "four writers resume after batch flush and ARM publication");
        atomic_store(&ctl->hot, 0);
        CHECK(!ioctl(fd, CMDP_MON_STOP) && command(2) && balanced(),
              "concurrent ARM/WP preserves all data and drains ownership");
    }
    finish();
}
static void capacity(void)
{
    puts("CMDP_BATCH_CASE pages=129 threads=4 kind=capacity");
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 7);
    CHECK(scan(), "prime capacity fixture"); usleep((Q_MS + 30) * 1000);
    struct cmdp_monitor_status st; INIT(st);
    CHECK(scan() && !ioctl(fd, CMDP_MON_STATUS, &st) && st.active == 7,
          "pending ARM candidates respect the global seven-page limit");
    finish();
}
int main(void)
{
    puts("TAP version 13");
    if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE) return 4;
    alarm(180);
    unsigned int sizes[] = {1, 63, 64, 65, 129, 513};
    for (unsigned int t = 1; t <= 4; t += 3)
        for (unsigned int i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) boundary(sizes[i], t);
    reset_window(); arm_abort(); arm_window(false); arm_window(true); capacity();
    CHECK(balanced(), "final ownership and test-pause state are zero");
    printf("1..%d\n", checks); return 0;
}
