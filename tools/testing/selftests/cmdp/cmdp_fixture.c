// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

/* A real, uncooperative workload: no CMD-P calls or readiness handshake. */
static _Atomic int stop;
static volatile unsigned char *area;
static size_t pages = 100, page_size;
static unsigned int threads = 4;
static const char *mode = "hotcold";

static uint64_t ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}

static void *worker(void *arg)
{
	size_t id = (uintptr_t)arg;
	unsigned char value = 1;
	while (!atomic_load_explicit(&stop, memory_order_relaxed)) {
		/* Distinct bytes avoid a C data race even on the same page. */
		if (!strcmp(mode, "concurrent"))
			area[id] = value++;
		else
			for (size_t i = id; i < pages / 4; i += threads)
				area[i * page_size] = value++;
		struct timespec pause = { .tv_nsec = 1000000 };
		nanosleep(&pause, NULL);
	}
	return NULL;
}

int main(int argc, char **argv)
{
	double seconds = 3;
	pthread_t tids[64];
	unsigned int created = 0;
	int result = 0;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--mode") && i + 1 < argc)
			mode = argv[++i];
		else if (!strcmp(argv[i], "--seconds") && i + 1 < argc)
			seconds = strtod(argv[++i], NULL);
		else if (!strcmp(argv[i], "--threads") && i + 1 < argc)
			threads = (unsigned int)strtoul(argv[++i], NULL, 10);
		else {
			fprintf(stderr, "Usage: %s [--mode never|once|hotcold|concurrent|churn] [--seconds N] [--threads 1..64]\n", argv[0]);
			return 2;
		}
	}
	if (!(seconds > 0 && seconds <= 86400) || threads < 1 || threads > 64 ||
	    (strcmp(mode, "never") && strcmp(mode, "once") && strcmp(mode, "hotcold") &&
	     strcmp(mode, "concurrent") && strcmp(mode, "churn")))
		return 2;
	page_size = (size_t)sysconf(_SC_PAGESIZE);
	area = mmap(NULL, pages * page_size, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (area == MAP_FAILED) {
		perror("mmap");
		return 1;
	}
	/* Deliberately no MADV_NOHUGEPAGE: launcher disables THP per process. */
	memset((void *)area, 0x42, pages * page_size);
	printf("fixture pid=%d mode=%s addr=0x%" PRIxPTR " pages=%zu\n",
	       getpid(), mode, (uintptr_t)area, pages);
	fflush(stdout);
	if (!strcmp(mode, "hotcold") || !strcmp(mode, "concurrent")) {
		for (; created < threads; created++) {
			int err = pthread_create(&tids[created], NULL, worker,
					 (void *)(uintptr_t)created);
			if (err) { errno = err; perror("pthread_create"); result = 1; break; }
		}
	}
	uint64_t start = ns(), deadline = start + (uint64_t)(seconds * 1e9);
	int wrote = 0;
	int cooled = 0;
	while (!result && ns() < deadline) {
		if (!strcmp(mode, "hotcold") && !cooled && ns() - start >= (deadline - start) / 2) {
			atomic_store(&stop, 1);
			for (unsigned int i = 0; i < created; i++) pthread_join(tids[i], NULL);
			created = 0;
			cooled = 1;
			puts("fixture_phase=cold");
			fflush(stdout);
		}
		if (!strcmp(mode, "once") && !wrote && ns() - start > 500000000) {
			for (size_t i = 0; i < pages / 4; i++)
				area[i * page_size] = 0x77;
			wrote = 1;
		}
		if (!strcmp(mode, "churn")) {
			void *p = mmap(NULL, 16 * page_size, PROT_READ | PROT_WRITE,
				       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
			if (p == MAP_FAILED) { perror("churn mmap"); result = 1; break; }
			memset(p, 0x29, 16 * page_size);
			if (mprotect(p, 16 * page_size, PROT_READ) ||
			    mprotect(p, 16 * page_size, PROT_READ | PROT_WRITE)) {
				perror("churn mprotect"); result = 1;
			}
			if (munmap(p, 16 * page_size)) { perror("churn munmap"); result = 1; }
		}
		struct timespec pause = { .tv_nsec = 10000000 };
		nanosleep(&pause, NULL);
	}
	atomic_store(&stop, 1);
	for (unsigned int i = 0; i < created; i++)
		pthread_join(tids[i], NULL);
	/* Cold pages must survive bit-for-bit regardless of sweeps. */
	for (size_t i = pages / 4; i < pages; i++)
		for (size_t j = 0; j < page_size; j++)
			if (area[i * page_size + j] != 0x42)
				result = 1;
	if (munmap((void *)area, pages * page_size))
		result = 1;
	printf("fixture_validation=%s\n", result ? "FAIL" : "PASS");
	return result;
}
