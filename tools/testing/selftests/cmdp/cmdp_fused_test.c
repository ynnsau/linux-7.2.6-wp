// SPDX-License-Identifier: GPL-2.0
/* Reuse the same CPU-writer fixture and legacy regressions, at a 2 MiB base. */
#define BASE UINT64_C(0x1000000000)
#define main cmdp_batch_original_main
#include "cmdp_batch_test.c"
#undef main

static struct cmdp_monitor_batch_status batch_status(void)
{
    struct cmdp_monitor_batch_status s;
    INIT(s);
    CHECK(!ioctl(fd, CMDP_MON_BATCH_STATUS, &s), "read shared-flush and region diagnostics");
    return s;
}
static void configure(unsigned int dirty, unsigned int wp)
{
    struct cmdp_monitor_batch_config c;
    INIT(c); c.dirty_batch_pages = dirty; c.wp_batch_pages = wp;
    CHECK(!ioctl(fd, CMDP_MON_BATCH_CONFIG, &c), "configure independent per-run ceilings");
    struct cmdp_monitor_batch_status s = batch_status();
    CHECK(s.dirty_batch_pages == (dirty ? dirty : 64) && s.wp_batch_pages == (wp ? wp : 64),
          "configuration readback matches both independent ceilings");
}
static void immutable(void)
{
    struct cmdp_monitor_batch_config c;
    INIT(c); c.dirty_batch_pages = 8; c.wp_batch_pages = 8;
    errno = 0;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EBUSY,
          "batch configuration cannot change after the first scan");
}
static bool alternate_state(void)
{
    struct cmdp_monitor_page p;
    struct cmdp_monitor_dirty_page d;
    for (unsigned int i = 0; i < pages; i++) {
        if (!query(i, &p) || !dirty_page(i, &d)) return false;
        if (i % 2 ? (p.state != CMDP_MON_PAGE_ARMED || d.valid) :
                    (p.state == CMDP_MON_PAGE_ARMED || !d.valid || !d.last_dirty || d.qualified_ns))
            return false;
    }
    return true;
}
static void limits(unsigned int dirty, unsigned int wp, unsigned int n, unsigned int t)
{
    printf("CMDP_FUSED_CASE dirty=%u wp=%u pages=%u threads=%u\n", dirty, wp, n, t);
    start(n, t, CMDP_MON_DIRTY_QUALIFY, n + 4096);
    configure(dirty, wp);
    CHECK(scan(), "prime all fixture pages");
    immutable();
    struct cmdp_monitor_dirty_page d;
    CHECK(dirty_page(0, &d) && d.valid && !d.qualified_ns, "first visit only establishes observation");
    usleep((Q_MS + 30) * 1000);
    CHECK(command(3) && scan() && alternate_state(), "mixed scan resets dirty evens and ARMs cold odds");
    struct cmdp_monitor_batch_status s = batch_status();
    CHECK(s.flush_calls == s.shared_flush_calls + s.dirty_only_flush_calls + s.wp_only_flush_calls &&
          s.max_dirty_pages <= dirty && s.max_wp_pages <= wp && s.max_span_pages <= (dirty > wp ? dirty : wp),
          "actual shared calls are counted once and both ceilings hold");
    if (dirty > 1 && wp > 1)
        CHECK(s.shared_flush_calls > 0, "disjoint dirty and ARM candidates use a common shootdown");
    CHECK(s.pmd_cache_hits >= n && s.pmd_walks && s.history_payload_bytes &&
          s.history_allocated_bytes >= s.history_payload_bytes,
          "cached upper walks and compact allocation accounting are exercised");
    printf("CMDP_FUSED_METRIC dirty=%u wp=%u calls=%llu shared=%llu max_dirty=%llu max_wp=%llu "
           "span=%llu time_chunks=%llu max_hold_ns=%llu payload=%llu allocated=%llu\n",
           dirty, wp, s.flush_calls, s.shared_flush_calls, s.max_dirty_pages, s.max_wp_pages,
           s.max_span_pages, s.time_chunks, s.max_hold_ns, s.history_payload_bytes, s.history_allocated_bytes);
    CHECK(command(1) && all_state(1, 1, CMDP_MON_PAGE_OFF_WRITE) && scan(), "writes revoke and re-prime mixed pages");
    usleep((Q_MS + 30) * 1000);
    CHECK(scan() && all_state(0, n, CMDP_MON_PAGE_ARMED), "all pages regain ARM after a fresh quiet interval");
    finish();
}
static void regions(void)
{
    start(2048, 4, CMDP_MON_DIRTY_QUALIFY, 32768);
    configure(64, 64);
    CHECK(scan(), "prime four aligned regions"); usleep((Q_MS + 30) * 1000);
    CHECK(scan() && all_state(0, pages, CMDP_MON_PAGE_ARMED), "all four regions become protected");
    struct cmdp_monitor_batch_status a = batch_status();
    CHECK(scan(), "scan complete protected regions");
    struct cmdp_monitor_batch_status b = batch_status();
    CHECK(b.region_full_skips - a.region_full_skips >= 4 &&
          b.region_skip_pages - a.region_skip_pages >= pages,
          "whole protected 2 MiB regions skip history and PTE work");
    struct cmdp_monitor_page before, after, neighbor;
    CHECK(query(pages / 2, &before) && query(pages / 2 + 1, &neighbor), "save region generations");
    CHECK(command(6) && query(pages / 2, &after) && after.state == CMDP_MON_PAGE_OFF_WRITE,
          "one write clears the region summary bit for that page");
    CHECK(scan() && query(pages / 2 + 1, &after) && after.arm_generation == neighbor.arm_generation,
          "mixed region preserves untouched protected pages");
    usleep((Q_MS + 30) * 1000);
    CHECK(scan() && query(pages / 2, &after) && after.state == CMDP_MON_PAGE_ARMED &&
          after.arm_generation != before.arm_generation && after.lifetime_id == before.lifetime_id,
          "written page re-ARMs as a new generation of its continuous lifetime");
    for (unsigned int op = 4; op <= 5; op++) {
        CHECK(query(pages / 2, &before) && command(op) && query(pages / 2, &after) &&
              after.state != CMDP_MON_PAGE_ARMED, "mapping lifecycle clears protected-region summary");
        CHECK(scan(), "changed mapping starts fresh observation");
        struct cmdp_monitor_dirty_page d;
        CHECK(dirty_page(pages / 2, &d) && d.valid && !d.qualified_ns, "mapping cannot inherit its old quiet certificate");
        usleep((Q_MS + 30) * 1000);
        CHECK(scan() && query(pages / 2, &after) && after.state == CMDP_MON_PAGE_ARMED &&
              after.lifetime_id != before.lifetime_id, "replacement mapping starts a new ARM lifetime");
    }
    finish();
}
static void mixed_window(bool abort, bool stop)
{
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 32768); configure(64, 64);
    CHECK(scan(), "prime mixed-window fixture"); usleep((Q_MS + 30) * 1000);
    CHECK(command(3), "make alternate pages dirty");
    injection(CMDP_MON_TEST_BATCH_ARM, abort ? 7 : 0);
    uint64_t paused;
    CHECK(!ioctl(fd, CMDP_MON_START) && (paused = wait_phase(CMDP_MON_TEST_BATCH_ARM)),
          "mixed batch pauses with dirty resets and ARM changes pending");
    struct cmdp_monitor_dirty_page d;
    struct cmdp_monitor_page pending;
    CHECK(dirty_page(0, &d) && !d.valid && query(1, &pending) &&
          pending.state != CMDP_MON_PAGE_ARMED,
          "neither dirty certificate nor ARM publication escapes before shared flush");
    if (stop) {
        CHECK(!ioctl(fd, CMDP_MON_STOP) && balanced(), "STOP drains mixed dirty and ARM work");
    } else {
        uint64_t until = now_ns() + UINT64_C(3000000000);
        struct cmdp_monitor_status st;
        do {
            INIT(st);
            CHECK(!ioctl(fd, CMDP_MON_STATUS, &st), "read asynchronous mixed scan");
            if (st.passes_completed >= 2) break;
            usleep(20000);
        } while (now_ns() < until);
        CHECK(st.passes_completed >= 2 && dirty_page(0, &d) && d.quiet_since_ns >= paused,
              "new dirty epoch starts after the shared flush pause");
        if (abort) {
            struct cmdp_monitor_page p;
            CHECK(query(1, &p) && p.state != CMDP_MON_PAGE_ARMED,
                  "partial protection abort publishes no ARM ownership in its chunk");
        }
        CHECK(!ioctl(fd, CMDP_MON_STOP) && balanced(), "mixed batch completion drains on STOP");
    }
    CHECK(command(1) && command(2), "mixed flush, abort and STOP preserve target contents");
    finish();
}
static void mixed_writers(void)
{
    start(129, 4, CMDP_MON_DIRTY_QUALIFY, 32768); configure(64, 64);
    CHECK(scan(), "prime concurrent shared-flush fixture"); usleep((Q_MS + 30) * 1000);
    CHECK(command(3), "alternate pages need dirty reset before concurrent ARM");
    injection(CMDP_MON_TEST_BATCH_ARM, 0);
    CHECK(!ioctl(fd, CMDP_MON_START) && wait_phase(CMDP_MON_TEST_BATCH_ARM),
          "shared dirty/ARM batch reaches its pre-flush pause");
    struct cmdp_monitor_dirty_page d;
    struct cmdp_monitor_page p;
    CHECK(dirty_page(0, &d) && !d.valid && query(1, &p) && p.state != CMDP_MON_PAGE_ARMED,
          "both pending sets remain unpublished before concurrent writers");
    atomic_store(&ctl->hot, 1);
    uint64_t until = now_ns() + UINT64_C(3000000000);
    bool ran = false;
    while (now_ns() < until) {
        ran = true;
        for (unsigned int i = 0; i < 4; i++) ran &= atomic_load(&ctl->iterations[i]) > 2;
        if (ran) break;
        usleep(100);
    }
    CHECK(ran, "all four CPU writers resume through the shared flush and WP handling");
    atomic_store(&ctl->hot, 0);
    CHECK(!ioctl(fd, CMDP_MON_STOP) && command(2) && balanced(),
          "concurrent shared-flush writers retain data and drain cleanly");
    struct cmdp_monitor_status st; INIT(st);
    CHECK(!ioctl(fd, CMDP_MON_STATUS, &st) && st.terminals[CMDP_MON_WRITE] >= 64,
          "writers actually revoke the protected odd fixture pages");
    struct cmdp_monitor_batch_status b = batch_status();
    CHECK(b.shared_flush_calls > 0, "concurrent writer case actually uses shared shootdowns");
    printf("CMDP_FUSED_MIXED_WRITERS threads=4 resumed=4 wp=%llu shared=%llu\n",
           st.terminals[CMDP_MON_WRITE], b.shared_flush_calls);
    finish();
}

static void allocation_bound(void)
{
    start(2049, 1, CMDP_MON_DIRTY_OBSERVE, 32768);
    close(fd);
    fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
    struct cmdp_monitor_attach a; INIT(a);
    a.pidfd = pidfd; a.max_pages = 32768; a.max_history = 256;
    CHECK(fd >= 0 && !ioctl(fd, CMDP_MON_ATTACH, &a), "attach small reserved-history budget");
    struct cmdp_monitor_dirty_config d; INIT(d); d.mode = CMDP_MON_DIRTY_OBSERVE; d.quiet_ms = Q_MS;
    CHECK(!ioctl(fd, CMDP_MON_DIRTY_CONFIG, &d) && scan(), "sample with partial blocks in separate regions");
    struct cmdp_monitor_status st; INIT(st);
    struct cmdp_monitor_batch_status b = batch_status();
    CHECK(!ioctl(fd, CMDP_MON_STATUS, &st) && st.history_entries <= 256 && st.resource_failures &&
          b.history_allocated_bytes <= 256 * 160 + 2 * 512 && !st.active,
          "reserved history slots bound sparse allocation and fail closed on exhaustion");
    printf("CMDP_FUSED_ALLOCATION_BOUND history=%llu allocated=%llu resources=%llu\n",
           st.history_entries, b.history_allocated_bytes, st.resource_failures);
    finish();
}

static void validation(void)
{
    start(1, 1, CMDP_MON_DIRTY_OBSERVE, 32768);
    configure(0, 0);
    struct cmdp_monitor_batch_config c;
    INIT(c); c.dirty_batch_pages = CMDP_BATCH_MAX + 1;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EINVAL, "reject dirty cap above 65536");
    INIT(c); c.wp_batch_pages = CMDP_BATCH_MAX + 1;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EINVAL, "reject WP cap above 65536");
    INIT(c); c.reserved[1] = 1;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EINVAL, "reject reserved configuration bits");
    INIT(c); c.version++;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EINVAL, "reject unsupported configuration version");
    INIT(c); c.size--;
    CHECK(ioctl(fd, CMDP_MON_BATCH_CONFIG, &c) == -1 && errno == EINVAL, "reject incorrect configuration size");
    CHECK(scan(), "sampling-only scan uses new implementation"); immutable(); finish();
}
int main(void)
{
    puts("TAP version 13");
    if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE) return 4;
    alarm(600); validation();
    const unsigned int cases[][3] = {
        {1, 1, 129}, {1, 64, 129}, {64, 1, 129}, {17, 91, 2049},
        {64, 64, 2049}, {512, 512, 2049}, {4096, 4096, 8193},
        {16384, 16384, 17001}, {65536, 65536, 66049},
        {65536, 64, 8193}, {64, 65536, 8193}
    };
    for (unsigned int t = 1; t <= 4; t += 3)
        for (unsigned int i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
            limits(cases[i][0], cases[i][1], cases[i][2], t);
    regions(); allocation_bound(); mixed_writers(); mixed_window(false, false); mixed_window(true, false); mixed_window(false, true);
    CHECK(balanced(), "final shared-flush ownership is zero");
    printf("1..%d\n", checks); return 0;
}
