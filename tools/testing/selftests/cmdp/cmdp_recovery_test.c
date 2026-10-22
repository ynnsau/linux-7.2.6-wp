// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <poll.h>
#include <sched.h>
#include <setjmp.h>
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
#define PAGE 4096UL
#define X86_SAVED_DIRTY (UINT64_C(1) << 58) /* x86-64 leaf PTE software bit */
#define Q_MS 200
#define PERIOD_MS 50
#define INIT(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
	(x).version = CMDP_MONITOR_VERSION; } while (0)
static int checks, fd = -1, pidfd = -1, commands = -1, replies = -1;
static pid_t child = -1;
static uintptr_t base;
static unsigned int threads = 1, pages = 64, numa_seconds;
static bool expect_stranded, cross_node;
struct request { char op; unsigned int page; };
struct reply { uint64_t base, iterations; int error, node; };
struct workers {
	unsigned char *area;
	atomic_int mode;
	atomic_uint active;
	atomic_bool quit;
	atomic_ullong iterations;
};
struct worker_arg { struct workers *w; unsigned int id; };
static sigjmp_buf fault_return;
static volatile sig_atomic_t expected_fault;
static uint64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static bool transfer(int f, void *buf, size_t n, bool out)
{
	char *p = buf;
	while (n) {
		ssize_t r = out ? write(f, p, n) : read(f, p, n);
		if (r < 0 && errno == EINTR) continue;
		if (r <= 0) return false;
		p += r; n -= r;
	}
	return true;
}
static uint64_t marker(unsigned int i) { return UINT64_C(0x12345678fedc0000) ^ i; }
static void store(unsigned char *p, unsigned int i)
{
	*(volatile uint64_t *)p = marker(i);
	*(volatile uint64_t *)(p + PAGE - 8) = ~marker(i);
}
static bool verify(const unsigned char *p, unsigned int i)
{
	return *(const volatile uint64_t *)p == marker(i) &&
		*(const volatile uint64_t *)(p + PAGE - 8) == ~marker(i);
}
static bool pin(unsigned int cpu)
{
	cpu_set_t mask;
	CPU_ZERO(&mask); CPU_SET(cpu, &mask);
	return !sched_setaffinity(0, sizeof(mask), &mask);
}
static void segv(int sig)
{
	if (expected_fault) siglongjmp(fault_return, 1);
	_exit(128 + sig);
}
static bool readonly_faults(unsigned char *p)
{
	expected_fault = 1;
	if (!sigsetjmp(fault_return, 1)) {
		*(volatile unsigned char *)p = 1;
		expected_fault = 0;
		return false;
	}
	expected_fault = 0;
	return true;
}
static void *worker(void *opaque)
{
	struct worker_arg *a = opaque;
	struct workers *w = a->w;
	if (!pin((cross_node ? 2 : 0) + (a->id & 1))) _exit(21);
	while (!atomic_load(&w->quit)) {
		int mode = atomic_load(&w->mode);
		if (!mode) { usleep(100); continue; }
		atomic_fetch_add(&w->active, 1);
		while (atomic_load(&w->mode) == mode) {
			if (mode == 2) store(w->area + (12 + a->id) * PAGE, 12 + a->id);
			else for (unsigned int i = 0; i < pages; i++)
				if (!verify(w->area + i * PAGE, i)) _exit(22);
			atomic_fetch_add(&w->iterations, 1);
		}
		atomic_fetch_sub(&w->active, 1);
	}
	return NULL;
}
static void workers_stop(struct workers *w)
{
	atomic_store(&w->mode, 0);
	while (atomic_load(&w->active)) usleep(100);
}
static void target(int input, int output)
{
	if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0) || !pin(0)) _exit(10);
	struct sigaction sa = { .sa_handler = segv };
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGSEGV, &sa, NULL)) _exit(11);
	unsigned char *p = mmap(NULL, pages * PAGE, PROT_READ | PROT_WRITE,
		MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) _exit(12);
	for (unsigned int i = 0; i < pages; i++) store(p + i * PAGE, i);
	struct workers w = { .area = p };
	pthread_t th[3]; struct worker_arg args[3];
	for (unsigned int i = 1; i < threads; i++) {
		args[i - 1] = (struct worker_arg){ &w, i };
		if (pthread_create(&th[i - 1], NULL, worker, &args[i - 1])) _exit(13);
	}
	struct reply r = { .base = (uintptr_t)p };
	if (!transfer(output, &r, sizeof(r), true)) _exit(14);
	struct request q;
	while (transfer(input, &q, sizeof(q), false)) {
		r.error = 0; r.node = -1;
		unsigned char *v = p + q.page * PAGE;
		switch (q.op) {
		case 'R':
			/* Both the single-thread target and all four target threads read
			 * the fixture. No target writes can repair an orphaned PTE. */
			atomic_store(&w.mode, 1);
			for (unsigned int i = 0; i < pages; i++) if (!verify(p + i * PAGE, i)) r.error = 1;
			usleep(1000); workers_stop(&w);
			break;
		case 'W': store(v, q.page); break;
		case 'F': for (unsigned int i = 0; i < pages; i++) store(p + i * PAGE, i); break;
		case 'P':
			r.error = mprotect(v, PAGE, PROT_READ);
			if (!r.error && !readonly_faults(v)) r.error = 1;
			break;
		case 'U': r.error = mprotect(v, PAGE, PROT_READ | PROT_WRITE); break;
		case 'M':
			if (mmap(v, PAGE, PROT_READ | PROT_WRITE,
				 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) != v) r.error = 1;
			else store(v, q.page);
			break;
		case 'D':
			r.error = madvise(v, PAGE, MADV_DONTNEED);
			if (!r.error) {
				if (*(volatile uint64_t *)v) r.error = 1;
				store(v, q.page);
			}
			break;
		case 'N': {
			void *page = v; int node = 1, status = -1;
			long rc = syscall(SYS_move_pages, 0, 1, &page, &node, &status, 2);
			r.error = rc < 0 || status != 1; r.node = status;
			break;
		}
		case 'B': {
			atomic_store(&w.mode, 2);
			while (atomic_load(&w.active) != threads - 1) usleep(100);
			/* Acknowledge all writers active. They run until the controller
			 * finishes its invalidation scans and explicitly sends T. */
			r.iterations = atomic_load(&w.iterations);
			if (!transfer(output, &r, sizeof(r), true)) _exit(18);
			uint64_t end = now_ns() + UINT64_C(20000000000);
			struct pollfd pollfd = { .fd = input, .events = POLLIN };
			while (now_ns() < end) {
				store(p + 12 * PAGE, 12); atomic_fetch_add(&w.iterations, 1);
				if (poll(&pollfd, 1, 0) > 0) {
					struct request stop_request;
					if (!transfer(input, &stop_request, sizeof(stop_request), false) || stop_request.op != 'T') r.error = 1;
					break;
				}
			}
			if (now_ns() >= end) r.error = 1;
			workers_stop(&w);
			break;
		}
		case 'A': {
			if (!pin(cross_node ? 2 : 0)) _exit(15);
			atomic_store(&w.mode, 1);
			uint64_t end = now_ns() + (uint64_t)numa_seconds * 1000000000;
			do {
				for (unsigned int i = 0; i < pages; i++)
					if (!verify(p + i * PAGE, i)) r.error = 1;
				atomic_fetch_add(&w.iterations, 1);
			} while (!r.error && now_ns() < end);
			workers_stop(&w);
			break;
		}
		case 'X':
			workers_stop(&w); atomic_store(&w.quit, true);
			for (unsigned int i = 1; i < threads; i++) pthread_join(th[i - 1], NULL);
			_exit(0);
		default: _exit(16);
		}
		r.iterations = atomic_load(&w.iterations);
		if (!transfer(output, &r, sizeof(r), true)) _exit(17);
	}
	_exit(0);
}
static bool send_command(char op, unsigned int page)
{
	struct request q = { op, page };
	return transfer(commands, &q, sizeof(q), true);
}
static bool command(char op, unsigned int page)
{
	struct reply r;
	return send_command(op, page) && transfer(replies, &r, sizeof(r), false) && !r.error;
}
static bool status(struct cmdp_monitor_status *s)
{
	INIT(*s); return !ioctl(fd, CMDP_MON_STATUS, s);
}
static bool snapshot(unsigned int i, struct cmdp_monitor_page_snapshot *s)
{
	uint64_t deadline = now_ns() + UINT64_C(2000000000);
	do {
		INIT(*s); s->addr = base + i * PAGE;
		if (!ioctl(fd, CMDP_MON_PAGE_SNAPSHOT, s)) return true;
		if (errno != EAGAIN) return false;
		usleep(1000);
	} while (now_ns() < deadline);
	return false;
}
static bool diagnostic(struct cmdp_monitor_diagnostics *d)
{
	INIT(*d); return !ioctl(fd, CMDP_MON_DIAGNOSTICS, d);
}
static bool reconciled(void)
{
	struct cmdp_monitor_status s;
	struct cmdp_monitor_diagnostics d;
	if (!status(&s) || !diagnostic(&d)) return false;
	uint64_t sum = s.active, unknown = 0;
	for (unsigned int i = 0; i < CMDP_MON_NR_REASONS; i++) sum += s.terminals[i];
	for (unsigned int i = 0; i < CMDP_MON_NR_UNKNOWNS; i++) unknown += d.rejected[i];
	return sum == s.first_arms + s.rearms && unknown == d.unknown;
}
static bool scan(void) { return !ioctl(fd, CMDP_MON_SCAN_ONCE); }
static bool invalidate(unsigned int first, unsigned int count)
{
	struct cmdp_monitor_test_invalidate v;
	INIT(v); v.start = base + first * PAGE; v.end = v.start + count * PAGE;
	return !ioctl(fd, CMDP_MON_TEST_INVALIDATE, &v);
}
static bool owned(unsigned int first, unsigned int count, bool want)
{
	struct cmdp_monitor_page_snapshot s;
	for (unsigned int i = first; i < first + count; i++)
		if (!snapshot(i, &s) || !!s.owned != want) return false;
	return true;
}
static void quiet_wait(void) { usleep((Q_MS + 30) * 1000); }
static bool freshly_qualified(unsigned int i, uint64_t after)
{
	struct cmdp_monitor_page_snapshot s;
	return snapshot(i, &s) && s.owned && !s.observation_valid &&
		s.quiet_since_ns >= after && s.qualified_ns >= s.quiet_since_ns + (uint64_t)Q_MS * 1000000;
}
static bool attach_mode(unsigned int mode)
{
	struct cmdp_monitor_attach a;
	struct cmdp_monitor_dirty_config c;
	fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
	if (fd < 0) return false;
	INIT(a); a.pidfd = pidfd; a.period_ms = PERIOD_MS;
	a.max_pages = 262144; a.max_history = 524288;
	INIT(c); c.mode = mode; c.quiet_ms = Q_MS;
	return !ioctl(fd, CMDP_MON_ATTACH, &a) && !ioctl(fd, CMDP_MON_DIRTY_CONFIG, &c);
}
static bool attach(void) { return attach_mode(CMDP_MON_DIRTY_QUALIFY); }
static bool balanced(void)
{
	FILE *f = fopen("/sys/kernel/debug/cmdp/stats", "r");
	char key[64]; unsigned long long value; unsigned int seen = 0;
	bool ok = f != NULL;
	if (!f) return false;
	while (fscanf(f, "%63s %llu", key, &value) == 2)
		if (!strcmp(key, "entries") || !strcmp(key, "managed") || !strcmp(key, "isolated")) {
			seen++; ok &= value == 0;
		}
	fclose(f); return ok && seen == 3;
}
static bool stop(void)
{
	struct cmdp_monitor_status s;
	bool ok = !ioctl(fd, CMDP_MON_STOP) && status(&s) && !s.active &&
		s.state == CMDP_MON_STOPPED && reconciled();
	close(fd); fd = -1;
	return ok && balanced();
}
static void print_page(const char *stage, unsigned int i)
{
	struct cmdp_monitor_page_snapshot s;
	if (!snapshot(i, &s)) return;
	printf("CMDP_RECOVERY_PAGE stage=%s page=%u category=%u pfn=%" PRIu64
	       " flags=%" PRIx64 " owned=%u state=%u generation=%" PRIu64
	       " epoch=%" PRIu64 " valid=%u quiet_ns=%" PRIu64 " qualified_ns=%" PRIu64 " now_ns=%" PRIu64 "\n",
	       stage, i, s.category, (uint64_t)s.pfn, (uint64_t)s.pte_flags, s.owned,
	       s.history_state, (uint64_t)s.arm_generation, (uint64_t)s.epoch_id,
	       s.observation_valid, (uint64_t)s.quiet_since_ns, (uint64_t)s.qualified_ns, (uint64_t)s.now_ns);
}
#define CHECK(ok, label) do { checks++; errno = 0; if (!(ok)) { \
	printf("not ok %d - %s (errno=%d %s)\n", checks, label, errno, strerror(errno)); \
	goto fail; } printf("ok %d - %s\n", checks, label); } while (0)
int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--threads") && ++i < argc) threads = atoi(argv[i]);
		else if (!strcmp(argv[i], "--expect-stranded")) expect_stranded = true;
		else if (!strcmp(argv[i], "--numa-seconds") && ++i < argc) numa_seconds = atoi(argv[i]);
		else if (!strcmp(argv[i], "--cross-node")) cross_node = true;
		else return 2;
	}
	if ((threads != 1 && threads != 4) || numa_seconds > 120 ||
	    (expect_stranded && numa_seconds) || (cross_node && !numa_seconds)) return 2;
	if (numa_seconds) pages = 32768;
	setvbuf(stdout, NULL, _IOLBF, 0);
	puts("TAP version 13");
	if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE || access(CMDP_MONITOR_DEVICE, R_OK | W_OK)) {
		puts("1..0 # SKIP requires root, 4 KiB pages, /dev/cmdp_monitor"); return 4;
	}
	alarm(180);
	printf("CMDP_RECOVERY_CONFIG threads=%u pages=%u quiet_ms=%u period_ms=%u expected=%s numa_seconds=%u cross_node=%u\n",
	       threads, pages, Q_MS, PERIOD_MS, expect_stranded ? "stranded" : "recovered", numa_seconds, cross_node);
	int c[2], r[2];
	CHECK(!pipe2(c, O_CLOEXEC) && !pipe2(r, O_CLOEXEC), "create fixture channels");
	child = fork();
	if (!child) { close(c[1]); close(r[0]); target(c[0], r[1]); }
	CHECK(child > 0, "fork target before attaching monitor");
	close(c[0]); close(r[1]); commands = c[1]; replies = r[0];
	struct reply reply;
	CHECK(transfer(replies, &reply, sizeof(reply), false), "target pages and exact requested thread count ready");
	base = reply.base; pidfd = syscall(SYS_pidfd_open, child, 0);
	CHECK(pidfd >= 0 && attach() && scan(), "attach and prime fresh dirty-qualified observation");
	quiet_wait();
	CHECK(scan() && owned(0, pages, true), "every initialized fixture page reaches ARM after full Q");
	struct cmdp_monitor_page_snapshot before, after;
	struct cmdp_monitor_diagnostics d0, d1;
	CHECK(snapshot(0, &before) && diagnostic(&d0) && reconciled(), "capture initial ownership, PTE, epoch and diagnostics");
	print_page("armed", 0);
	if (numa_seconds) {
		CHECK(!ioctl(fd, CMDP_MON_START) && command('A', 0), "read-only workload runs on selected NUMA node under periodic monitoring");
		CHECK(diagnostic(&d1) && d1.source[CMDP_MON_SOURCE_NUMA].calls > d0.source[CMDP_MON_SOURCE_NUMA].calls &&
		      d1.source[CMDP_MON_SOURCE_NUMA].arm_revoked > d0.source[CMDP_MON_SOURCE_NUMA].arm_revoked,
		      "actual automatic NUMA notifier exercised and revoked ARM generations");
		uint64_t deadline = now_ns() + UINT64_C(5000000000);
		bool recovered = false;
		do {
			CHECK(command('R', 0) && snapshot(0, &after), "read-only NUMA recovery observation");
			recovered = after.owned && after.arm_generation != before.arm_generation &&
				after.qualified_ns >= after.quiet_since_ns + (uint64_t)Q_MS * 1000000;
			if (!recovered) usleep(PERIOD_MS * 1000);
		} while (!recovered && now_ns() < deadline);
		CHECK(recovered, "fixture page regains a new qualified ARM generation after real NUMA invalidation");
		print_page("numa-recovered", 0);
		printf("CMDP_RECOVERY_NUMA threads=%u cross_node=%u calls=%" PRIu64 " arm_revoked=%" PRIu64 " recovered=1\n",
		       threads, cross_node, (uint64_t)(d1.source[CMDP_MON_SOURCE_NUMA].calls - d0.source[CMDP_MON_SOURCE_NUMA].calls),
		       (uint64_t)(d1.source[CMDP_MON_SOURCE_NUMA].arm_revoked - d0.source[CMDP_MON_SOURCE_NUMA].arm_revoked));
		CHECK(stop() && command('R', 0), "NUMA integration preserves data and drains ownership");
		goto finish;
	}
	uint64_t invalidated_ns = now_ns();
	CHECK(invalidate(0, pages) && owned(0, pages, false) && snapshot(0, &after) &&
	      after.pfn == before.pfn && after.pte_flags == before.pte_flags &&
	      !after.observation_valid && after.history_state == CMDP_MON_PAGE_INVALIDATED,
	      "external no-op invalidation ends ownership and epoch without changing exact PTE");
	print_page("invalidated", 0);
	CHECK(diagnostic(&d1) && d1.source[CMDP_MON_SOURCE_TEST].arm_revoked - d0.source[CMDP_MON_SOURCE_TEST].arm_revoked == pages,
	      "test-source diagnostics account for every revoked fixture generation");
	CHECK(command('R', 0) && scan() && snapshot(0, &after), "read-only workload and first post-invalidation scan");
	print_page("first-observation", 0);
	if (expect_stranded) {
		for (unsigned int i = 0; i < 4; i++) {
			quiet_wait();
			CHECK(command('R', 0) && scan() && owned(0, pages, false), "baseline remains stranded through reads and repeated full-Q scans");
		}
		CHECK(snapshot(0, &after) && !after.observation_valid && after.epoch_id == before.epoch_id &&
		      diagnostic(&d1) && d1.rejected[CMDP_MON_UNKNOWN_READONLY] > d0.rejected[CMDP_MON_UNKNOWN_READONLY],
		      "baseline has unchanged mapping, invalid quiet epoch and explicit read-only rejections");
		print_page("stranded", 0);
		puts("CMDP_RECOVERY_EXPECTED_FAILURE_REPRODUCED stranded=yes");
		CHECK(stop() && command('R', 0), "baseline cleanup preserves application data");
		goto finish;
	}
	CHECK(!after.owned && after.observation_valid && after.epoch_id > before.epoch_id &&
	      after.quiet_since_ns >= invalidated_ns && !after.qualified_ns,
	      "first recovery visit establishes a new epoch without immediate ARM");
	usleep(Q_MS * 1000 / 4);
	CHECK(command('R', 0) && scan() && owned(0, pages, false), "old quiet age cannot admit pages before a fresh full Q");
	quiet_wait();
	CHECK(command('R', 0) && scan() && owned(0, pages, true), "all cold pages recover ARM without any application stores");
	CHECK(freshly_qualified(0, invalidated_ns) && snapshot(0, &after) && after.arm_generation != before.arm_generation,
	      "recovered ARM has new generation and measured full quiet interval");
	print_page("recovered", 0);
	printf("CMDP_RECOVERY_LATENCY threads=%u invalidated_ns=%" PRIu64 " recovered_ns=%" PRIu64 " latency_ns=%" PRIu64 "\n",
	       threads, invalidated_ns, (uint64_t)after.now_ns, (uint64_t)after.now_ns - invalidated_ns);
	struct cmdp_monitor_page p;
	for (unsigned int i = 0; i < 4; i++) {
		INIT(p); p.addr = base + i * PAGE;
		CHECK(command('W', i) && !ioctl(fd, CMDP_MON_PAGE, &p) && p.state == CMDP_MON_PAGE_OFF_WRITE &&
		      p.reason == CMDP_MON_WRITE, "write after recovery revokes its new ARM generation");
	}
	CHECK(scan() && owned(0, 4, false), "post-WP first observation cannot immediately re-ARM");
	quiet_wait();
	CHECK(scan() && owned(0, 4, true), "written subset re-ARMs after another full quiet interval");
	CHECK(command('P', 5) && scan() && owned(5, 1, false) && command('R', 0),
	      "mprotect read-only remains outside writable admission and an actual store faults");
	CHECK(command('U', 5) && scan(), "restore legitimate write permission and start fresh observation");
	quiet_wait(); CHECK(scan() && owned(5, 1, true), "restored writable mapping qualifies afresh");
	const char operations[] = { 'M', 'D', 'N' };
	for (unsigned int i = 0; i < sizeof(operations); i++) {
		unsigned int page = 6 + i;
		CHECK(snapshot(page, &before) && before.owned, "lifecycle negative begins with an ARMED page");
		if (operations[i] == 'N') {
			/* ARMED folios are LRU-isolated and intentionally cannot migrate.
			 * Release ownership, then migrate a fresh recovery observation. */
			CHECK(invalidate(page, 1) && scan() && snapshot(page, &before) &&
			      !before.owned && before.observation_valid, "prime unowned recovery before explicit migration");
		}
		uint64_t changed_ns = now_ns();
		CHECK(command(operations[i], page) && snapshot(page, &after) && !after.owned && !after.observation_valid,
		      "mapping replacement, DONTNEED or migration discards ARMED ownership and epoch");
		if (operations[i] == 'N') CHECK(after.pfn != before.pfn, "explicit NUMA migration changes physical backing");
		CHECK(scan() && snapshot(page, &after) && !after.owned && after.observation_valid &&
		      after.epoch_id > before.epoch_id && after.quiet_since_ns >= changed_ns,
		      "changed backing cannot inherit prior quiet evidence");
		quiet_wait();
		CHECK(scan() && freshly_qualified(page, changed_ns), "changed mapping waits a fresh full Q before ARM");
	}
	CHECK(invalidate(9, 1) && scan() && snapshot(9, &before) && before.observation_valid,
	      "prime read-only recovery epoch for intervening-write negative");
	CHECK(command('W', 9) && scan() && snapshot(9, &after) && !after.owned && after.observation_valid &&
	      after.epoch_id > before.epoch_id, "store during recovery restarts quiet evidence");
	uint64_t write_epoch = after.quiet_since_ns;
	quiet_wait(); CHECK(scan() && freshly_qualified(9, write_epoch), "intervening write requires a full new quiet interval");
	if (threads == 4) {
		CHECK(send_command('B', 0) && transfer(replies, &reply, sizeof(reply), false) && !reply.error,
		      "four writers acknowledge active before controller begins invalidation scans");
		uint64_t iterations_before = reply.iterations;
		for (unsigned int i = 0; i < 20; i++) CHECK(invalidate(12, 4) && scan(), "concurrent writes and external invalidation complete");
		CHECK(send_command('T', 0) && transfer(replies, &reply, sizeof(reply), false) && !reply.error &&
		      reply.iterations > iterations_before + 1000 && command('R', 0),
		      "four concurrent writers preserve all page markers");
		CHECK(scan(), "fresh post-race observation");
		quiet_wait(); CHECK(scan() && owned(12, 4, true), "raced pages regain ARM after becoming quiet");
	}
	CHECK(reconciled(), "generation conservation and exclusive rejection sums reconcile");
	CHECK(invalidate(0, pages) && scan() && snapshot(0, &after) && after.observation_valid,
	      "enter recovery observation before STOP cancellation");
	CHECK(stop() && command('R', 0), "STOP during recovery drains ownership without data damage");
	CHECK(command('F', 0) && attach_mode(CMDP_MON_DIRTY_DISABLED) && scan() && owned(0, pages, true),
	      "unconditional monitor arms dirty pages to construct legitimate saved-dirty PTEs");
	CHECK(snapshot(0, &before) && before.category == CMDP_MON_PTE_SAVED_DIRTY &&
	      invalidate(0, pages) && snapshot(0, &after) && !after.owned &&
	      after.category == CMDP_MON_PTE_SAVED_DIRTY && after.pte_flags == before.pte_flags,
	      "external no-op invalidation leaves real read-only saved-dirty encoding unchanged");
	print_page("saved-dirty-untracked", 0);
	CHECK(stop() && attach() && scan(), "fresh qualified monitor observes legitimate saved-dirty mapping");
	struct cmdp_monitor_dirty_page dirty;
	INIT(dirty); dirty.addr = base;
	CHECK(!ioctl(fd, CMDP_MON_DIRTY_PAGE, &dirty) && dirty.valid && dirty.last_dirty &&
	      snapshot(0, &after) && !after.owned && after.category == CMDP_MON_PTE_READONLY &&
	      after.pfn == before.pfn && after.pte_flags == (before.pte_flags & ~X86_SAVED_DIRTY),
	      "saved-dirty observation harvests dirty evidence, clears only that bit and starts fresh Q");
	print_page("saved-dirty-primed", 0);
	quiet_wait(); CHECK(scan() && owned(0, pages, true), "new attachment arms cold mapping");
	CHECK(invalidate(0, pages) && scan() && snapshot(0, &after) && after.observation_valid,
	      "enter recovery before target exit");
	CHECK(send_command('X', 0), "target exits while pages are observing recovery");
	int child_status;
	CHECK(waitpid(child, &child_status, 0) == child && WIFEXITED(child_status) && !WEXITSTATUS(child_status),
	      "target threads exit normally during recovery");
	child = -1;
	CHECK(stop() && balanced(), "exit and final STOP leave no owned or isolated pages");
	puts("CMDP_RECOVERY_FIXED_VERIFIED recovered=yes");
	goto done;
finish:
	CHECK(send_command('X', 0), "request clean target exit");
	CHECK(waitpid(child, &child_status, 0) == child && WIFEXITED(child_status) && !WEXITSTATUS(child_status), "target exits normally");
	child = -1;
done:
	close(pidfd); close(commands); close(replies);
	printf("1..%d\n", checks); return 0;
fail:
	if (fd >= 0) { ioctl(fd, CMDP_MON_STOP); close(fd); }
	if (child > 0) { kill(child, SIGKILL); waitpid(child, NULL, 0); }
	printf("1..%d\n", checks); return 1;
}
