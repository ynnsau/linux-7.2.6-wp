// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <linux/cmdp.h>

#define DEVICE "/dev/cmdp"
#define STATS "/sys/kernel/debug/cmdp/stats"
#define DEFAULT_SAMPLES 1000
#define MAX_SAMPLES 1000000
#define CLOCK_BASE_STORES 1000000
#define STORES_PER_SAMPLE 256

struct sample_set {
	const char *name;
	uint64_t *values;
};

struct counters {
	uint64_t arm_ns;
	uint64_t revoke_ns;
	uint64_t revokes;
	uint64_t entries;
	uint64_t managed;
	uint64_t isolated;
};

static volatile unsigned char baseline_byte;

static inline uint64_t tsc_begin(void)
{
	unsigned int lo, hi;

	asm volatile("lfence\n\trdtsc" : "=a" (lo), "=d" (hi) : : "memory");
	return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t tsc_end(unsigned int *aux)
{
	unsigned int lo, hi;

	asm volatile("rdtscp\n\tlfence" : "=a" (lo), "=d" (hi), "=c" (*aux) : : "memory");
	return ((uint64_t)hi << 32) | lo;
}

static uint64_t raw_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts)) {
		perror("clock_gettime");
		exit(EXIT_FAILURE);
	}
	return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void fail(const char *what)
{
	fprintf(stderr, "%s: %s\n", what, strerror(errno));
	exit(EXIT_FAILURE);
}

static void read_counters(struct counters *c)
{
	FILE *f = fopen(STATS, "r");
	char name[32];
	uint64_t value;

	if (!f)
		fail("open CMD-P stats");
	memset(c, 0, sizeof(*c));
	while (fscanf(f, "%31s %" SCNu64, name, &value) == 2) {
		if (!strcmp(name, "arm_ns"))
			c->arm_ns = value;
		else if (!strcmp(name, "revoke_ns"))
			c->revoke_ns = value;
		else if (!strcmp(name, "revokes"))
			c->revokes = value;
		else if (!strcmp(name, "entries"))
			c->entries = value;
		else if (!strcmp(name, "managed"))
			c->managed = value;
		else if (!strcmp(name, "isolated"))
			c->isolated = value;
	}
	fclose(f);
}

static void require_balanced(const struct counters *c)
{
	if (c->entries || c->managed || c->isolated) {
		fprintf(stderr, "unbalanced manager: entries=%" PRIu64
			" managed=%" PRIu64 " isolated=%" PRIu64 "\n",
			c->entries, c->managed, c->isolated);
		exit(EXIT_FAILURE);
	}
}

static int compare_u64(const void *a, const void *b)
{
	uint64_t left = *(const uint64_t *)a;
	uint64_t right = *(const uint64_t *)b;

	return (left > right) - (left < right);
}

static void summarize(struct sample_set *set, size_t count, double divisor,
		      const char *unit)
{
	qsort(set->values, count, sizeof(*set->values), compare_u64);
	printf("%-24s count=%zu min=%.2f median=%.2f p95=%.2f p99=%.2f max=%.2f %s\n",
	       set->name, count, set->values[0] / divisor,
	       set->values[(count - 1) / 2] / divisor,
	       set->values[((count - 1) * 95) / 100] / divisor,
	       set->values[((count - 1) * 99) / 100] / divisor,
	       set->values[count - 1] / divisor, unit);
}

static int choose_cpu(void)
{
	cpu_set_t allowed, one;
	int cpu;

	if (sched_getaffinity(0, sizeof(allowed), &allowed))
		fail("sched_getaffinity");
	for (cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (CPU_ISSET(cpu, &allowed))
			break;
	}
	if (cpu == CPU_SETSIZE) {
		errno = EINVAL;
		fail("no allowed CPU");
	}
	CPU_ZERO(&one);
	CPU_SET(cpu, &one);
	if (sched_setaffinity(0, sizeof(one), &one))
		fail("sched_setaffinity");
	return cpu;
}

int main(int argc, char **argv)
{
	struct sample_set baseline, arm, first_write, kernel_arm, kernel_revoke;
	struct sample_set overhead;
	struct counters before, after;
	struct cmdp_page_req req;
	volatile unsigned char *page;
	uint64_t wall_start, baseline_wall, active_wall;
	uint64_t *storage;
	size_t count = DEFAULT_SAMPLES, i;
	unsigned int aux;
	int fd, cpu;
	long page_size = sysconf(_SC_PAGESIZE);

	if (argc > 2 || (argc == 2 &&
	    (sscanf(argv[1], "%zu", &count) != 1 || count < DEFAULT_SAMPLES ||
	     count > MAX_SAMPLES))) {
		fprintf(stderr, "usage: %s [samples>=1000]\n", argv[0]);
		return EXIT_FAILURE;
	}
	if (geteuid() || page_size != 4096) {
		fprintf(stderr, "requires root and 4 KiB pages\n");
		return EXIT_FAILURE;
	}
	cpu = choose_cpu();
	fd = open(DEVICE, O_RDWR);
	if (fd < 0)
		fail("open /dev/cmdp");
	page = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (page == MAP_FAILED)
		fail("mmap CMD-P page");
	if (madvise((void *)page, page_size, MADV_NOHUGEPAGE) && errno != EINVAL)
		fail("MADV_NOHUGEPAGE");
	*page = 0x42;

	storage = calloc(count * 6, sizeof(*storage));
	if (!storage)
		fail("calloc samples");
	baseline = (struct sample_set) { "ordinary store", storage };
	arm = (struct sample_set) { "ioctl arm", storage + count };
	first_write = (struct sample_set) { "first write/revoke", storage + 2 * count };
	kernel_arm = (struct sample_set) { "kernel arm_ns delta", storage + 3 * count };
	kernel_revoke = (struct sample_set) { "kernel revoke_ns delta", storage + 4 * count };
	overhead = (struct sample_set) { "timestamp pair overhead", storage + 5 * count };

	printf("cpu=%d page_size=%ld samples=%zu\n", cpu, page_size, count);
	for (i = 0; i < count; i++) {
		uint64_t start = tsc_begin();
		unsigned int j;

		for (j = 0; j < STORES_PER_SAMPLE; j++)
			baseline_byte++;
		baseline.values[i] = tsc_end(&aux) - start;
	}
	wall_start = raw_ns();
	for (i = 0; i < CLOCK_BASE_STORES; i++)
		baseline_byte++;
	baseline_wall = raw_ns() - wall_start;

	/* Warm up the exact arm/fault path before collecting measured samples. */
	for (i = 0; i < 16; i++) {
		*page = (unsigned char)i;
		if (ioctl(fd, CMDP_IOC_ARM, &(struct cmdp_page_req) {
			.addr = (uintptr_t)page, .flags = 0 }))
			fail("warmup arm ioctl");
		*page = (unsigned char)(i + 1);
	}
	read_counters(&before);
	require_balanced(&before);

	for (i = 0; i < count; i++) {
		uint64_t start = tsc_begin();

		asm volatile("" : : : "memory");
		overhead.values[i] = tsc_end(&aux) - start;
	}
	summarize(&overhead, count, 1.0, "TSC_ticks/pair");
	summarize(&baseline, count, STORES_PER_SAMPLE, "TSC_ticks/store");

	wall_start = raw_ns();
	for (i = 0; i < count; i++) {
		uint64_t start, end;
		unsigned int start_cpu = sched_getcpu();

		read_counters(&before);
		req = (struct cmdp_page_req) { .addr = (uintptr_t)page, .flags = 0 };
		start = tsc_begin();
		if (ioctl(fd, CMDP_IOC_ARM, &req))
			fail("arm ioctl");
		end = tsc_end(&aux);
		arm.values[i] = end - start;
		if ((int)start_cpu != cpu || aux != (unsigned int)cpu ||
		    sched_getcpu() != cpu) {
			fprintf(stderr, "CPU migration during arm sample %zu\n", i);
			return EXIT_FAILURE;
		}
		read_counters(&after);
		kernel_arm.values[i] = after.arm_ns - before.arm_ns;
		if (after.entries != before.entries + 1 ||
		    after.managed != before.managed + 1)
			fail("arm state/counter validation");

		start_cpu = sched_getcpu();
		start = tsc_begin();
		*page = (unsigned char)i;
		end = tsc_end(&aux);
		first_write.values[i] = end - start;
		if ((int)start_cpu != cpu || aux != (unsigned int)cpu ||
		    sched_getcpu() != cpu) {
			fprintf(stderr, "CPU migration during first-write sample %zu\n", i);
			return EXIT_FAILURE;
		}
		read_counters(&after);
		if (after.revokes != before.revokes + 1)
			fail("expected exactly one revoke");
		kernel_revoke.values[i] = after.revoke_ns - before.revoke_ns;
		require_balanced(&after);
	}
	active_wall = raw_ns() - wall_start;

	summarize(&arm, count, 1.0, "TSC_ticks/op");
	summarize(&first_write, count, 1.0, "TSC_ticks/op");
	summarize(&kernel_arm, count, 1.0, "ns/op");
	summarize(&kernel_revoke, count, 1.0, "ns/op");
	printf("CLOCK_MONOTONIC_RAW ordinary_store_avg_ns=%.2f"
	       " cmdp_full_loop_avg_ns=%" PRIu64 "\n",
	       (double)baseline_wall / CLOCK_BASE_STORES, active_wall / count);
	read_counters(&after);
	require_balanced(&after);
	if (munmap((void *)page, page_size))
		fail("munmap");
	close(fd);
	free(storage);
	return 0;
}
