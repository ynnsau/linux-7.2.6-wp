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
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "../kselftest.h"

#define CONTROL "/sys/kernel/debug/cmdp/control"
#define STATS "/sys/kernel/debug/cmdp/stats"
#define PAGE 4096

static int control = -1;

static void require(bool ok, const char *what)
{
	if (!ok)
		ksft_exit_fail_msg("%s: %s\n", what, strerror(errno));
}

static int command(const char *op, unsigned long value)
{
	char buf[96];
	int len = snprintf(buf, sizeof(buf), "%s %lx\n", op, value);
	ssize_t ret = write(control, buf, len);

	return ret == len ? 0 : -1;
}

static uint64_t stat_value(const char *key)
{
	FILE *f = fopen(STATS, "r");
	char name[32];
	uint64_t value;

	require(f != NULL, "open stats");
	while (fscanf(f, "%31s %" SCNu64, name, &value) == 2) {
		if (!strcmp(name, key)) {
			fclose(f);
			return value;
		}
	}
	fclose(f);
	ksft_exit_fail_msg("missing statistic %s\n", key);
	return 0;
}

static uint64_t page_pfn(void *p)
{
	uint64_t entry = 0;
	int fd = open("/proc/self/pagemap", O_RDONLY);

	require(fd >= 0, "open pagemap");
	require(pread(fd, &entry, sizeof(entry),
		      (uintptr_t)p / PAGE * sizeof(entry)) == sizeof(entry), "pagemap");
	close(fd);
	require(entry & (1ULL << 63), "page present");
	return entry & ((1ULL << 55) - 1);
}

static volatile unsigned char *new_page(void)
{
	void *p = mmap(NULL, PAGE, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

	require(p != MAP_FAILED, "mmap");
	/* Required when the kernel supports anonymous huge pages. */
	if (madvise(p, PAGE, MADV_NOHUGEPAGE) && errno != EINVAL)
		require(false, "MADV_NOHUGEPAGE");
	memset(p, 0x42, PAGE);
	return p;
}

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
	return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static bool balanced(void)
{
	return stat_value("managed") == 0 && stat_value("isolated") == 0 &&
		stat_value("entries") == 0;
}

struct writer {
	pthread_barrier_t *barrier;
	volatile unsigned char *p;
};

static void *write_page(void *arg)
{
	struct writer *w = arg;

	pthread_barrier_wait(w->barrier);
	*w->p = 0x76;
	return NULL;
}

static void concurrent_writers(volatile unsigned char *p)
{
	pthread_barrier_t barrier;
	pthread_t threads[2];
	struct writer args[2];
	uint64_t revokes = stat_value("revokes"), waiters = stat_value("waiters");
	uint64_t pfn = page_pfn((void *)p);
	int i;

	require(command("test_delay_ms", 100) == 0, "revoke delay fixture");
	require(!pthread_barrier_init(&barrier, NULL, 3), "barrier");
	for (i = 0; i < 2; i++) {
		args[i] = (struct writer) { .barrier = &barrier, .p = p + i };
		require(!pthread_create(&threads[i], NULL, write_page, &args[i]), "pthread_create");
	}
	require(command("arm", (uintptr_t)p) == 0, "arm concurrent writers");
	pthread_barrier_wait(&barrier);
	for (i = 0; i < 2; i++)
		require(!pthread_join(threads[i], NULL), "pthread_join");
	pthread_barrier_destroy(&barrier);
	require(command("test_delay_ms", 0) == 0, "clear delay");
	ksft_test_result(stat_value("revokes") == revokes + 1 &&
			 stat_value("waiters") == waiters + 1 &&
			 page_pfn((void *)p) == pfn && balanced(),
			 "two writers: one owner, one waiter, same PFN\n");
}

/* Keep the control fd in another mm so exit exercises notifier release,
 * rather than merely the debugfs file's close handler.
 */
static void exit_cleanup(void)
{
	int sv[2], status, received = -1;
	pid_t child;
	uint64_t revokes = stat_value("revokes");
	char data = 'x', ancillary[CMSG_SPACE(sizeof(int))];
	struct iovec iov = { .iov_base = &data, .iov_len = 1 };
	struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1,
		.msg_control = ancillary, .msg_controllen = sizeof(ancillary) };
	struct cmsghdr *cmsg;

	require(!socketpair(AF_UNIX, SOCK_STREAM, 0, sv), "socketpair");
	child = fork();
	require(child >= 0, "fork before session");
	if (!child) {
		volatile unsigned char *p = new_page();

		close(sv[0]);
		control = open(CONTROL, O_WRONLY);
		if (control < 0 || command("arm", (uintptr_t)p))
			_exit(1);
		memset(ancillary, 0, sizeof(ancillary));
		cmsg = CMSG_FIRSTHDR(&msg);
		cmsg->cmsg_level = SOL_SOCKET;
		cmsg->cmsg_type = SCM_RIGHTS;
		cmsg->cmsg_len = CMSG_LEN(sizeof(int));
		memcpy(CMSG_DATA(cmsg), &control, sizeof(control));
		if (sendmsg(sv[1], &msg, 0) != 1)
			_exit(2);
		/* Parent acknowledges the fd transfer before this mm exits. */
		if (read(sv[1], &data, 1) != 1)
			_exit(3);
		_exit(0);
	}
	close(sv[1]);
	require(recvmsg(sv[0], &msg, 0) == 1, "receive live session fd");
	cmsg = CMSG_FIRSTHDR(&msg);
	require(cmsg && cmsg->cmsg_type == SCM_RIGHTS, "SCM_RIGHTS");
	memcpy(&received, CMSG_DATA(cmsg), sizeof(received));
	require(write(sv[0], &data, 1) == 1, "ack fd");
	require(waitpid(child, &status, 0) == child, "wait exit");
	ksft_test_result(WIFEXITED(status) && !WEXITSTATUS(status) &&
			 stat_value("revokes") == revokes + 1 && balanced(),
			 "mm exit drains entries while session fd remains open\n");
	close(received);
	close(sv[0]);
}

static int run_tests(void)
{
	volatile unsigned char *p;
	uint64_t revokes, publishes, pfn, t0, arm_ns, write_ns, stale;
	int i, ret;

	ksft_print_header();
	if (geteuid() || sysconf(_SC_PAGESIZE) != PAGE)
		ksft_exit_skip("requires root and 4 KiB pages\n");
	control = open(CONTROL, O_WRONLY);
	if (control < 0)
		ksft_exit_skip("CONFIG_CMDP control unavailable: %s\n", strerror(errno));
	if (command("test_delay_ms", 0)) {
		close(control);
		ksft_exit_skip("CONFIG_CMDP_TEST required\n");
	}
	ksft_set_plan(11);
	p = new_page();
	pfn = page_pfn((void *)p);
	revokes = stat_value("revokes");
	t0 = now_ns();
	require(command("arm", (uintptr_t)p) == 0, "arm pre-touched page");
	arm_ns = now_ns() - t0;
	ksft_test_result(*p == 0x42 && stat_value("managed") == 1 &&
			 stat_value("revokes") == revokes, "arm and read without revoke\n");
	t0 = now_ns();
	*p = 0x55;
	write_ns = now_ns() - t0;
	ksft_test_result(*p == 0x55 && stat_value("revokes") == revokes + 1 &&
			 page_pfn((void *)p) == pfn && balanced(),
			 "first write revokes once, completes, preserves PFN\n");
	ksft_print_msg("application arm=%" PRIu64 " ns first-write=%" PRIu64 " ns\n",
		       arm_ns, write_ns);
	concurrent_writers(p);

	for (i = 0; i < 2; i++) {
		*p = 0x51;
		require(command(i ? "test_get" : "test_pin", (uintptr_t)p) == 0, "hold fixture");
		publishes = stat_value("publishes");
		errno = 0;
		ret = command("arm", (uintptr_t)p);
		ksft_test_result(ret == -1 && errno == EBUSY &&
				 stat_value("publishes") == publishes && balanced(),
				 "reject existing %s\n", i ? "GUP reference" : "FOLL_PIN pin");
		require(command("test_drop", 0) == 0, "drop fixture");
	}
	*p = 0x52;
	publishes = stat_value("publishes");
	require(command("test_fail_after", 1) == 0, "inject failure after first PTE");
	ret = command("arm", (uintptr_t)p);
	*p = 0x53;
	ksft_test_result(ret == -1 && stat_value("publishes") == publishes &&
			 *p == 0x53 && balanced(), "partial protection failure leaves ordinary RO fault usable\n");

	for (i = 0; i < 64; i++) {
		require(command("arm", (uintptr_t)p) == 0, "repeat arm");
		*p = i;
		require(balanced(), "cycle ref/isolation balance");
	}
	ksft_test_result_pass("64 arm/write cycles balance ownership\n");

	require(command("arm", (uintptr_t)p) == 0, "arm old generation");
	require(command("revoke", (uintptr_t)p) == 0, "revoke old generation");
	require(command("arm", (uintptr_t)p) == 0, "arm new generation");
	stale = stat_value("stale");
	revokes = stat_value("revokes");
	require(command("test_stale", (uintptr_t)p) == 0, "old completion injection");
	ksft_test_result(stat_value("stale") == stale + 1 &&
			 stat_value("managed") == 1 && stat_value("revokes") == revokes,
			 "stale completion leaves new generation managed\n");
	errno = 0;
	ret = mprotect((void *)p, PAGE, PROT_READ);
	ksft_test_result(ret == -1 && errno == EBUSY,
			 "mprotect is rejected while managed\n");
	require(munmap((void *)p, PAGE) == 0, "munmap active");
	ksft_test_result(stat_value("revokes") == revokes + 1 && balanced(),
			 "munmap synchronously drains and releases ownership\n");
	close(control);
	control = -1;
	exit_cleanup();
	ksft_finished();
	return 0;
}

int main(int argc, char **argv)
{
	/* Optional standalone initramfs runner for an isolated test kernel. */
	if (argc > 1 && !strcmp(argv[1], "--init")) {
		int status;
		pid_t child;

		mkdir("/proc", 0755);
		mkdir("/sys", 0755);
		mount("proc", "/proc", "proc", 0, NULL);
		mount("sysfs", "/sys", "sysfs", 0, NULL);
		mount("debugfs", "/sys/kernel/debug", "debugfs", 0, NULL);
		child = fork();
		if (!child)
			return run_tests();
		if (child < 0 || waitpid(child, &status, 0) != child)
			status = 1 << 8;
		printf("CMDP_SELFTEST_EXIT=%d\n", WIFEXITED(status) ? WEXITSTATUS(status) : 1);
		fflush(stdout);
		reboot(RB_POWER_OFF);
		return 1;
	}
	return run_tests();
}
