// SPDX-License-Identifier: GPL-2.0
/*
 * Three separate sweeps: first anonymous write, ARM every page, protected write.
 * Build against cmdp-v1 UAPI: cc -O2 -Wall -Wextra -Werror -std=gnu11 \
 *   -Wl,-z,now -I/path/to/kernel/usr/include cmdp_sweep_bench.c -lm -o cmdp_sweep_bench
 *
 * Each timestamp interval includes the operation and timestamp overhead.
 * A protected store includes fault handling, any refault, return to userspace,
 * and execution of the store; it is not a kernel-only handler measurement.
 * RDTSCP waits for prior instructions, not global visibility of buffered stores.
 * TSC ticks are reference-clock ticks, not variable-frequency CPU core cycles.
 */
#define _GNU_SOURCE
#include <cpuid.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#include <linux/cmdp.h>

#if !defined(__x86_64__)
#error "This benchmark requires x86-64 with invariant TSC and RDTSCP"
#endif

#define STATS_PATH "/sys/kernel/debug/cmdp/stats"
#define MAX_MIB 65536U
#define OVERHEAD_SAMPLES 10000U
#define CSV_BUFFER_SIZE 65536U
#define NS_PER_SEC UINT64_C(1000000000)

enum stat_id { ARMS, PUBLISHES, REVOKES, WAITERS, REJECTS, ENTRIES,
	MANAGED, ISOLATED, STALE, ARM_NS, REVOKE_NS, NR_STATS };
static const char *const stat_names[NR_STATS] = {
	"arms", "publishes", "revokes", "waiters", "rejects", "entries",
	"managed", "isolated", "stale", "arm_ns", "revoke_ns"
};
struct counters { uint64_t v[NR_STATS]; };
enum phase_id { INITIAL_WRITE, ARM_IOCTL, WP_WRITE, NR_PHASES };
struct phase {
	const char *name;
	uint64_t *ticks, wall_ns;
	long minor, major;
	size_t completed;
};
static volatile sig_atomic_t interrupted;

static void signal_handler(int sig) { interrupted = sig; }

static inline uint64_t tsc_begin(void)
{
	unsigned int lo, hi;
	__asm__ volatile("lfence\n\trdtsc\n\tlfence"
		: "=a" (lo), "=d" (hi) : : "memory");
	return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t tsc_end(unsigned int *aux)
{
	unsigned int lo, hi;
	__asm__ volatile("rdtscp\n\tlfence"
		: "=a" (lo), "=d" (hi), "=c" (*aux) : : "memory");
	return ((uint64_t)hi << 32) | lo;
}

static int raw_ns(uint64_t *ns)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts)) {
		perror("clock_gettime");
		return -1;
	}
	*ns = (uint64_t)ts.tv_sec * NS_PER_SEC + (uint64_t)ts.tv_nsec;
	return 0;
}

static int parse_u64(const char *s, uint64_t maximum, uint64_t *out)
{
	char *end;
	unsigned long long n;
	if (!s || !*s || *s < '0' || *s > '9')
		return -1;
	errno = 0;
	n = strtoull(s, &end, 10);
	if (errno || *end || n > maximum)
		return -1;
	*out = (uint64_t)n;
	return 0;
}

static int pin_cpu(int requested)
{
	cpu_set_t allowed, one;
	int cpu = requested;
	if (sched_getaffinity(0, sizeof(allowed), &allowed)) {
		perror("sched_getaffinity");
		return -1;
	}
	if (cpu < 0)
		for (cpu = 0; cpu < CPU_SETSIZE && !CPU_ISSET(cpu, &allowed); cpu++)
			;
	if (cpu >= CPU_SETSIZE || !CPU_ISSET(cpu, &allowed)) {
		fprintf(stderr, "requested CPU is outside the allowed affinity set\n");
		return -1;
	}
	CPU_ZERO(&one);
	CPU_SET(cpu, &one);
	if (sched_setaffinity(0, sizeof(one), &one)) {
		perror("sched_setaffinity");
		return -1;
	}
	return cpu;
}

static int check_tsc(void)
{
	unsigned int a, b, c, d;
	if (!__get_cpuid(0x80000001U, &a, &b, &c, &d) || !(d & (1U << 27)) ||
	    !__get_cpuid(0x80000007U, &a, &b, &c, &d) || !(d & (1U << 8))) {
		fprintf(stderr, "RDTSCP and invariant TSC are required\n");
		return -1;
	}
	return 0;
}

static int calibrate(unsigned int token, double rates[3], uint64_t spans[3],
		     double *ticks_per_ns)
{
	double sorted[3], tmp;
	for (int i = 0; i < 3; i++) {
		struct timespec delay = { .tv_sec = 0, .tv_nsec = 150000000 };
		uint64_t wall0, wall1, start, end;
		unsigned int aux0, aux1;
		if (raw_ns(&wall0))
			return -1;
		start = tsc_end(&aux0);
		while (nanosleep(&delay, &delay)) {
			if (errno != EINTR || interrupted) {
				perror("calibration nanosleep");
				return -1;
			}
		}
		end = tsc_end(&aux1);
		if (raw_ns(&wall1))
			return -1;
		if (aux0 != token || aux1 != token || end <= start || wall1 <= wall0) {
			fprintf(stderr, "invalid TSC calibration or changed TSC_AUX\n");
			return -1;
		}
		spans[i] = wall1 - wall0;
		sorted[i] = rates[i] = (double)(end - start) / (double)spans[i];
	}
	for (int i = 0; i < 3; i++)
		for (int j = i + 1; j < 3; j++)
			if (sorted[i] > sorted[j]) {
				tmp = sorted[i]; sorted[i] = sorted[j]; sorted[j] = tmp;
			}
	*ticks_per_ns = sorted[1];
	return 0;
}

/* Fixed buffers avoid new userspace allocation while pages are protected. */
static int read_counters(int fd, struct counters *out)
{
	char buf[4096], *line, *save;
	size_t used = 0;
	unsigned int seen = 0;
	ssize_t n;
	if (lseek(fd, 0, SEEK_SET) < 0)
		goto io_error;
	while ((n = read(fd, buf + used, sizeof(buf) - 1 - used)) > 0) {
		used += (size_t)n;
		if (used == sizeof(buf) - 1) {
			fprintf(stderr, "CMD-P stats exceeded fixed input buffer\n");
			return -1;
		}
	}
	if (n < 0)
		goto io_error;
	buf[used] = '\0';
	memset(out, 0, sizeof(*out));
	for (line = strtok_r(buf, "\n", &save); line;
	     line = strtok_r(NULL, "\n", &save)) {
		char key[32], value[32], extra;
		uint64_t parsed;
		if (sscanf(line, "%31s %31s %c", key, value, &extra) != 2 ||
		    parse_u64(value, UINT64_MAX, &parsed)) {
			fprintf(stderr, "malformed CMD-P stats line\n");
			return -1;
		}
		for (int i = 0; i < NR_STATS; i++) {
			if (strcmp(key, stat_names[i]))
				continue;
			if (seen & (1U << i)) {
				fprintf(stderr, "duplicate CMD-P counter: %s\n", key);
				return -1;
			}
			seen |= 1U << i;
			out->v[i] = parsed;
		}
	}
	if (seen != (1U << NR_STATS) - 1) {
		fprintf(stderr, "missing required CMD-P stats fields\n");
		return -1;
	}
	return 0;
io_error:
	perror("read CMD-P stats");
	return -1;
}

static int validate_counters(const struct counters *base,
			     const struct counters *now, uint64_t arms,
			     uint64_t revokes, uint64_t active)
{
	for (int i = 0; i < NR_STATS; i++) {
		uint64_t expected;
		if (i == ARM_NS || i == REVOKE_NS) {
			if (now->v[i] >= base->v[i])
				continue;
			goto mismatch;
		}
		if (i == ENTRIES || i == MANAGED || i == ISOLATED)
			expected = active;
		else {
			uint64_t delta = i == ARMS || i == PUBLISHES ? arms :
					 i == REVOKES ? revokes : 0;
			if (UINT64_MAX - base->v[i] < delta)
				goto mismatch;
			expected = base->v[i] + delta;
		}
		if (now->v[i] == expected)
			continue;
		fprintf(stderr, "counter %s: expected=%" PRIu64 " observed=%" PRIu64 "\n",
			stat_names[i], expected, now->v[i]);
		return -1;
mismatch:
		fprintf(stderr, "CMD-P counter decreased or overflowed: %s\n", stat_names[i]);
		return -1;
	}
	return 0;
}

static int check_residency(void *region, size_t bytes, unsigned char *vec,
			   size_t pages, size_t expected)
{
	size_t resident = 0;
	if (mincore(region, bytes, vec)) {
		perror("mincore");
		return -1;
	}
	for (size_t i = 0; i < pages; i++)
		resident += !!(vec[i] & 1U);
	if (resident != expected) {
		fprintf(stderr, "residency mismatch: expected=%zu observed=%zu\n",
			expected, resident);
		return -1;
	}
	return 0;
}

static void prefault(void *memory, size_t bytes, size_t page_size)
{
	volatile unsigned char *p = memory;
	for (size_t i = 0; i < bytes; i += page_size)
		p[i] = 0;
	p[bytes - 1] = 0;
}

static int run_phase(struct phase *phase, enum phase_id kind, int fd,
		     volatile unsigned char *region, size_t pages,
		     size_t page_size, unsigned int token, int cpu)
{
	struct rusage before, after;
	struct cmdp_page_req req = { .addr = 0, .flags = 0 };
	uint64_t wall0, wall1;
	if (interrupted || sched_getcpu() != cpu)
		return -1;
	printf("phase_start=%s pages=%zu\n", phase->name, pages);
	if (getrusage(RUSAGE_SELF, &before)) {
		perror("getrusage before phase");
		return -1;
	}
	if (raw_ns(&wall0))
		return -1;
	for (size_t i = 0; i < pages; i++) {
		volatile unsigned char *addr = region + i * page_size;
		uint64_t start, end;
		unsigned int aux;
		int rc = 0;
		if (interrupted)
			return -1;
		if (kind == ARM_IOCTL) {
			req.addr = (uintptr_t)addr;
			start = tsc_begin();
			rc = ioctl(fd, CMDP_IOC_ARM, &req);
			end = tsc_end(&aux);
		} else {
			unsigned char value = kind == INITIAL_WRITE ? 0x42 : 0x5a;
			start = tsc_begin();
			*addr = value;
			end = tsc_end(&aux);
		}
		if (rc) {
			fprintf(stderr, "ARM failed at page=%zu offset=%zu: %s\n",
				i, i * page_size, strerror(errno));
			return -1;
		}
		phase->completed = i + 1;
		if (aux != token || end < start) {
			fprintf(stderr, "invalid TSC or TSC_AUX changed in %s page=%zu\n",
				phase->name, i);
			return -1;
		}
		phase->ticks[i] = end - start;
	}
	if (raw_ns(&wall1))
		return -1;
	if (getrusage(RUSAGE_SELF, &after)) {
		perror("getrusage after phase");
		return -1;
	}
	phase->wall_ns = wall1 - wall0;
	phase->minor = after.ru_minflt - before.ru_minflt;
	phase->major = after.ru_majflt - before.ru_majflt;
	if (sched_getcpu() != cpu) {
		fprintf(stderr, "CPU affinity changed during phase\n");
		return -1;
	}
	return interrupted ? -1 : 0;
}

static int compare_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return (x > y) - (x < y);
}

static void summarize(const char *name, uint64_t *values, size_t n, double rate)
{
	long double mean = 0, m2 = 0, sum = 0;
	for (size_t i = 0; i < n; i++) {
		long double delta = (long double)values[i] - mean;
		mean += delta / (i + 1);
		m2 += delta * ((long double)values[i] - mean);
		sum += values[i];
	}
	long double std = sqrtl(m2 / n);
	qsort(values, n, sizeof(*values), compare_u64);
	long double median = ((long double)values[(n - 1) / 2] + values[n / 2]) / 2;
	size_t p95 = (n * 95 + 99) / 100 - 1, p99 = (n * 99 + 99) / 100 - 1;
	printf("%s count=%zu mean_ns=%.3Lf std_population_ns=%.3Lf"
	       " median_ns=%.3Lf p95_ns=%.3f p99_ns=%.3f min_ns=%.3f max_ns=%.3f\n",
	       name, n, mean / rate, std / rate, median / rate,
	       values[p95] / rate, values[p99] / rate, values[0] / rate, values[n - 1] / rate);
	printf("%s mean_ticks=%.3Lf std_population_ticks=%.3Lf sum_ticks=%.0Lf"
	       " sum_timed_intervals_ns=%.3Lf\n", name, mean, std, sum, sum / rate);
}

static void usage(const char *program)
{
	printf("Usage: %s [--mib N] [--cpu N] [--csv PATH]\n"
	       "  --mib N    1..%u MiB; default 1024 (1 GiB)\n"
	       "  --cpu N    allowed logical CPU; default first allowed CPU\n"
	       "  --csv PATH create new per-page raw sample file; refuses overwrite\n"
	       "  --help     show this help\n"
	       "Requires /dev/cmdp, readable CMD-P debugfs stats, and 4 KiB pages.\n"
	       "Strict sequence: first writes -> all ARM ioctls -> all protected writes.\n",
	       program, MAX_MIB);
}

int main(int argc, char **argv)
{
	static const struct option options[] = {
		{ "mib", required_argument, NULL, 'm' },
		{ "cpu", required_argument, NULL, 'c' },
		{ "csv", required_argument, NULL, 'o' },
		{ "help", no_argument, NULL, 'h' }, { NULL, 0, NULL, 0 }
	};
	struct phase phases[NR_PHASES] = {
		{ .name = "initial_write" }, { .name = "arm_ioctl" }, { .name = "wp_write" }
	};
	struct counters base, boundaries[NR_PHASES], final;
	struct sigaction sa = { .sa_handler = signal_handler };
	uint64_t mib = 1024, parsed, overhead[OVERHEAD_SAMPLES], spans[3];
	uint64_t *samples = NULL;
	double rates[3], rate = 0;
	unsigned char *resident = NULL;
	void *region = MAP_FAILED;
	char *csv_buffer = NULL;
	const char *csv_path = NULL;
	FILE *csv = NULL;
	size_t bytes = 0, pages = 0, sample_bytes = 0;
	long page_size = sysconf(_SC_PAGESIZE);
	unsigned int token, aux;
	int cpu = -1, fd = -1, stats_fd = -1, result = EXIT_FAILURE, opt;
	bool completed = false;
	setvbuf(stdout, NULL, _IONBF, 0);
	while ((opt = getopt_long(argc, argv, "", options, NULL)) != -1) {
		switch (opt) {
		case 'm':
			if (parse_u64(optarg, MAX_MIB, &mib) || !mib)
				goto bad_arguments;
			break;
		case 'c':
			if (parse_u64(optarg, CPU_SETSIZE - 1, &parsed))
				goto bad_arguments;
			cpu = (int)parsed;
			break;
		case 'o': csv_path = optarg; break;
		case 'h': usage(argv[0]); return EXIT_SUCCESS;
		default: goto bad_arguments;
		}
	}
	if (optind != argc)
		goto bad_arguments;
	if (page_size != 4096 || mib > SIZE_MAX / (1024U * 1024U)) {
		fprintf(stderr, "requires 4 KiB pages and representable allocation size\n");
		goto out;
	}
	bytes = (size_t)mib * 1024U * 1024U;
	pages = bytes / (size_t)page_size;
	if (pages > SIZE_MAX / (NR_PHASES * sizeof(*samples)) || pages > SIZE_MAX / 100) {
		fprintf(stderr, "sample allocation or percentile index overflow\n");
		goto out;
	}
	sample_bytes = pages * NR_PHASES * sizeof(*samples);
	if (check_tsc() || (cpu = pin_cpu(cpu)) < 0)
		goto out;
	(void)tsc_end(&token); /* Full token, including any NUMA bits; not a CPU ID. */
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGINT, &sa, NULL) || sigaction(SIGTERM, &sa, NULL)) {
		perror("sigaction");
		goto out;
	}
	samples = malloc(sample_bytes);
	resident = malloc(pages);
	if (!samples || !resident) {
		perror("allocate auxiliary buffers");
		goto out;
	}
	prefault(samples, sample_bytes, (size_t)page_size);
	prefault(resident, pages, (size_t)page_size);
	for (int i = 0; i < NR_PHASES; i++)
		phases[i].ticks = samples + (size_t)i * pages;
	if (csv_path) {
		csv = fopen(csv_path, "wx");
		csv_buffer = malloc(CSV_BUFFER_SIZE);
		if (!csv || !csv_buffer) {
			perror("create CSV or allocate CSV buffer");
			goto out;
		}
		prefault(csv_buffer, CSV_BUFFER_SIZE, (size_t)page_size);
		if (setvbuf(csv, csv_buffer, _IOFBF, CSV_BUFFER_SIZE)) {
			fprintf(stderr, "setvbuf CSV failed\n");
			goto out;
		}
	}
	stats_fd = open(STATS_PATH, O_RDONLY | O_CLOEXEC);
	if (stats_fd < 0) { perror("open CMD-P stats"); goto out; }
	fd = open("/dev/cmdp", O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror("open /dev/cmdp"); goto out; }
	if (calibrate(token, rates, spans, &rate))
		goto out;
	for (size_t i = 0; i < OVERHEAD_SAMPLES; i++) {
		uint64_t start = tsc_begin(), end = tsc_end(&aux);
		if (aux != token || end < start) {
			fprintf(stderr, "invalid timestamp overhead calibration\n");
			goto out;
		}
		overhead[i] = end - start;
	}
	region = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (region == MAP_FAILED) { perror("mmap target"); goto out; }
	if (madvise(region, bytes, MADV_NOHUGEPAGE)) {
		perror("MADV_NOHUGEPAGE target");
		goto out;
	}
	if (read_counters(stats_fd, &base) || validate_counters(&base, &base, 0, 0, 0) ||
	    check_residency(region, bytes, resident, pages, 0))
		goto out;
	printf("mib=%" PRIu64 " bytes=%zu pages=%zu page_size=%ld cpu=%d tsc_aux=0x%x\n",
	       mib, bytes, pages, page_size, cpu, token);
	for (int i = 0; i < NR_PHASES; i++) {
		uint64_t armed = i >= ARM_IOCTL ? pages : 0;
		uint64_t revoked = i == WP_WRITE ? pages : 0;
		if (run_phase(&phases[i], (enum phase_id)i, fd, region, pages,
			      (size_t)page_size, token, cpu) ||
		    read_counters(stats_fd, &boundaries[i]) ||
		    validate_counters(&base, &boundaries[i], armed, revoked, armed - revoked) ||
		    (i != ARM_IOCTL && check_residency(region, bytes, resident, pages, pages)))
			goto out;
	}
	completed = true;
	result = EXIT_SUCCESS;
out:
	/* Closing the session first revokes even a partially armed region. */
	if (fd >= 0) {
		if (close(fd)) { perror("close /dev/cmdp"); result = EXIT_FAILURE; }
		if (stats_fd >= 0) {
			if (read_counters(stats_fd, &final) || final.v[ENTRIES] ||
			    final.v[MANAGED] || final.v[ISOLATED]) {
				fprintf(stderr, "cleanup: CMD-P active counters did not return to zero\n");
				result = EXIT_FAILURE;
			} else {
				printf("cleanup entries=0 managed=0 isolated=0\n");
			}
		}
	}
	if (region != MAP_FAILED && munmap(region, bytes)) {
		perror("munmap target"); result = EXIT_FAILURE;
	}
	if (stats_fd >= 0 && close(stats_fd)) { perror("close stats"); result = EXIT_FAILURE; }
	if (completed) {
		if (csv) {
			fprintf(csv, "page_index,offset_bytes,initial_write_ticks,arm_ioctl_ticks,wp_write_ticks\n");
			for (size_t i = 0; i < pages; i++)
				fprintf(csv, "%zu,%zu,%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
					i, i * (size_t)page_size, phases[0].ticks[i],
					phases[1].ticks[i], phases[2].ticks[i]);
			if (ferror(csv)) { perror("write CSV"); result = EXIT_FAILURE; }
		}
		for (int i = 0; i < 3; i++)
			printf("tsc_calibration_%d ticks_per_ns=%.9f interval_ns=%" PRIu64 "\n",
			       i + 1, rates[i], spans[i]);
		printf("tsc_ticks_per_ns=%.9f calibration=median_of_3 overhead_subtracted=no\n", rate);
		summarize("empty_timestamp_pair", overhead, OVERHEAD_SAMPLES, rate);
		for (int i = 0; i < NR_PHASES; i++) {
			summarize(phases[i].name, phases[i].ticks, pages, rate);
			printf("%s wall_ns=%" PRIu64 " wall_ns_per_page=%.3f"
			       " process_minor_faults=%ld process_major_faults=%ld\n",
			       phases[i].name, phases[i].wall_ns,
			       (double)phases[i].wall_ns / pages, phases[i].minor, phases[i].major);
			printf("%s delta_arms=%" PRIu64 " delta_publishes=%" PRIu64
			       " delta_revokes=%" PRIu64 " entries=%" PRIu64
			       " managed=%" PRIu64 " isolated=%" PRIu64 "\n",
			       phases[i].name, boundaries[i].v[ARMS] - base.v[ARMS],
			       boundaries[i].v[PUBLISHES] - base.v[PUBLISHES],
			       boundaries[i].v[REVOKES] - base.v[REVOKES], boundaries[i].v[ENTRIES],
			       boundaries[i].v[MANAGED], boundaries[i].v[ISOLATED]);
		}
	} else {
		fprintf(stderr, "run incomplete: initial_write=%zu arm_ioctl=%zu wp_write=%zu\n",
			phases[0].completed, phases[1].completed, phases[2].completed);
	}
	if (csv && fclose(csv)) { perror("close CSV"); result = EXIT_FAILURE; }
	free(csv_buffer);
	free(resident);
	free(samples);
	if (interrupted) {
		fprintf(stderr, "interrupted by signal %d; session cleanup attempted\n", interrupted);
		result = 128 + interrupted;
	}
	if (completed && result == EXIT_SUCCESS)
		printf("validation=PASS initial_resident_pages=0 final_resident_pages=%zu"
		       " new_rejects=0 new_waiters=0 new_stale=0\n", pages);
	else
		printf("validation=FAIL\n");
	return result;
bad_arguments:
	usage(argv[0]);
	return EXIT_FAILURE;
}
