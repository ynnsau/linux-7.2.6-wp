// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <setjmp.h>
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
#error "pidfd_open syscall number is unavailable for this architecture"
#endif
#endif

#define PAGE 4096
#define NPAGE 100
#define INIT(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
	(x).version = CMDP_MONITOR_VERSION; } while (0)
static int checks, fd = -1, pidfd = -1, control_fd = -1, response_fd = -1;
static pid_t target = -1;
static uintptr_t base, sparse, heap_addr;
static struct cmdp_monitor_page before[NPAGE], after[NPAGE];
static sigjmp_buf write_fault;
static volatile sig_atomic_t expect_fault;
static void fault_handler(int sig)
{
	if (expect_fault) siglongjmp(write_fault, 1);
	_exit(128 + sig);
}
static bool store_faults(unsigned char *p)
{
	expect_fault = 1;
	if (!sigsetjmp(write_fault, 1)) {
		*(volatile unsigned char *)p = 0xee;
		expect_fault = 0;
		return false;
	}
	expect_fault = 0;
	return true;
}
struct reply { uint64_t addr, extra, heap; int result; };
static bool transfer(int f, void *buf, size_t len, bool writing)
{
	char *p = buf;
	while (len) {
		ssize_t n = writing ? write(f, p, len) : read(f, p, len);
		if (n < 0 && errno == EINTR) continue;
		if (n <= 0) return false;
		p += n; len -= n;
	}
	return true;
}
struct writer { volatile unsigned char *p; unsigned char value; pthread_barrier_t *barrier; };
static void *writer(void *arg)
{
	struct writer *w = arg;
	pthread_barrier_wait(w->barrier);
	*w->p = w->value;
	return NULL;
}
static int writers(unsigned char *area, bool different)
{
	pthread_t ts[8];
	struct writer ws[8];
	pthread_barrier_t b;
	if (pthread_barrier_init(&b, NULL, 9)) return 1;
	for (int i = 0; i < 8; i++) {
		ws[i] = (struct writer){ area + (different ? i * PAGE : i), (unsigned char)(0x60 + i), &b };
		if (pthread_create(&ts[i], NULL, writer, &ws[i])) _exit(21);
	}
	pthread_barrier_wait(&b);
	for (int i = 0; i < 8; i++) pthread_join(ts[i], NULL);
	pthread_barrier_destroy(&b);
	for (int i = 0; i < 8; i++) if (*ws[i].p != ws[i].value) return 1;
	return 0;
}
static void child(int command, int response)
{
	if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)) _exit(10);
	struct sigaction sa = { .sa_handler = fault_handler };
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGSEGV, &sa, NULL)) _exit(16);
	size_t length = NPAGE * PAGE;
	unsigned char *area = mmap(NULL, length, PROT_READ | PROT_WRITE,
				  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	unsigned char *lazy = MAP_FAILED;
	uintptr_t heap = 0;
	if (area == MAP_FAILED) _exit(11);
	memset(area, 0x42, length);
	struct reply r = { .addr = (uintptr_t)area };
	if (!transfer(response, &r, sizeof(r), true)) _exit(12);
	char op;
	while (transfer(command, &op, 1, false)) {
		r.result = 0;
		switch (op) {
		case 'W':
		case 'R':
			for (int i = 0; i < 25; i++) {
				*(volatile unsigned char *)(area + i * PAGE) = op;
				if (area[i * PAGE] != (unsigned char)op) r.result = 1;
			}
			break;
		case 'C': r.result = writers(area, false); break;
		case 'D': r.result = writers(area, true); break;
		case 'P':
			r.result = mprotect(area, PAGE, PROT_READ);
			if (!r.result) {
				r.result = !store_faults(area);
				if (mprotect(area, PAGE, PROT_READ | PROT_WRITE)) r.result = 1;
			}
			break;
		case 'M': {
			void *reserved = mmap(NULL, length + PAGE, PROT_NONE,
					      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (reserved == MAP_FAILED) { r.result = 1; break; }
			void *m = mremap(area, length, length + PAGE,
					MREMAP_MAYMOVE | MREMAP_FIXED, reserved);
			if (m == MAP_FAILED) { munmap(reserved, length + PAGE); r.result = 1; break; }
			area = m; length += PAGE;
			break;
		}
		case 'U': r.result = munmap(area, PAGE); break;
		case 'B': {
			void *m = mmap(area, PAGE, PROT_READ | PROT_WRITE,
				      MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
			r.result = m == MAP_FAILED;
			if (!r.result) memset(m, 0x51, PAGE);
			break;
		}
		case 'N':
			lazy = mmap(NULL, 16 * PAGE, PROT_READ | PROT_WRITE,
				    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			r.result = lazy == MAP_FAILED;
			break;
		case 'T': if (lazy == MAP_FAILED) r.result = 1; else lazy[0] = 0x35; break;
		case 'A': {
			void *allocation = malloc(5 * PAGE);
			if (!allocation) { r.result = 1; break; }
			heap = ((uintptr_t)allocation + PAGE - 1) & ~(uintptr_t)(PAGE - 1);
			memset((void *)heap, 0x3a, 4 * PAGE);
			break;
		}
		case 'F': {
			pid_t c = fork();
			if (!c) _exit(0);
			int s = 0;
			if (c < 0 || waitpid(c, &s, 0) != c || !WIFEXITED(s) || WEXITSTATUS(s)) r.result = 1;
			break;
		}
		case 'E': execl("/bin/sh", "sh", "-c", "exit 0", (char *)NULL); _exit(13);
		case 'X': _exit(0);
		default: _exit(14);
		}
		r.addr = (uintptr_t)area;
		r.extra = lazy == MAP_FAILED ? 0 : (uintptr_t)lazy;
		r.heap = heap;
		if (!transfer(response, &r, sizeof(r), true)) _exit(15);
	}
	_exit(0);
}
static bool command(char op)
{
	struct reply r;
	if (!transfer(control_fd, &op, 1, true) || !transfer(response_fd, &r, sizeof(r), false)) return false;
	base = r.addr; sparse = r.extra; heap_addr = r.heap;
	return !r.result;
}
static bool status(struct cmdp_monitor_status *s)
{
	INIT(*s);
	return !ioctl(fd, CMDP_MON_STATUS, s);
}
static void periodic_diagnostic(const char *phase, const struct cmdp_monitor_status *s,
				uint64_t old_passes, int polls, int status_errno)
{
	printf("# periodic %s: old_completed=%llu state=%u stop_reason=%u "
	       "started=%llu completed=%llu active=%llu overruns=%llu "
	       "last_pass=%llu cursor=0x%llx complete=%u error=%d "
	       "start_ns=%llu end_ns=%llu now_ns=%llu polls=%d status_errno=%d\n",
	       phase, (unsigned long long)old_passes, s->state, s->stop_reason,
	       (unsigned long long)s->passes_started, (unsigned long long)s->passes_completed,
	       (unsigned long long)s->active, (unsigned long long)s->overruns,
	       (unsigned long long)s->last_pass.id, (unsigned long long)s->last_pass.cursor,
	       s->last_pass.complete, s->last_pass.error,
	       (unsigned long long)s->last_pass.start_ns, (unsigned long long)s->last_pass.end_ns,
	       (unsigned long long)s->now_ns, polls, status_errno);
}
static bool conserved(void)
{
	struct cmdp_monitor_status s;
	if (!status(&s)) return false;
	uint64_t terminal = 0, cohort_arms = 0;
	for (int i = 0; i < CMDP_MON_NR_REASONS; i++) terminal += s.terminals[i];
	if (s.first_arms + s.rearms != s.active + terminal) return false;
	for (uint64_t id = 1; id <= s.passes_completed; id++) {
		struct cmdp_monitor_cohort c;
		INIT(c); c.pass_id = id;
		if (ioctl(fd, CMDP_MON_COHORT, &c)) return false;
		if (c.pass.first_arms + c.pass.rearms != c.pass.active + c.pass.write_revoked + c.pass.other_terminal) return false;
		if (c.pass.active_first + c.pass.active_rearm != c.pass.active ||
		    c.pass.active_first > c.pass.first_arms || c.pass.active_rearm > c.pass.rearms) return false;
		cohort_arms += c.pass.first_arms + c.pass.rearms;
	}
	return s.passes_started != s.passes_completed || cohort_arms == s.first_arms + s.rearms;
}
static bool query(uintptr_t addr, struct cmdp_monitor_page *p)
{
	INIT(*p); p->addr = addr;
	return !ioctl(fd, CMDP_MON_PAGE, p);
}
static bool all_pages(struct cmdp_monitor_page *p)
{
	for (int i = 0; i < NPAGE; i++) if (!query(base + i * PAGE, &p[i])) return false;
	return true;
}
static bool count_states(unsigned int armed, unsigned int off)
{
	unsigned int a = 0, o = 0;
	if (!all_pages(after)) return false;
	for (int i = 0; i < NPAGE; i++) {
		a += after[i].state == CMDP_MON_PAGE_ARMED;
		o += after[i].state == CMDP_MON_PAGE_OFF_WRITE;
	}
	return a == armed && o == off;
}
static bool attach_limit(bool scan_only, unsigned int max_pages)
{
	struct cmdp_monitor_attach a;
	fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
	if (fd < 0) return false;
	INIT(a); a.pidfd = pidfd; a.period_ms = 20;
	a.flags = scan_only ? CMDP_MONITOR_F_SCAN_ONLY : 0;
	a.max_pages = max_pages; a.max_history = 32768;
	return !ioctl(fd, CMDP_MON_ATTACH, &a);
}
static bool attach(bool scan_only) { return attach_limit(scan_only, 16384); }
static bool balanced(void)
{
	FILE *f = fopen("/sys/kernel/debug/cmdp/stats", "r");
	if (!f) return false;
	char key[64]; unsigned long long value;
	int seen = 0;
	bool ok = true;
	while (fscanf(f, "%63s %llu", key, &value) == 2) {
		if (!strcmp(key, "entries") || !strcmp(key, "managed") || !strcmp(key, "isolated")) {
			seen++; ok &= value == 0;
		}
	}
	fclose(f);
	return ok && seen == 3;
}
static bool killed_owner_cleanup(void)
{
	int ready[2], st;
	if (pipe2(ready, O_CLOEXEC)) return false;
	pid_t owner = fork();
	if (!owner) {
		close(ready[0]);
		char c = attach(false) && !ioctl(fd, CMDP_MON_SCAN_ONCE) ? 'Y' : 'N';
		if (!transfer(ready[1], &c, 1, true)) _exit(25);
		for (;;) pause();
	}
	close(ready[1]);
	if (owner < 0) { close(ready[0]); return false; }
	char c = 0;
	bool ok = transfer(ready[0], &c, 1, false) && c == 'Y';
	close(ready[0]);
	if (kill(owner, SIGKILL) || waitpid(owner, &st, 0) != owner) return false;
	for (int i = 0; i < 200; i++) {
		if (balanced()) return ok;
		usleep(10000);
	}
	return false;
}
static bool stopped(unsigned int reason)
{
	for (int i = 0; i < 200; i++) {
		struct cmdp_monitor_status s;
		if (!status(&s)) return false;
		if (s.state == CMDP_MON_STOPPED) return s.stop_reason == reason && s.active == 0;
		usleep(10000);
	}
	return false;
}
#define CHECK(test, name) do { checks++; if (!(test)) { \
	printf("not ok %d - %s (errno=%d %s)\n", checks, name, errno, strerror(errno)); \
	goto fail; } printf("ok %d - %s\n", checks, name); } while (0)
int main(int argc, char **argv)
{
	const char *which = "all";
	if (argc == 3 && !strcmp(argv[1], "--case")) which = argv[2];
	else if (argc != 1) return 2;
	bool functional = !strcmp(which, "all") || !strcmp(which, "functional");
	bool concurrent = !strcmp(which, "all") || !strcmp(which, "concurrency");
	bool lifecycle = !strcmp(which, "all") || !strcmp(which, "lifecycle");
	if (!functional && !concurrent && !lifecycle) return 2;
	puts("TAP version 13");
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE || access(CMDP_MONITOR_DEVICE, R_OK | W_OK)) {
		puts("1..0 # SKIP requires root, 4 KiB pages, and /dev/cmdp_monitor"); return 4;
	}
	alarm(90);
	int c[2], r[2];
	CHECK(!pipe2(c, O_CLOEXEC) && !pipe2(r, O_CLOEXEC), "create fixture channels");
	target = fork();
	if (!target) { close(c[1]); close(r[0]); child(c[0], r[1]); }
	CHECK(target >= 0, "fork unmonitored target");
	close(c[0]); close(r[1]); control_fd = c[1]; response_fd = r[0];
	struct reply ready;
	CHECK(transfer(response_fd, &ready, sizeof(ready), false) && !ready.result,
	      "ordinary anonymous 100-page fixture, process THP disabled");
	base = ready.addr;
	pidfd = syscall(SYS_pidfd_open, target, 0);
	CHECK(pidfd >= 0, "pidfd identity");
	CHECK(attach(true), "attach inventory-only session");
	CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE), "inventory sweep");
	struct cmdp_monitor_status s;
	CHECK(status(&s) && !s.active && !s.first_arms && s.last_pass.resident_pages >= NPAGE,
	      "inventory observes resident pages without arming");
	CHECK(!ioctl(fd, CMDP_MON_STOP), "stop inventory"); close(fd); fd = -1;
	CHECK(attach_limit(false, 1), "attach explicit one-page resource limit");
	CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && status(&s) && s.resource_failures &&
	      s.last_pass.skipped[CMDP_MON_SKIP_RESOURCE] && s.active <= 1,
	      "resource cap reports partial coverage explicitly");
	CHECK(!ioctl(fd, CMDP_MON_STOP) && conserved(), "resource-limited session drains and balances");
	close(fd); fd = -1;
	CHECK(attach(false), "attach ARM session");
	CHECK(command('A'), "allocate and populate ordinary malloc storage");
	CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "100-pages-armed");
	struct cmdp_monitor_page heap_page;
	CHECK(query(heap_addr, &heap_page) && heap_page.state == CMDP_MON_PAGE_ARMED,
	      "ordinary malloc page is armed without MADV_NOHUGEPAGE");
	CHECK(conserved(), "initial status and cohort conservation");
	CHECK(all_pages(before), "save page lifetimes and ARM generations");
	if (functional) {
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "never-write-100-survivors");
		bool retained = true;
		for (int i = 0; i < NPAGE; i++) retained &= after[i].arm_generation == before[i].arm_generation && after[i].armed_ns == before[i].armed_ns;
		CHECK(retained, "survivors retain original generation and timestamp");
		CHECK(command('W') && count_states(75, 25), "100-pages-75-survivors-25-write-revokes");
		memcpy(before, after, sizeof(before));
		CHECK(command('R') && count_states(75, 25), "repeated writes do not create another revoke");
		retained = true;
		for (int i = 0; i < 25; i++) retained &= after[i].terminal_ns == before[i].terminal_ns && after[i].arm_count == before[i].arm_count;
		CHECK(retained, "write terminal timestamps remain stable without rearm");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "25-pages-rearmed");
		retained = true;
		for (int i = 0; i < NPAGE; i++) retained &= i < 25 ?
			after[i].arm_count == before[i].arm_count + 1 && after[i].arm_generation != before[i].arm_generation :
			after[i].arm_generation == before[i].arm_generation;
		CHECK(retained, "exactly-25-fixture-rearms-75-retained");
		CHECK(conserved(), "rearm status and cohort conservation");
		CHECK(command('W') && count_states(75, 25), "second-write-cycle");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE), "reset for next scenarios");
	}
	if (concurrent) {
		CHECK(command('C') && count_states(99, 1), "eight-concurrent-writers-one-page-one-terminal");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "rearm concurrent page");
		CHECK(command('D') && count_states(92, 8), "eight-concurrent-writers-eight-pages");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE), "reset after concurrency");
	}
	if (lifecycle) {
		CHECK(command('P'), "mprotect succeeds and read-only store raises SIGSEGV");
		struct cmdp_monitor_page p;
		CHECK(query(base, &p) && p.state != CMDP_MON_PAGE_ARMED, "mprotect invalidates protection");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "rearm after mprotect");
		uintptr_t old = base;
		CHECK(command('M') && base != old, "mremap succeeds and moves target mapping");
		CHECK(!query(old, &p) || p.state != CMDP_MON_PAGE_ARMED, "old mremap address no longer armed");
		CHECK(!ioctl(fd, CMDP_MON_SCAN_ONCE) && count_states(100, 0), "scan discovers moved mapping");
		CHECK(query(base, &p), "record lifetime before unmap");
		uint64_t lifetime = p.lifetime_id;
		CHECK(command('U') && (!query(base, &p) || p.state != CMDP_MON_PAGE_ARMED), "munmap invalidates old lifetime");
		CHECK(command('B') && !ioctl(fd, CMDP_MON_SCAN_ONCE) && query(base, &p) &&
		      p.state == CMDP_MON_PAGE_ARMED && p.lifetime_id != lifetime, "same-address remap starts new lifetime");
		CHECK(command('N') && !ioctl(fd, CMDP_MON_SCAN_ONCE), "scan untouched anonymous mapping");
		CHECK(status(&s) && s.last_pass.skipped[CMDP_MON_SKIP_NONRESIDENT] >= 16,
		      "nonresident pages skipped without forced population");
		CHECK(command('T') && !ioctl(fd, CMDP_MON_SCAN_ONCE) && query(sparse, &p) && p.state == CMDP_MON_PAGE_ARMED,
		      "newly resident page discovered next sweep");
		CHECK(command('F'), "application fork succeeds");
		CHECK(stopped(CMDP_MON_FORK), "fork stops and drains monitor with explicit reason");
		CHECK(command('W'), "original parent remains writable after fork stop");
		close(fd); fd = -1;
		CHECK(attach(false) && !ioctl(fd, CMDP_MON_SCAN_ONCE), "new session after fork stop");
	}
	CHECK(status(&s), "record manual pass count");
	uint64_t old_passes = s.passes_completed;
	uint64_t previous_end = s.now_ns;
	periodic_diagnostic("before-start", &s, old_passes, 0, 0);
	CHECK(!ioctl(fd, CMDP_MON_START), "start periodic monitoring");
	bool progressed = false;
	int polls = 0, status_errno = 0;
	for (int i = 0; i < 500; i++) {
		struct cmdp_monitor_status current;
		polls++;
		if (!status(&current)) { status_errno = errno; break; }
		s = current;
		if (s.passes_completed >= old_passes + 2) { progressed = true; break; }
		usleep(10000);
	}
	periodic_diagnostic("after-wait", &s, old_passes, polls, status_errno);
	bool completed_ok = progressed;
	for (int i = 0; progressed && i < 2; i++) {
		struct cmdp_monitor_cohort cohort;
		INIT(cohort); cohort.pass_id = old_passes + i + 1;
		if (ioctl(fd, CMDP_MON_COHORT, &cohort)) {
			printf("# periodic completed cohort %llu query failed: errno=%d %s\n",
			       (unsigned long long)cohort.pass_id, errno, strerror(errno));
			completed_ok = false;
			break;
		}
		printf("# periodic completed cohort %llu: complete=%u error=%d start_ns=%llu end_ns=%llu\n",
		       (unsigned long long)cohort.pass.id, cohort.pass.complete, cohort.pass.error,
		       (unsigned long long)cohort.pass.start_ns, (unsigned long long)cohort.pass.end_ns);
		completed_ok &= cohort.pass.id == cohort.pass_id && cohort.pass.complete && !cohort.pass.error &&
			cohort.pass.start_ns >= previous_end && cohort.pass.end_ns >= cohort.pass.start_ns;
		previous_end = cohort.pass.end_ns;
	}
	CHECK(completed_ok,
	      "at least two new periodic sweeps complete");
	CHECK(!ioctl(fd, CMDP_MON_STOP) && status(&s) && !s.active && s.state == CMDP_MON_STOPPED,
	      "explicit stop drains all active pages");
	uint64_t pre_stop = s.pre_stop_active;
	CHECK(conserved(), "stopped status and all cohort counts conserve generations");
	CHECK(!ioctl(fd, CMDP_MON_STOP) && status(&s) && s.pre_stop_active == pre_stop,
	      "stop is idempotent and preserves pre-stop active count");
	close(fd); fd = -1;
	CHECK(killed_owner_cleanup(), "SIGKILL monitor owner closes session and drains all entries");
	CHECK(command('W'), "target remains writable after monitor owner SIGKILL");
	CHECK(attach(false) && !ioctl(fd, CMDP_MON_SCAN_ONCE), "arm session for target exit");
	char op = lifecycle ? 'E' : 'X';
	CHECK(transfer(control_fd, &op, 1, true), "request target termination");
	CHECK(stopped(lifecycle ? CMDP_MON_EXEC : CMDP_MON_TARGET_EXIT), "exec-or-exit drains and records reason");
	int child_status;
	CHECK(waitpid(target, &child_status, 0) == target && WIFEXITED(child_status) && !WEXITSTATUS(child_status), "fixture exited successfully");
	target = -1;
	close(fd); close(pidfd); close(control_fd); close(response_fd);
	printf("1..%d\n", checks);
	return 0;
fail:
	if (fd >= 0) { ioctl(fd, CMDP_MON_STOP); close(fd); }
	if (target > 0) { kill(target, SIGKILL); waitpid(target, NULL, 0); }
	if (pidfd >= 0) close(pidfd);
	if (control_fd >= 0) close(control_fd);
	if (response_fd >= 0) close(response_fd);
	printf("1..%d\n", checks);
	return 1;
}
