// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <linux/cmdp.h>

/* The test kernel is new; the build host may have older libc headers. */
#ifndef SYS_pidfd_open
#if defined(__x86_64__)
#define SYS_pidfd_open 434
#else
#error "pidfd_open syscall number unavailable"
#endif
#endif
#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif
#ifndef MADV_COLD
#define MADV_COLD 20
#define MADV_PAGEOUT 21
#endif

#define PAGE 4096UL
#define RECLAIM_FIRST 64
#define RECLAIM_PAGES 2048
#define PAGES (RECLAIM_FIRST + RECLAIM_PAGES)
#define Q_MS 200
#define INIT(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
	(x).version = CMDP_MONITOR_VERSION; } while (0)
static int checks, fd = -1, pidfd = -1, command_fd = -1, response_fd = -1;
static pid_t child_pid = -1;
static uintptr_t base;
static uint64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static bool transfer(int f, void *data, size_t n, bool output)
{
	char *p = data;
	while (n) {
		ssize_t r = output ? write(f, p, n) : read(f, p, n);
		if (r < 0 && errno == EINTR) continue;
		if (r <= 0) return false;
		p += r; n -= r;
	}
	return true;
}
struct reply { uint64_t addr, iterations; int result, migrated; };
struct workers {
	unsigned char *area;
	atomic_bool run, quit, churn;
	atomic_uint active, churning;
	atomic_ullong iterations;
};
struct writer_arg { struct workers *workers; unsigned int index; };
static uint64_t marker(unsigned int i) { return UINT64_C(0x123456789abcdef0) ^ i; }
static void store_page(unsigned char *p, uint64_t value)
{
	*(volatile uint64_t *)p = value;
	*(volatile uint64_t *)(p + PAGE - sizeof(value)) = ~value;
}
static bool verify_page(const unsigned char *p, uint64_t value)
{
	return *(const volatile uint64_t *)p == value &&
		*(const volatile uint64_t *)(p + PAGE - sizeof(value)) == ~value;
}
static uint64_t reclaim_word(unsigned int page, unsigned int word, uint64_t generation)
{
	return marker(page) ^ (UINT64_C(0x9e3779b97f4a7c15) * (word + 1)) ^ generation;
}
static void store_reclaim_page(unsigned char *p, unsigned int page, uint64_t generation)
{
	for (unsigned int i = 0; i < PAGE / sizeof(uint64_t); i++)
		((volatile uint64_t *)p)[i] = reclaim_word(page, i, generation);
}
static bool verify_reclaim_page(const unsigned char *p, unsigned int page, uint64_t generation)
{
	for (unsigned int i = 0; i < PAGE / sizeof(uint64_t); i++)
		if (((const volatile uint64_t *)p)[i] != reclaim_word(page, i, generation)) return false;
	return true;
}
static void *writer(void *opaque)
{
	struct writer_arg *arg = opaque;
	struct workers *w = arg->workers;
	cpu_set_t mask;
	CPU_ZERO(&mask); CPU_SET(arg->index - 1, &mask);
	if (pthread_setaffinity_np(pthread_self(), sizeof(mask), &mask)) _exit(18);
	uint64_t value = marker(arg->index);
	while (!atomic_load(&w->quit)) {
		if (!atomic_load(&w->run)) { usleep(100); continue; }
		atomic_fetch_add(&w->active, 1);
		while (atomic_load(&w->run)) {
			store_page(w->area + arg->index * PAGE, value);
			atomic_fetch_add(&w->iterations, 1);
		}
		atomic_fetch_sub(&w->active, 1);
	}
	return NULL;
}
static void *churner(void *opaque)
{
	struct workers *w = opaque;
	while (!atomic_load(&w->quit)) {
		if (!atomic_load(&w->churn)) { usleep(100); continue; }
		atomic_store(&w->churning, 1);
		while (atomic_load(&w->churn)) {
			if (mprotect(w->area + 8 * PAGE, PAGE, PROT_READ) ||
			    mprotect(w->area + 8 * PAGE, PAGE, PROT_READ | PROT_WRITE) ||
			    madvise(w->area + 9 * PAGE, PAGE, MADV_DONTNEED)) _exit(31);
			store_page(w->area + 9 * PAGE, marker(9));
		}
		atomic_store(&w->churning, 0);
	}
	return NULL;
}
static void stop_workers(struct workers *w)
{
	atomic_store(&w->run, false); atomic_store(&w->churn, false);
	while (atomic_load(&w->active) || atomic_load(&w->churning)) usleep(100);
}
static void target(int input, int output)
{
	if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)) _exit(10);
	cpu_set_t mask;
	CPU_ZERO(&mask); CPU_SET(0, &mask);
	if (sched_setaffinity(0, sizeof(mask), &mask)) _exit(17);
	/* A known address makes mapping replacement and pagemap checks explicit. */
	unsigned char *p = mmap((void *)UINT64_C(0x100000000), PAGES * PAGE,
		PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
	if (p == MAP_FAILED) _exit(11);
	for (unsigned int i = 0; i < PAGES; i++) {
		if (i < RECLAIM_FIRST) store_page(p + i * PAGE, marker(i));
		else store_reclaim_page(p + i * PAGE, i, 0);
	}
	struct workers w = { .area = p };
	pthread_t threads[5]; struct writer_arg args[4];
	for (unsigned int i = 0; i < 4; i++) {
		args[i] = (struct writer_arg){ &w, i + 1 };
		if (pthread_create(&threads[i], NULL, writer, &args[i])) _exit(12);
	}
	if (pthread_create(&threads[4], NULL, churner, &w)) _exit(13);
	struct reply r = { .addr = (uintptr_t)p };
	uint64_t reclaim_generation = 0;
	if (!transfer(output, &r, sizeof(r), true)) _exit(14);
	char op;
	while (transfer(input, &op, 1, false)) {
		r.result = 0; r.migrated = 0;
		switch (op) {
		case 'W': store_page(p + 5 * PAGE, marker(5)); break;
		case 'H':
			for (unsigned int i = 1; i <= 4; i++) store_page(p + i * PAGE, marker(i));
			break;
		case 'S':
			atomic_store(&w.run, true);
			while (atomic_load(&w.active) != 4) usleep(100);
			break;
		case 'J':
			atomic_store(&w.churn, true);
			while (!atomic_load(&w.churning)) usleep(100);
			break;
		case 'T': stop_workers(&w); break;
		case 'P':
			r.result = mprotect(p + 6 * PAGE, PAGE, PROT_READ) ||
				mprotect(p + 6 * PAGE, PAGE, PROT_READ | PROT_WRITE);
			break;
		case 'M': {
			unsigned char *q = mmap(p + 7 * PAGE, PAGE, PROT_READ | PROT_WRITE,
				MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
			if (q != p + 7 * PAGE) r.result = 1;
			else store_page(q, marker(7));
			break;
		}
		case 'N': {
			void *page = p + 12 * PAGE;
			int node = 1, status = -1;
			long rc = syscall(SYS_move_pages, 0, 1, &page, &node, &status, 2);
			r.result = rc < 0 || status < 0;
			r.migrated = !r.result && status == 1;
			break;
		}
		case 'B':
			reclaim_generation ^= UINT64_C(0xfeedbabe);
			for (unsigned int i = RECLAIM_FIRST; i < PAGES; i++)
				store_reclaim_page(p + i * PAGE, i, reclaim_generation);
			break;
		case 'R':
			r.result = madvise(p + RECLAIM_FIRST * PAGE, RECLAIM_PAGES * PAGE, MADV_COLD) ||
				madvise(p + RECLAIM_FIRST * PAGE, RECLAIM_PAGES * PAGE, MADV_PAGEOUT);
			break;
		case 'V':
			stop_workers(&w);
			for (unsigned int i = 0; i < PAGES; i++) {
				bool valid = i < RECLAIM_FIRST ? verify_page(p + i * PAGE, marker(i)) :
					verify_reclaim_page(p + i * PAGE, i, reclaim_generation);
				if (!valid) r.result = 1;
			}
			break;
		case 'X':
			stop_workers(&w); atomic_store(&w.quit, true);
			for (int i = 0; i < 5; i++) pthread_join(threads[i], NULL);
			_exit(0);
		default: _exit(15);
		}
		r.iterations = atomic_load(&w.iterations);
		if (!transfer(output, &r, sizeof(r), true)) _exit(16);
	}
	_exit(0);
}
static bool command_reply(char op, struct reply *r)
{
	return transfer(command_fd, &op, 1, true) &&
		transfer(response_fd, r, sizeof(*r), false) && !r->result;
}
static bool command(char op) { struct reply r; return command_reply(op, &r); }
static bool status(struct cmdp_monitor_status *s)
{
	INIT(*s); return !ioctl(fd, CMDP_MON_STATUS, s);
}
static bool query(unsigned int index, struct cmdp_monitor_page *p)
{
	INIT(*p); p->addr = base + index * PAGE;
	return !ioctl(fd, CMDP_MON_PAGE, p);
}
static bool scan(void) { return !ioctl(fd, CMDP_MON_SCAN_ONCE); }
static bool pagemap(unsigned int index, uint64_t *entry)
{
	char path[64];
	snprintf(path, sizeof(path), "/proc/%d/pagemap", child_pid);
	int f = open(path, O_RDONLY | O_CLOEXEC);
	if (f < 0) return false;
	ssize_t n = pread(f, entry, sizeof(*entry), ((base / PAGE) + index) * sizeof(*entry));
	close(f); return n == sizeof(*entry);
}
static bool linux_dirty_and_softdirty(unsigned int index)
{
	uint64_t entry, flags = 0;
	if (!pagemap(index, &entry) || !(entry & (UINT64_C(1) << 63)) ||
	    !(entry & (UINT64_C(1) << 55))) return false;
	uint64_t pfn = entry & ((UINT64_C(1) << 55) - 1);
	int f = open("/proc/kpageflags", O_RDONLY | O_CLOEXEC);
	if (f < 0 || !pfn) { if (f >= 0) close(f); return false; }
	ssize_t n = pread(f, &flags, sizeof(flags), pfn * sizeof(flags));
	close(f);
	return n == sizeof(flags) && (flags & (UINT64_C(1) << 4)); /* KPF_DIRTY */
}
static bool dirty_status(struct cmdp_monitor_dirty_status *s)
{
	INIT(*s); return !ioctl(fd, CMDP_MON_DIRTY_STATUS, s);
}
static bool dirty_page(unsigned int index, struct cmdp_monitor_dirty_page *p)
{
	INIT(*p); p->addr = base + index * PAGE;
	return !ioctl(fd, CMDP_MON_DIRTY_PAGE, p);
}
static bool attach(unsigned int mode, unsigned int history)
{
	struct cmdp_monitor_attach a;
	struct cmdp_monitor_dirty_config c;
	fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
	if (fd < 0) return false;
	INIT(a); a.pidfd = pidfd; a.period_ms = 10;
	a.max_pages = 32768; a.max_history = history;
	INIT(c); c.mode = mode; c.quiet_ms = Q_MS;
	return !ioctl(fd, CMDP_MON_ATTACH, &a) && !ioctl(fd, CMDP_MON_DIRTY_CONFIG, &c);
}
static bool armed(unsigned int index)
{
	struct cmdp_monitor_page p;
	return query(index, &p) && p.state == CMDP_MON_PAGE_ARMED;
}
static bool qualified(unsigned int index)
{
	struct cmdp_monitor_dirty_page d;
	return armed(index) && dirty_page(index, &d) && !d.valid && d.quiet_since_ns &&
		d.qualified_ns >= d.quiet_since_ns + (uint64_t)Q_MS * 1000000;
}
static void quiet_wait(void) { usleep((Q_MS + 30) * 1000); }
static bool progress(struct cmdp_monitor_progress *p)
{
	INIT(*p); return !ioctl(fd, CMDP_MON_PROGRESS, p);
}
static void *hold_mmap(void *opaque)
{
	struct cmdp_monitor_test_hold h;
	INIT(h); h.milliseconds = 2000;
	*(int *)opaque = ioctl(fd, CMDP_MON_TEST_HOLD_MMAP, &h);
	return NULL;
}
static void *stop_monitor(void *opaque)
{
	*(int *)opaque = ioctl(fd, CMDP_MON_STOP); return NULL;
}
static bool close_session(void)
{
	struct cmdp_monitor_status s;
	bool ok = !ioctl(fd, CMDP_MON_STOP) && status(&s) &&
		s.state == CMDP_MON_STOPPED && !s.active;
	close(fd); fd = -1; return ok;
}
static bool balanced(void)
{
	FILE *f = fopen("/sys/kernel/debug/cmdp/stats", "r");
	if (!f) return false;
	char key[64]; unsigned long long value; unsigned int seen = 0;
	bool ok = true;
	while (fscanf(f, "%63s %llu", key, &value) == 2) {
		if (!strcmp(key, "entries") || !strcmp(key, "managed") || !strcmp(key, "isolated")) {
			seen++; ok &= value == 0;
		}
	}
	fclose(f); return ok && seen == 3;
}
#define CHECK(ok, text) do { checks++; errno = 0; if (!(ok)) { \
	printf("not ok %d - %s (errno=%d %s)\n", checks, text, errno, strerror(errno)); \
	goto fail; } printf("ok %d - %s\n", checks, text); } while (0)

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	puts("TAP version 13");
	if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE || access(CMDP_MONITOR_DEVICE, R_OK | W_OK)) {
		puts("1..0 # SKIP requires root, 4 KiB pages, /dev/cmdp_monitor"); return 4;
	}
	alarm(180);
	int c[2], r[2];
	CHECK(!pipe2(c, O_CLOEXEC) && !pipe2(r, O_CLOEXEC), "fixture channels");
	child_pid = fork();
	if (!child_pid) { close(c[1]); close(r[0]); target(c[0], r[1]); }
	CHECK(child_pid > 0, "fork fixture before monitor attach");
	close(c[0]); close(r[1]); command_fd = c[1]; response_fd = r[0];
	struct reply reply;
	CHECK(transfer(response_fd, &reply, sizeof(reply), false), "resident pages and four writer threads ready");
	base = reply.addr;
	pidfd = syscall(SYS_pidfd_open, child_pid, 0);
	CHECK(pidfd >= 0 && attach(CMDP_MON_DIRTY_OBSERVE, 32768), "attach sampling-only policy");
	struct cmdp_monitor_status s;
	struct cmdp_monitor_dirty_status ds, ds_before;
	struct cmdp_monitor_dirty_page d0, d1;
	struct cmdp_monitor_page p0, p1;
	struct cmdp_monitor_dirty_config invalid;
	INIT(invalid); invalid.mode = UINT32_MAX; invalid.quiet_ms = Q_MS;
	errno = 0;
	CHECK(ioctl(fd, CMDP_MON_DIRTY_CONFIG, &invalid) < 0 && errno == EINVAL,
	      "reject unknown dirty policy mode");
	invalid.mode = CMDP_MON_DIRTY_OBSERVE; invalid.reserved[0] = 1; errno = 0;
	CHECK(ioctl(fd, CMDP_MON_DIRTY_CONFIG, &invalid) < 0 && errno == EINVAL,
	      "reject nonzero configuration reserved fields");
	invalid.reserved[0] = 0; invalid.quiet_ms = 1; errno = 0;
	CHECK(ioctl(fd, CMDP_MON_DIRTY_CONFIG, &invalid) < 0 && errno == EINVAL,
	      "reject quiet thresholds below ten milliseconds");
	uint64_t initial_pagemap;
	CHECK(pagemap(0, &initial_pagemap) && (initial_pagemap & (UINT64_C(1) << 55)),
	      "fixture initially has the soft-dirty PTE flag set");
	uint64_t begin = now_ns();
	CHECK(scan() && dirty_page(0, &d0) && d0.valid && d0.quiet_since_ns >= begin,
	      "first observation establishes a new synchronized epoch");
	CHECK(status(&s) && !s.first_arms && !s.rearms && !s.active,
	      "sampling-only first scan creates no ARM generations");
	CHECK(linux_dirty_and_softdirty(0), "dirty reset preserves Linux folio dirty accounting and PTE soft-dirty");
	quiet_wait();
	CHECK(scan() && dirty_page(0, &d1) && d1.valid && !d1.last_dirty &&
	      d1.quiet_since_ns == d0.quiet_since_ns && d1.epoch_id == d0.epoch_id,
	      "clean sampling preserves its original observation boundary");
	CHECK(command('H') && scan() && dirty_page(1, &d0) && d0.last_dirty,
	      "a write between samples is observed even when bytes keep the same value");
	CHECK(dirty_status(&ds) && ds.dirty && ds.clean && ds.flushes && ds.resets &&
	      ds.samples && ds.valid_observations && ds.metadata_bytes,
	      "dirty counters report observation, reset/flush work and metadata");
	CHECK(dirty_page(6, &d0) && d0.valid && command('P') && dirty_page(6, &d1) && !d1.valid,
	      "mprotect invalidates OFF history that has never been ARMED");
	CHECK(scan() && dirty_page(6, &d1) && d1.valid && d1.epoch_id > d0.epoch_id,
	      "permission reuse starts a fresh observation epoch");
	CHECK(dirty_page(7, &d0) && d0.valid && command('M') && dirty_page(7, &d1) && !d1.valid &&
	      scan() && dirty_page(7, &d1) && d1.valid && d1.epoch_id > d0.epoch_id,
	      "MAP_FIXED address reuse discards clean evidence regardless of PFN reuse");
	CHECK(dirty_page(12, &d0) && d0.valid && command_reply('N', &reply) && reply.migrated &&
	      dirty_page(12, &d1) && !d1.valid && scan() && dirty_page(12, &d1) &&
	      d1.valid && d1.epoch_id > d0.epoch_id && d1.pfn != d0.pfn,
	      "explicit NUMA migration invalidates OFF history and changes backing page");
	CHECK(command('B') && scan() && linux_dirty_and_softdirty(RECLAIM_FIRST),
	      "fresh pattern survives hardware dirty harvest and remains Linux-dirty");
	CHECK(dirty_page(RECLAIM_FIRST, &d0) && d0.valid && command('R'),
	      "request actual pageout of 8 MiB after synchronized dirty harvesting");
	unsigned int swapped_index = 0, swapped_pages = 0;
	for (unsigned int retry = 0; retry < 100 && !swapped_pages; retry++) {
		for (unsigned int i = RECLAIM_FIRST; i < PAGES; i++) {
			uint64_t entry;
			if (pagemap(i, &entry) && (entry & (UINT64_C(1) << 62))) {
				if (!swapped_pages) swapped_index = i;
				swapped_pages++;
			}
		}
		if (!swapped_pages) usleep(10000);
	}
	CHECK(swapped_pages && dirty_page(swapped_index, &d1) && !d1.valid,
	      "pagemap proves real swapout and swapped-page quiet evidence is invalidated");
	printf("# swapped anonymous pages=%u first_index=%u\n", swapped_pages, swapped_index);
	uint64_t swapped_epoch = d1.epoch_id;
	CHECK(scan() && command('V') && scan() && dirty_page(swapped_index, &d1) &&
	      (!d1.valid || d1.epoch_id > swapped_epoch),
	      "swap-in restores patterned data without reusing old quiet evidence (swapcache may be excluded)");
	CHECK(command('S') && command('J'), "start four writers and concurrent permission/reclaim churn");
	for (int i = 0; i < 20; i++) CHECK(scan(), "sampling completes during concurrent writers and invalidations");
	CHECK(command_reply('T', &reply) && reply.iterations > 1000 && command('V'),
	      "four writers racing reset/TLB flush retain every page marker");
	printf("# concurrent writer iterations=%" PRIu64 "\n", reply.iterations);
	CHECK(status(&s) && !s.first_arms && !s.rearms && !s.active && !s.terminals[CMDP_MON_WRITE],
	      "sampling-only mode caused no ARM or CMD-P write revocation");
	CHECK(close_session() && balanced(), "sampling-only STOP leaves zero page ownership");

	CHECK(attach(CMDP_MON_DIRTY_QUALIFY, 32768) && scan(), "attach quiet-qualified policy and prime all candidates");
	CHECK(dirty_page(0, &d0) && d0.valid && !armed(0),
	      "a newly observed clean page cannot qualify on its first visit");
	struct cmdp_monitor_dirty_config config;
	INIT(config); config.mode = CMDP_MON_DIRTY_OBSERVE; config.quiet_ms = Q_MS;
	errno = 0;
	CHECK(ioctl(fd, CMDP_MON_DIRTY_CONFIG, &config) < 0 && errno == EBUSY,
	      "policy cannot change after sampling has started");
	quiet_wait();
	CHECK(command('W') && scan() && dirty_page(5, &d1) && d1.valid && d1.last_dirty && !armed(5),
	      "dirty candidate is reset rather than admitted after nominal quiet time");
	CHECK(qualified(0) && query(0, &p0), "cold page qualifies only after actual synchronized quiet duration");
	uint64_t reset_ns = d1.quiet_since_ns;
	quiet_wait();
	CHECK(scan() && qualified(5) && dirty_page(5, &d1) && d1.quiet_since_ns == reset_ns,
	      "write-once page qualifies after its fresh quiet interval");
	CHECK(query(0, &p1) && p1.arm_generation == p0.arm_generation && p1.armed_ns == p0.armed_ns,
	      "successful ARM survivor remains protected across later sampling passes");
	CHECK(command('W') && query(5, &p1) && p1.state == CMDP_MON_PAGE_OFF_WRITE &&
	      dirty_page(5, &d1) && !d1.valid,
	      "WP returns the page to OFF with no inherited quiet certificate");
	CHECK(scan() && dirty_page(5, &d0) && d0.valid && !armed(5),
	      "first visit following WP primes observation without immediate re-ARM");
	quiet_wait();
	CHECK(scan() && qualified(5) && query(5, &p1) && p1.arm_count == 2,
	      "WP page re-ARMs only after a fresh full quiet interval");
	CHECK(dirty_status(&ds) && ds.wp_resets && ds.qualified && status(&s) && s.rearms,
	      "qualified admissions, WP resets and re-ARMs are reported");
	CHECK(close_session() && command('V') && balanced(), "qualified STOP drains ownership and preserves all data");

	CHECK(attach(CMDP_MON_DIRTY_QUALIFY, 32768) && scan(), "prime separate hot-page qualification test");
	CHECK(dirty_page(1, &d0), "record hot-page observation");
	for (int i = 0; i < 4; i++) {
		quiet_wait();
		CHECK(command('H') && scan() && dirty_page(1, &d1) && d1.valid && d1.last_dirty &&
		      !armed(1) && d1.epoch_id > d0.epoch_id,
		      "a write in every completed interval keeps the hot page unprotected");
		d0 = d1;
	}
	CHECK(dirty_status(&ds_before) && command('S') && command('J'),
	      "start concurrent reset/invalidation races with admission enabled");
	for (int i = 0; i < 20; i++) CHECK(scan(), "qualified scan completes under four-thread write and mapping races");
	CHECK(command('T') && command('V') && dirty_status(&ds) && ds.invalidations > ds_before.invalidations,
	      "racing invalidations discard history while application data stays intact");
	CHECK(close_session() && balanced(), "racing qualification STOP drains every owned page");

	CHECK(attach(CMDP_MON_DIRTY_QUALIFY, 1) && scan() && status(&s) && dirty_status(&ds) &&
	      s.history_entries == 1 && ds.resource_skips && !s.first_arms,
	      "history exhaustion is reported and unsampled pages never inherit clean evidence");
	CHECK(close_session() && balanced(), "resource-limited session cleans up");

	CHECK(attach(CMDP_MON_DIRTY_QUALIFY, 32768) && scan(), "prime sampling cancellation fixture");
	pthread_t holder, stopper; int hold_rc = -1, stop_rc = -1;
	struct cmdp_monitor_progress progress_now;
	CHECK(!pthread_create(&holder, NULL, hold_mmap, &hold_rc), "start bounded mmap read-lock holder");
	bool held = false;
	for (int i = 0; i < 1000; i++) {
		if (progress(&progress_now) && progress_now.test_mmap_readers) { held = true; break; }
		usleep(1000);
	}
	CHECK(held && !ioctl(fd, CMDP_MON_START), "start dirty sampling while reader holds target mmap lock");
	bool waiting = false;
	for (int i = 0; i < 1000; i++) {
		if (progress(&progress_now) && progress_now.scan_phase == CMDP_MON_SCAN_WAIT_MMAP &&
		    progress_now.scan_lock_retries) { waiting = true; break; }
		usleep(1000);
	}
	CHECK(waiting && !pthread_create(&stopper, NULL, stop_monitor, &stop_rc),
	      "request STOP after confirming the sampler waits for mmap access");
	bool cancelled = false;
	for (int i = 0; i < 500; i++) {
		if (progress(&progress_now) && progress_now.stop_request_ns &&
		    progress_now.scan_phase == CMDP_MON_SCAN_IDLE && progress_now.test_mmap_readers) {
			cancelled = true; break;
		}
		usleep(1000);
	}
	CHECK(cancelled, "sampler cancels before the outstanding mmap reader releases");
	CHECK(!pthread_join(holder, NULL) && !hold_rc && !pthread_join(stopper, NULL) && !stop_rc &&
	      close_session() && command('V') && balanced(), "STOP returns with data intact and no leaked ownership");

	char op = 'X'; int child_status;
	CHECK(transfer(command_fd, &op, 1, true) && waitpid(child_pid, &child_status, 0) == child_pid &&
	      WIFEXITED(child_status) && !WEXITSTATUS(child_status), "target threads exit cleanly");
	child_pid = -1;
	close(pidfd); close(command_fd); close(response_fd);
	printf("1..%d\n", checks); return 0;
fail:
	if (fd >= 0) { ioctl(fd, CMDP_MON_STOP); close(fd); }
	if (child_pid > 0) { kill(child_pid, SIGKILL); waitpid(child_pid, NULL, 0); }
	printf("1..%d\n", checks); return 1;
}
