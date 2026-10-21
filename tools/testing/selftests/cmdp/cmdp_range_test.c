// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <setjmp.h>
#include <signal.h>
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
#if defined(__x86_64__)
#define SYS_pidfd_open 434
#else
#error "pidfd_open syscall number unavailable"
#endif
#endif

#define PAGE 4096UL
#define INIT(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
	(x).version = CMDP_MONITOR_VERSION; } while (0)
static unsigned long prefix = 16384, victims = 1024;
static unsigned int numa_seconds;
static bool head, empty, stop_wait;
static int monitor_fd = -1, command_fd = -1, response_fd = -1, pid_fd = -1;
static pid_t child_pid = -1;
static uintptr_t base;
static int checks;
struct reply { uint64_t base, elapsed_ns, checksum; int result; };
static sigjmp_buf expected_fault;
static volatile sig_atomic_t expect_segv;

static unsigned char page_mark(unsigned long i)
{
	return (unsigned char)(0x42 ^ i ^ (i >> 8) ^ (i >> 16));
}
static bool page_matches(volatile unsigned char *p, unsigned char mark)
{
	unsigned char inverse = mark ^ 0xff;
	return p[0] == mark && p[PAGE - 1] == inverse;
}
static void page_store(volatile unsigned char *p, unsigned char mark)
{
	p[0] = mark; p[PAGE - 1] = mark ^ 0xff;
}
static void segv_handler(int sig)
{
	if (expect_segv) siglongjmp(expected_fault, 1);
	_exit(128 + sig);
}
static bool readonly_faults(volatile unsigned char *p)
{
	expect_segv = 1;
	if (!sigsetjmp(expected_fault, 1)) {
		*p = 0x11;
		expect_segv = 0;
		return false;
	}
	expect_segv = 0;
	return true;
}

static uint64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static bool transfer(int fd, void *data, size_t n, bool output)
{
	char *p = data;
	while (n) {
		ssize_t r = output ? write(fd, p, n) : read(fd, p, n);
		if (r < 0 && errno == EINTR) continue;
		if (r <= 0) return false;
		p += r; n -= r;
	}
	return true;
}
static bool pin_cpu(int cpu)
{
	cpu_set_t mask;
	CPU_ZERO(&mask); CPU_SET(cpu, &mask);
	return !sched_setaffinity(0, sizeof(mask), &mask);
}
static void target(int input, int output)
{
	if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0) ||
	    (numa_seconds && !pin_cpu(0))) _exit(10);
	struct sigaction sa = { .sa_handler = segv_handler };
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGSEGV, &sa, NULL)) _exit(16);
	size_t bytes = (prefix + victims) * PAGE;
	unsigned char *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) _exit(11);
	/* One allocation guarantees the unaffected prefix precedes the victim. */
	for (unsigned long i = 0; i < prefix + (empty ? 0 : victims); i++)
		page_store(p + i * PAGE, page_mark(i));
	struct reply r = { .base = (uintptr_t)p };
	if (!transfer(output, &r, sizeof(r), true)) _exit(12);
	char op;
	bool protected = false;
	while (transfer(input, &op, 1, false)) {
		uint64_t started = now_ns();
		r.result = 0;
		if (op == 'P') {
			r.result = mprotect(p + (head ? 0 : prefix * PAGE),
					    victims * PAGE, PROT_READ);
			r.elapsed_ns = now_ns() - started;
			if (!r.result) {
				protected = true;
				for (unsigned long i = 0; i < prefix + (empty ? 0 : victims); i++)
					if (!page_matches(p + i * PAGE, page_mark(i))) r.result = 1;
				unsigned char *first = p + (head ? 0 : prefix * PAGE);
				if (!readonly_faults(first) || !readonly_faults(first + (victims - 1) * PAGE))
					r.result = 1;
			}
		} else if (op == 'N') {
			if (!pin_cpu(2)) _exit(13);
			uint64_t end = now_ns() + (uint64_t)numa_seconds * 1000000000;
			/* CPU 0 first-touch, CPU 2 readers: different guest NUMA nodes.
			 * No mapping changes, fork, or application data stores here.
			 */
			uint64_t sum = 0;
			do {
				uint64_t pass_sum = 0;
				for (unsigned long i = 0; i < prefix + victims; i++) {
					volatile unsigned char *page = p + i * PAGE;
					unsigned char first = page[0], last = page[PAGE - 1];
					unsigned char mark = page_mark(i);
					unsigned char inverse = mark ^ 0xff;
					if (first != mark || last != inverse) r.result = 1;
					pass_sum += first + last;
				}
				if (pass_sum != (uint64_t)(prefix + victims) * 255) r.result = 1;
				sum += pass_sum;
			} while (!r.result && now_ns() < end);
			r.checksum = sum;
		} else if (op == 'V') {
			/* Check data before changing PTE permissions, then exercise the
			 * untouched writable VMAs' lazy post-STOP write-fault path.
			 */
			for (unsigned long i = 0; i < prefix + victims; i++) {
				volatile unsigned char *page = p + i * PAGE;
				if (empty && i >= prefix) {
					if (page[0] || page[PAGE - 1]) r.result = 1;
				} else if (!page_matches(page, page_mark(i))) r.result = 1;
			}
			for (unsigned long i = 0; i < prefix + victims; i++) {
				bool victim = head ? i < victims : i >= prefix;
				if (!protected || !victim) page_store(p + i * PAGE, page_mark(i) ^ 0x5a);
			}
			if (protected) {
				if (mprotect(p + (head ? 0 : prefix * PAGE), victims * PAGE, PROT_READ | PROT_WRITE))
					r.result = 1;
				else for (unsigned long i = 0; i < victims; i++) {
					unsigned long page = (head ? 0 : prefix) + i;
					page_store(p + page * PAGE, page_mark(page) ^ 0x5a);
				}
			}
			for (unsigned long i = 0; i < prefix + victims; i++)
				if (!page_matches(p + i * PAGE, page_mark(i) ^ 0x5a)) r.result = 1;
		} else if (op == 'X') {
			_exit(0);
		} else _exit(14);
		if (op != 'P') r.elapsed_ns = now_ns() - started;
		if (!transfer(output, &r, sizeof(r), true)) _exit(15);
	}
	_exit(0);
}
static bool status(struct cmdp_monitor_status *s)
{
	INIT(*s); return !ioctl(monitor_fd, CMDP_MON_STATUS, s);
}
static bool progress(struct cmdp_monitor_progress *p)
{
	INIT(*p); return !ioctl(monitor_fd, CMDP_MON_PROGRESS, p);
}
static bool page_state(uintptr_t addr, unsigned int wanted)
{
	struct cmdp_monitor_page p;
	INIT(p); p.addr = addr;
	return !ioctl(monitor_fd, CMDP_MON_PAGE, &p) && p.state == wanted;
}
static bool conserved(const struct cmdp_monitor_status *s)
{
	uint64_t terminals = 0;
	for (int i = 0; i < CMDP_MON_NR_REASONS; i++) terminals += s->terminals[i];
	return s->first_arms + s->rearms == s->active + terminals;
}
static bool balanced(void)
{
	FILE *f = fopen("/sys/kernel/debug/cmdp/stats", "r");
	char key[64]; unsigned long long value; int seen = 0; bool ok = true;
	if (!f) return false;
	while (fscanf(f, "%63s %llu", key, &value) == 2) {
		if (!strcmp(key, "entries") || !strcmp(key, "managed") || !strcmp(key, "isolated")) {
			seen++; ok &= value == 0;
		}
	}
	fclose(f); return ok && seen == 3;
}
static bool write_text(const char *path, const char *value, bool append)
{
	int fd = open(path, O_WRONLY | (append ? O_APPEND : O_TRUNC));
	if (fd < 0) return false;
	size_t len = strlen(value);
	bool ok = transfer(fd, (void *)value, len, true);
	close(fd); return ok;
}
static const char *trace_root = "/sys/kernel/debug/tracing";
static bool trace_write(const char *name, const char *value, bool append)
{
	char path[256];
	snprintf(path, sizeof(path), "%s/%s", trace_root, name);
	return write_text(path, value, append);
}
static bool trace_start(void)
{
	char filter[80];
	snprintf(filter, sizeof(filter), "common_pid == %ld\n", (long)child_pid);
	return trace_write("tracing_on", "0\n", false) &&
		/* Per-page migration notifications are numerous. Reserve enough
		 * guest-only buffer, and still reject any recorded loss below.
		 */
		trace_write("buffer_size_kb", "65536\n", false) &&
		trace_write("trace_clock", "mono\n", false) &&
		trace_write("trace", "\n", false) &&
		trace_write("kprobe_events", "p:cmdp_range_numa change_prot_numa\n", true) &&
		trace_write("kprobe_events", "r:cmdp_range_numa_return change_prot_numa\n", true) &&
		trace_write("kprobe_events", "p:cmdp_range_invalidate cmdp_invalidate\n", true) &&
		trace_write("events/kprobes/filter", filter, false) &&
		trace_write("events/kprobes/enable", "1\n", false) &&
		trace_write("tracing_on", "1\n", false);
}
static bool trace_finish(void)
{
	char path[256], line[2048]; int depth = 0;
	unsigned long entries = 0, overlaps = 0, returns = 0, invalidations = 0;
	unsigned long completed_nested = 0, current_nested = 0, printed = 0, samples = 0;
	unsigned long long recorded = 0, written = 0;
	bool malformed = false, header = false, losses = false;
	if (!trace_write("tracing_on", "0\n", false)) return false;
	/* This runs only after SIGSTOP + waitpid confirms target quiescence. */
	for (long cpu = 0; cpu < sysconf(_SC_NPROCESSORS_CONF); cpu++) {
		snprintf(path, sizeof(path), "%s/per_cpu/cpu%ld/stats", trace_root, cpu);
		FILE *stats = fopen(path, "r");
		if (!stats) return false;
		unsigned long long lost = 0, value; unsigned int seen = 0;
		while (fgets(line, sizeof(line), stats)) {
			if (sscanf(line, "overrun: %llu", &value) == 1 ||
			    sscanf(line, "commit overrun: %llu", &value) == 1 ||
			    sscanf(line, "dropped events: %llu", &value) == 1) {
				lost += value; seen++;
			}
		}
		fclose(stats);
		printf("# numa_trace_cpu cpu=%ld lost=%llu counters=%u\n", cpu, lost, seen);
		if (lost || seen != 3) losses = true;
	}
	snprintf(path, sizeof(path), "%s/kprobe_profile", trace_root);
	FILE *profile = fopen(path, "r");
	if (!profile) return false;
	unsigned int profiles = 0;
	while (fgets(line, sizeof(line), profile)) {
		char name[128]; unsigned long long hits, missed;
		if (sscanf(line, "%127s %llu %llu", name, &hits, &missed) == 3 &&
		    strstr(name, "cmdp_range_")) {
			profiles++;
			printf("# numa_kprobe name=%s hits=%llu missed=%llu\n", name, hits, missed);
			if (missed) losses = true;
		}
	}
	fclose(profile);
	if (profiles != 3) losses = true;
	snprintf(path, sizeof(path), "%s/trace", trace_root);
	FILE *f = fopen(path, "r");
	if (!f) return false;
	puts("CMDP_NUMA_TRACE_BEGIN");
	while (fgets(line, sizeof(line), f)) {
		bool show = false;
		if (sscanf(line, "# entries-in-buffer/entries-written: %llu/%llu", &recorded, &written) == 2) {
			header = true;
			printf("# numa_trace_buffer recorded=%llu written=%llu\n", recorded, written);
		}
		if (strstr(line, "cmdp_range_numa_return:")) {
			returns++; show = true;
			if (depth != 1) malformed = true;
			if (depth == 1 && current_nested) completed_nested++;
			if (depth) depth--;
		} else if (strstr(line, "cmdp_range_numa:")) {
			entries++; show = true;
			if (depth) malformed = true;
			depth++; current_nested = 0;
		} else if (strstr(line, "cmdp_range_invalidate:")) {
			invalidations++;
			if (depth == 1) {
				overlaps++; current_nested++;
				if (samples < 8) { show = true; samples++; }
			}
		}
		if (show && printed < 48) { fputs(line, stdout); printed++; }
	}
	bool read_error = ferror(f);
	fclose(f);
	puts("CMDP_NUMA_TRACE_END");
	printf("# numa_trace entries=%lu returns=%lu invalidations=%lu nested_invalidations=%lu "
	       "completed_nested_calls=%lu unmatched_depth=%d malformed=%u lost=%u printed=%lu\n",
	       entries, returns, invalidations, overlaps, completed_nested, depth,
	       malformed, losses, printed);
	bool disabled = trace_write("events/kprobes/enable", "0\n", false);
	return disabled && !read_error && header && recorded == written &&
		recorded == (unsigned long long)entries + returns + invalidations && !losses && !malformed &&
		entries && entries == returns && !depth && completed_nested;
}
static void *hold_mmap(void *arg)
{
	struct cmdp_monitor_test_hold h;
	INIT(h); h.milliseconds = 2000;
	*(int *)arg = ioctl(monitor_fd, CMDP_MON_TEST_HOLD_MMAP, &h);
	return NULL;
}
static void *stop_monitor(void *arg)
{
	*(int *)arg = ioctl(monitor_fd, CMDP_MON_STOP); return NULL;
}
#define CHECK(ok, text) do { checks++; if (!(ok)) { \
	printf("not ok %d - %s (errno=%d %s)\n", checks, text, errno, strerror(errno)); \
	goto fail; } printf("ok %d - %s\n", checks, text); } while (0)

int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--prefix-pages") && i + 1 < argc) prefix = strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--victim-pages") && i + 1 < argc) victims = strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--numa-seconds") && i + 1 < argc) numa_seconds = strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--head")) head = true;
		else if (!strcmp(argv[i], "--empty")) empty = true;
		else if (!strcmp(argv[i], "--stop-wait")) stop_wait = true;
		else return 2;
	}
	if (!victims || prefix > 2200000 || victims > 2200000 ||
	    prefix + victims > 2200000 || (head && empty) ||
	    (numa_seconds && (head || empty || stop_wait)) || numa_seconds > 120) return 2;
	setvbuf(stdout, NULL, _IOLBF, 0);
	puts("TAP version 13");
	if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE || access(CMDP_MONITOR_DEVICE, R_OK | W_OK)) {
		puts("1..0 # SKIP requires root, 4 KiB pages, /dev/cmdp_monitor"); return 4;
	}
	/* This process alarm is a convenience; a host QEMU watchdog is mandatory. */
	alarm(300);
	int c[2], r[2];
	CHECK(!pipe2(c, O_CLOEXEC) && !pipe2(r, O_CLOEXEC), "fixture channels");
	child_pid = fork();
	if (!child_pid) { close(c[1]); close(r[0]); target(c[0], r[1]); }
	CHECK(child_pid > 0, "fork fixture before monitoring");
	close(c[0]); close(r[1]); command_fd = c[1]; response_fd = r[0];
	struct reply ready;
	CHECK(transfer(response_fd, &ready, sizeof(ready), false), "resident contiguous fixture ready");
	base = ready.base;
	pid_fd = syscall(SYS_pidfd_open, child_pid, 0);
	monitor_fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
	CHECK(pid_fd >= 0 && monitor_fd >= 0, "monitor and pidfd open");
	struct cmdp_monitor_attach a;
	INIT(a); a.pidfd = pid_fd; a.period_ms = 1000;
	a.max_pages = prefix + victims + 65536; a.max_history = a.max_pages;
	CHECK(!ioctl(monitor_fd, CMDP_MON_ATTACH, &a), "attach exact target");
	CHECK(!ioctl(monitor_fd, CMDP_MON_SCAN_ONCE), "initial sweep completes");
	struct cmdp_monitor_status before, after;
	struct cmdp_monitor_progress p0, p1;
	CHECK(status(&before) && progress(&p0) && conserved(&before) &&
	      !before.resource_failures, "initial accounting and progress available");
	bool armed = true;
	for (unsigned long i = 0; i < prefix + (empty ? 0 : victims); i++)
		if (!page_state(base + i * PAGE, CMDP_MON_PAGE_ARMED)) { armed = false; break; }
	CHECK(armed, "every populated fixture page is armed before the operation");
	if (stop_wait) {
		pthread_t holder, stopper; int hold_rc = -1, stop_rc = -1;
		CHECK(!pthread_create(&holder, NULL, hold_mmap, &hold_rc), "bounded test reader starts");
		bool held = false;
		for (int i = 0; i < 1000; i++) {
			if (progress(&p1) && p1.test_mmap_readers) { held = true; break; }
			usleep(1000);
		}
		CHECK(held, "test hook confirms target mmap read lock held");
		CHECK(!ioctl(monitor_fd, CMDP_MON_START), "schedule sweep behind mmap reader");
		bool waiting = false;
		for (int i = 0; i < 1000; i++) {
			if (progress(&p1) && p1.scan_phase == CMDP_MON_SCAN_WAIT_MMAP && p1.scan_lock_retries) {
				waiting = true; break;
			}
			usleep(1000);
		}
		CHECK(waiting && p1.test_mmap_readers, "scanner demonstrably waits for mmap writer access");
		CHECK(!pthread_create(&stopper, NULL, stop_monitor, &stop_rc), "request STOP while scanner waits");
		bool cancelled = false;
		for (int i = 0; i < 500; i++) {
			if (progress(&p1) && p1.stop_request_ns && p1.scan_phase == CMDP_MON_SCAN_IDLE &&
			    p1.test_mmap_readers) { cancelled = true; break; }
			usleep(1000);
		}
		CHECK(cancelled, "scanner cancellation completes while reader still holds mmap lock");
		CHECK(!pthread_join(holder, NULL) && !hold_rc &&
		      !pthread_join(stopper, NULL) && !stop_rc, "STOP drains after finite reader releases");
		CHECK(progress(&p1) && p1.stop_complete_ns >= p1.stop_drain_ns &&
		      p1.stop_drain_ns >= p1.stop_request_ns && p1.stop_request_active,
		      "STOP request, drain, completion timestamps and request population recorded");
		printf("# stop_wait request_to_drain_ns=%" PRIu64 " request_to_complete_ns=%" PRIu64
		       " scan_wait_total_ns=%" PRIu64 " retries=%" PRIu64 "\n",
		       (uint64_t)(p1.stop_drain_ns - p1.stop_request_ns),
		       (uint64_t)(p1.stop_complete_ns - p1.stop_request_ns),
		       (uint64_t)p1.scan_lock_wait_total_ns, (uint64_t)p1.scan_lock_retries);
	} else if (numa_seconds) {
		CHECK(trace_start(), "target-filtered NUMA and notifier kprobes enabled");
		CHECK(!ioctl(monitor_fd, CMDP_MON_START), "periodic rearming enabled for NUMA workload");
		char op = 'N'; struct reply result;
		CHECK(transfer(command_fd, &op, 1, true) && transfer(response_fd, &result, sizeof(result), false)
		      && !result.result && result.checksum &&
		      result.elapsed_ns >= (uint64_t)numa_seconds * 1000000000,
		      "remote-node read loop preserves every page marker for the full requested interval");
		int stopped_status;
		CHECK(!kill(child_pid, SIGSTOP) && waitpid(child_pid, &stopped_status, WUNTRACED) == child_pid &&
		      WIFSTOPPED(stopped_status), "target quiesced before stopping NUMA trace");
		CHECK(status(&after) && progress(&p1) && conserved(&after), "NUMA accounting captured before STOP");
		CHECK(trace_finish(), "lossless balanced NUMA trace contains a completed nested CMD-P invalidation");
		CHECK(after.terminals[CMDP_MON_INVALIDATE] > before.terminals[CMDP_MON_INVALIDATE] &&
		      p1.range_calls > p0.range_calls, "NUMA workload invalidated armed pages");
		printf("# numa seconds=%u invalidations=%" PRIu64 " range_calls=%" PRIu64 " lookup_nodes=%" PRIu64 "\n",
		       numa_seconds, (uint64_t)(after.terminals[CMDP_MON_INVALIDATE] - before.terminals[CMDP_MON_INVALIDATE]),
		       (uint64_t)(p1.range_calls - p0.range_calls), (uint64_t)(p1.lookup_nodes - p0.lookup_nodes));
		CHECK(!kill(child_pid, SIGCONT), "resume target after NUMA trace capture");
	} else {
		char op = 'P'; struct reply result;
		CHECK(progress(&p0) && transfer(command_fd, &op, 1, true) &&
		      transfer(response_fd, &result, sizeof(result), false) && !result.result,
		      "mprotect preserves page markers and both victim boundaries reject stores");
		CHECK(progress(&p1) && status(&after) && conserved(&after), "range accounting captured before STOP");
		uint64_t revoked = after.terminals[CMDP_MON_INVALIDATE] - before.terminals[CMDP_MON_INVALIDATE];
		CHECK(revoked == (empty ? 0 : victims), "exactly the resident victim pages invalidate");
		bool states = true;
		if (!empty) for (unsigned long i = 0; i < victims; i++)
			if (!page_state(base + ((head ? 0 : prefix) + i) * PAGE, CMDP_MON_PAGE_INVALIDATED)) {
				states = false; break;
			}
		if (prefix) {
			uintptr_t first = base + (head ? victims * PAGE : 0);
			states &= page_state(first, CMDP_MON_PAGE_ARMED) &&
				page_state(first + (prefix - 1) * PAGE, CMDP_MON_PAGE_ARMED);
		}
		CHECK(states, "victim states and unaffected boundary survivors correct");
		uint64_t calls = p1.lookup_calls - p0.lookup_calls;
		uint64_t nodes = p1.lookup_nodes - p0.lookup_nodes;
		CHECK(calls <= revoked + 4, "search count scales with victims, including bounded empty searches");
		unsigned int bits = 0;
		for (uint64_t n = before.active + 1; n; n >>= 1) bits++;
		/* Red-black tree height <= 2 log2(n+1). Allow small integer slack.
		 * This is a work bound, not a hardware-dependent runtime threshold.
		 */
		CHECK(calls && nodes <= calls * (2 * bits + 2), "lookup work bounded by tree height per search");
		printf("CMDP_RANGE_METRIC {\"prefix_pages\":%lu,\"victim_pages\":%lu,\"position\":\"%s\","
		       "\"active_before\":%" PRIu64 ",\"invalidated\":%" PRIu64 ",\"elapsed_ns\":%" PRIu64 ","
		       "\"lookup_calls\":%" PRIu64 ",\"lookup_nodes\":%" PRIu64 ",\"lookup_max_nodes\":%" PRIu64 ","
		       "\"index_lock_max_ns\":%" PRIu64 ",\"range_ns\":%" PRIu64 "}\n",
		       prefix, victims, empty ? "empty" : head ? "head" : "tail", (uint64_t)before.active, revoked,
		       result.elapsed_ns, calls, nodes, (uint64_t)p1.lookup_max_nodes,
		       (uint64_t)p1.index_lock_max_ns, (uint64_t)(p1.range_ns - p0.range_ns));
	}
	CHECK(!ioctl(monitor_fd, CMDP_MON_STOP) && status(&after) && !after.active &&
	      after.state == CMDP_MON_STOPPED && conserved(&after), "STOP leaves conserved zero active ownership");
	char verify_op = 'V'; struct reply verified;
	CHECK(transfer(command_fd, &verify_op, 1, true) &&
	      transfer(response_fd, &verified, sizeof(verified), false) && !verified.result,
	      "after STOP every fixture page retains data and accepts verified stores through reused PTEs");
	close(monitor_fd); monitor_fd = -1;
	char exit_op = 'X'; int child_status;
	CHECK(transfer(command_fd, &exit_op, 1, true) && waitpid(child_pid, &child_status, 0) == child_pid &&
	      WIFEXITED(child_status) && !WEXITSTATUS(child_status), "target exits cleanly");
	child_pid = -1;
	CHECK(balanced(), "global entries, managed, isolated all zero");
	close(pid_fd); close(command_fd); close(response_fd);
	printf("1..%d\n", checks); return 0;
fail:
	/* In a broken kernel this can block: the outer host watchdog kills QEMU. */
	if (monitor_fd >= 0) { ioctl(monitor_fd, CMDP_MON_STOP); close(monitor_fd); }
	if (child_pid > 0) { kill(child_pid, SIGKILL); waitpid(child_pid, NULL, 0); }
	printf("1..%d\n", checks); return 1;
}
