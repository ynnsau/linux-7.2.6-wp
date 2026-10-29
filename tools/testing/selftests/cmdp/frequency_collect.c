// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <linux/perf_event.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#include "snapshot_control.h"

#define EVENTS 3
static const char *const names[EVENTS] = { "aperf", "mperf", "tsc" };
static volatile sig_atomic_t interrupted;
struct cpu_counter {
	int cpu, fd[EVENTS];
	uint64_t id[EVENTS];
};
struct collector {
	struct cpu_counter counters[CPU_SETSIZE];
	size_t count;
	uint64_t sequence;
	char online[4096];
};
struct group_read {
	uint64_t nr, time_enabled, time_running;
	struct { uint64_t value, id; } values[EVENTS];
};
static void on_signal(int signal_number) { interrupted = signal_number; }

static int read_text(const char *path, char *out, size_t size)
{
	FILE *f = fopen(path, "r");
	if (!f) return -1;
	bool ok = fgets(out, size, f) != NULL;
	int saved = errno;
	if (fclose(f) && ok) return -1;
	if (!ok) { errno = saved ? saved : EIO; return -1; }
	out[strcspn(out, "\r\n")] = 0;
	return 0;
}

static unsigned long long integer(const char *s, unsigned long long maximum)
{
	char *end;
	errno = 0;
	unsigned long long value = strtoull(s, &end, 10);
	if (!*s || *s == '-' || errno || *end || value > maximum) {
		fprintf(stderr, "invalid integer: %s\n", s); exit(2);
	}
	return value;
}

static int parse_cpus(struct collector *c)
{
	const char *p = c->online;
	while (*p) {
		char *end;
		if (*p < '0' || *p > '9') goto invalid;
		errno = 0;
		unsigned long first = strtoul(p, &end, 10), last = first;
		if (errno || first >= CPU_SETSIZE) goto invalid;
		if (*end == '-') {
			p = end + 1;
			if (*p < '0' || *p > '9') goto invalid;
			last = strtoul(p, &end, 10);
			if (errno || last < first || last >= CPU_SETSIZE) goto invalid;
		}
		if (*end && *end != ',') goto invalid;
		for (unsigned long cpu = first; cpu <= last; cpu++) {
			if (c->count && cpu <= (unsigned long)c->counters[c->count - 1].cpu)
				goto invalid;
			struct cpu_counter *counter = &c->counters[c->count++];
			counter->cpu = cpu;
			for (int i = 0; i < EVENTS; i++) counter->fd[i] = -1;
		}
		if (!*end) return 0;
		p = end + 1;
	}
invalid:
	errno = EINVAL;
	return -1;
}

static int discover_events(uint32_t *type, uint64_t config[EVENTS])
{
	char text[128], path[256];
	if (read_text("/sys/bus/event_source/devices/msr/type", text, sizeof(text))) return -1;
	*type = integer(text, UINT32_MAX);
	if (read_text("/sys/bus/event_source/devices/msr/format/event", text, sizeof(text))) return -1;
	if (strcmp(text, "config:0-63")) {
		fprintf(stderr, "unsupported msr event encoding: %s\n", text);
		errno = ENOTSUP; return -1;
	}
	for (int i = 0; i < EVENTS; i++) {
		snprintf(path, sizeof(path), "/sys/bus/event_source/devices/msr/events/%s", names[i]);
		if (read_text(path, text, sizeof(text))) return -1;
		char *end;
		errno = 0;
		if (strncmp(text, "event=", 6)) { errno = EINVAL; return -1; }
		config[i] = strtoull(text + 6, &end, 0);
		if (errno || end == text + 6 || *end) { errno = EINVAL; return -1; }
	}
	return 0;
}

static int open_counters(struct collector *c, uint32_t type, const uint64_t config[EVENTS])
{
	for (size_t cpu = 0; cpu < c->count; cpu++) {
		struct cpu_counter *counter = &c->counters[cpu];
		for (int i = 0; i < EVENTS; i++) {
			struct perf_event_attr attr = {
				.type = type, .size = sizeof(attr), .config = config[i],
				.disabled = i == 0, .pinned = i == 0,
				.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_ID |
					PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING
			};
			counter->fd[i] = syscall(__NR_perf_event_open, &attr, -1,
					counter->cpu, i ? counter->fd[0] : -1, PERF_FLAG_FD_CLOEXEC);
			if (counter->fd[i] < 0 || ioctl(counter->fd[i], PERF_EVENT_IOC_ID, &counter->id[i])) {
				fprintf(stderr, "cannot open/read msr %s on CPU%d: %s\n",
					names[i], counter->cpu, strerror(errno));
				return -1;
			}
		}
		if (ioctl(counter->fd[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) ||
		    ioctl(counter->fd[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP)) return -1;
	}
	return 0;
}

static int decode_group(const struct cpu_counter *counter, const struct group_read *r,
			uint64_t values[EVENTS])
{
	if (r->nr != EVENTS || !r->time_running || r->time_enabled != r->time_running) {
		fprintf(stderr, "CPU%d: missing or multiplexed MSR counters (nr=%" PRIu64
			", enabled=%" PRIu64 ", running=%" PRIu64 ")\n", counter->cpu,
			r->nr, r->time_enabled, r->time_running);
		errno = EIO; return -1;
	}
	bool found[EVENTS] = { false };
	for (int i = 0; i < EVENTS; i++) {
		int j;
		for (j = 0; j < EVENTS; j++) if (r->values[i].id == counter->id[j]) break;
		if (j == EVENTS || found[j]) { errno = EPROTO; return -1; }
		found[j] = true;
		values[j] = r->values[i].value;
	}
	return 0;
}

static int sample(struct collector *c, const char *label)
{
	char online[sizeof(c->online)];
	if (read_text("/sys/devices/system/cpu/online", online, sizeof(online))) return -1;
	if (strcmp(online, c->online)) {
		fprintf(stderr, "CPU online set changed during frequency collection\n");
		errno = ESTALE; return -1;
	}
	c->sequence++;
	for (size_t cpu = 0; cpu < c->count; cpu++) {
		struct cpu_counter *counter = &c->counters[cpu];
		struct group_read r;
		uint64_t values[EVENTS];
		uint64_t begin = snapshot_clock_ns(CLOCK_MONOTONIC);
		uint64_t real = snapshot_clock_ns(CLOCK_REALTIME);
		ssize_t n = read(counter->fd[0], &r, sizeof(r));
		uint64_t end = snapshot_clock_ns(CLOCK_MONOTONIC);
		if (n != sizeof(r)) {
			fprintf(stderr, "CPU%d: short MSR group read: %zd\n", counter->cpu, n);
			if (n >= 0) errno = EIO;
			return -1;
		}
		if (decode_group(counter, &r, values)) return -1;
		printf("{\"event\":\"frequency\",\"seq\":%" PRIu64 ",\"label\":\"%s\",\"cpu\":%d,"
		       "\"monotonic_ns\":%" PRIu64 ",\"realtime_ns\":%" PRIu64
		       ",\"read_end_ns\":%" PRIu64 ",\"time_enabled\":%" PRIu64
		       ",\"time_running\":%" PRIu64 ",\"aperf\":%" PRIu64
		       ",\"mperf\":%" PRIu64 ",\"tsc\":%" PRIu64 "}\n",
		       c->sequence, label, counter->cpu, begin, real, end,
		       r.time_enabled, r.time_running, values[0], values[1], values[2]);
	}
	return 0;
}

static int boundary_sample(const char *label, void *arg)
{
	snapshot_boundary("boundary_begin", label, true);
	int ret = sample(arg, label), saved = errno;
	snapshot_boundary("boundary_end", label, !ret);
	errno = saved;
	return ret;
}

static void usage(const char *name)
{
	fprintf(stderr, "Usage: %s [--interval-ms 11000] [--observer-cpu 24] "
		"[--duration-sec N] [--control-fd FD]\n"
		"Count all online CPUs using grouped MSR APERF/MPERF/TSC events.\n"
		"No overflow sampling; process stays on the observer CPU.\n"
		"Control commands: snapshot LABEL\\n; EOF disables command input.\n", name);
}

int main(int argc, char **argv)
{
	static const struct option options[] = {
		{ "interval-ms", 1, 0, 'i' }, { "observer-cpu", 1, 0, 'c' },
		{ "duration-sec", 1, 0, 'd' }, { "control-fd", 1, 0, 'f' },
		{ "help", 0, 0, 'h' }, { 0 }
	};
	struct collector collector = { 0 };
	struct snapshot_control control = { .fd = -1 };
	unsigned int interval_ms = 11000, observer_cpu = 24;
	uint64_t duration = 0;
	int option, control_fd = -1, result = 1;
	while ((option = getopt_long(argc, argv, "+", options, NULL)) != -1) {
		switch (option) {
		case 'i': interval_ms = integer(optarg, 3600000); break;
		case 'c': observer_cpu = integer(optarg, CPU_SETSIZE - 1); break;
		case 'd': duration = integer(optarg, 86400); break;
		case 'f':
			if (control_fd >= 0) { fprintf(stderr, "repeated --control-fd\n"); return 2; }
			control_fd = integer(optarg, INT_MAX); break;
		case 'h': usage(argv[0]); return 0;
		default: usage(argv[0]); return 2;
		}
	}
	if (optind != argc || interval_ms < 10) { usage(argv[0]); return 2; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	struct sigaction action = { .sa_handler = on_signal };
	sigemptyset(&action.sa_mask);
	sigaction(SIGTERM, &action, NULL);
	sigaction(SIGINT, &action, NULL);
	if (control_fd >= 0 && snapshot_control_open(&control, control_fd)) { perror("control fd"); goto out; }
	if (read_text("/sys/devices/system/cpu/online", collector.online, sizeof(collector.online)) ||
	    parse_cpus(&collector)) { perror("online CPUs"); goto out; }
	cpu_set_t affinity, actual;
	CPU_ZERO(&affinity); CPU_SET(observer_cpu, &affinity);
	if (sched_setaffinity(0, sizeof(affinity), &affinity) ||
	    sched_getaffinity(0, sizeof(actual), &actual)) { perror("observer affinity"); goto out; }
	if (!CPU_EQUAL(&affinity, &actual)) { fprintf(stderr, "observer affinity differs\n"); goto out; }
	uint32_t type;
	uint64_t config[EVENTS];
	if (discover_events(&type, config) || open_counters(&collector, type, config)) {
		perror("MSR counters (root/CAP_PERFMON may be required)"); goto out;
	}
	printf("{\"event\":\"frequency_config\",\"online_cpus\":\"%s\",\"cpu_count\":%zu,"
	       "\"observer_cpu\":%u,\"interval_ms\":%u,\"pmu_type\":%u,"
	       "\"aperf_config\":%" PRIu64 ",\"mperf_config\":%" PRIu64
	       ",\"tsc_config\":%" PRIu64 "}\n", collector.online, collector.count,
	       observer_cpu, interval_ms, type, config[0], config[1], config[2]);
	if (boundary_sample("initial", &collector)) { perror("initial sample"); goto out; }
	uint64_t start = snapshot_clock_ns(CLOCK_MONOTONIC);
	uint64_t next = start + (uint64_t)interval_ms * 1000000;
	result = 0;
	while (!interrupted) {
		uint64_t now = snapshot_clock_ns(CLOCK_MONOTONIC);
		if (duration && now - start >= duration * 1000000000ULL) break;
		uint64_t deadline = next;
		if (duration && start + duration * 1000000000ULL < deadline)
			deadline = start + duration * 1000000000ULL;
		int timeout = now >= deadline ? 0 : (int)((deadline - now + 999999) / 1000000);
		struct pollfd p = { .fd = control.fd, .events = POLLIN };
		int n = poll(&p, 1, timeout);
		if (n < 0 && errno != EINTR) { perror("poll"); result = 1; break; }
		if (p.revents && snapshot_control_read(&control, boundary_sample, &collector)) {
			perror("control snapshot"); result = 1; break;
		}
		now = snapshot_clock_ns(CLOCK_MONOTONIC);
		if (now >= next && !interrupted) {
			if (sample(&collector, "")) { perror("periodic sample"); result = 1; break; }
			do { next += (uint64_t)interval_ms * 1000000; } while (next <= now);
		}
	}
	if (!result && boundary_sample("final", &collector)) { perror("final sample"); result = 1; }
out:
	for (size_t cpu = 0; cpu < collector.count; cpu++)
		for (int i = 0; i < EVENTS; i++)
			if (collector.counters[cpu].fd[i] >= 0) close(collector.counters[cpu].fd[i]);
	if (control.fd >= 0) close(control.fd);
	printf("{\"event\":\"frequency_exit\",\"exit_code\":%d,\"signal\":%d}\n", result, interrupted);
	return result;
}
