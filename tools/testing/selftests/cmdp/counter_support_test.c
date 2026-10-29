// SPDX-License-Identifier: GPL-2.0
/* Exercise production parsers and group decoding without privileges. */
#define main frequency_collect_main
#include "frequency_collect.c"
#undef main
#include <assert.h>

struct labels { int count; char last[SNAPSHOT_LABEL_MAX + 1]; };
static int record_label(const char *label, void *arg)
{
	struct labels *labels = arg;
	labels->count++;
	strcpy(labels->last, label);
	return 0;
}

static void check_invalid(const char *input, size_t size, bool close_writer)
{
	int p[2];
	struct snapshot_control control = { .fd = -1 };
	struct labels labels = { 0 };
	assert(pipe(p) == 0);
	assert(snapshot_control_open(&control, p[0]) == 0);
	close(p[0]);
	assert(write(p[1], input, size) == (ssize_t)size);
	if (close_writer) close(p[1]);
	assert(snapshot_control_read(&control, record_label, &labels) == -1);
	assert(errno == EPROTO);
	assert(labels.count == 0);
	if (control.fd >= 0) close(control.fd);
	if (!close_writer) close(p[1]);
}

int main(void)
{
	int p[2];
	struct snapshot_control control = { .fd = -1 };
	struct labels labels = { 0 };
	assert(pipe(p) == 0);
	assert(snapshot_control_open(&control, p[0]) == 0);
	assert(fcntl(control.fd, F_GETFD) & FD_CLOEXEC);
	close(p[0]);
	assert(write(p[1], "snap", 4) == 4);
	assert(snapshot_control_read(&control, record_label, &labels) == 0);
	assert(labels.count == 0);
	const char *tail = "shot measurement_start\nsnapshot measurement_end\n";
	assert(write(p[1], tail, strlen(tail)) == (ssize_t)strlen(tail));
	assert(snapshot_control_read(&control, record_label, &labels) == 0);
	assert(labels.count == 2 && !strcmp(labels.last, "measurement_end"));
	char maximum[sizeof("snapshot ") + SNAPSHOT_LABEL_MAX + 1];
	memcpy(maximum, "snapshot ", 9);
	memset(maximum + 9, 'x', SNAPSHOT_LABEL_MAX);
	maximum[9 + SNAPSHOT_LABEL_MAX] = '\n';
	assert(write(p[1], maximum, 10 + SNAPSHOT_LABEL_MAX) == 10 + SNAPSHOT_LABEL_MAX);
	assert(snapshot_control_read(&control, record_label, &labels) == 0);
	assert(labels.count == 3 && strlen(labels.last) == SNAPSHOT_LABEL_MAX);
	close(p[1]);
	assert(snapshot_control_read(&control, record_label, &labels) == 0);
	assert(control.fd == -1); /* poll ignores -1: EOF cannot cause a busy loop. */
	check_invalid("invalid label\n", 14, false);
	check_invalid("snapshot \n", 10, false);
	check_invalid("snapshot bad label\n", 19, false);
	check_invalid("snapshot bad\"label\n", 19, false);
	check_invalid("snapshot bad\0label\n", 19, false);
	check_invalid("snapshot incomplete", 19, true);
	char overlong[128]; memset(overlong, 'a', sizeof(overlong));
	check_invalid(overlong, sizeof(overlong), false);
	struct cpu_counter counter = { .cpu = 24, .id = { 100, 101, 102 } };
	struct group_read reading = {
		.nr = 3, .time_enabled = 10000, .time_running = 10000,
		.values = { { 30, 102 }, { 10, 100 }, { 20, 101 } }
	};
	uint64_t values[3];
	assert(decode_group(&counter, &reading, values) == 0);
	assert(values[0] == 10 && values[1] == 20 && values[2] == 30);
	reading.time_running--;
	assert(decode_group(&counter, &reading, values) == -1);
	reading.time_running++;
	reading.values[2].id = 100;
	assert(decode_group(&counter, &reading, values) == -1);
	reading.values[2].id = 999;
	assert(decode_group(&counter, &reading, values) == -1);
	struct collector collector = { .online = "0-3,8,24-25" };
	assert(parse_cpus(&collector) == 0 && collector.count == 7);
	assert(collector.counters[4].cpu == 8 && collector.counters[6].cpu == 25);
	memset(&collector, 0, sizeof(collector)); strcpy(collector.online, "0-3,2");
	assert(parse_cpus(&collector) == -1);
	snapshot_boundary("test_anchor", "valid:label-1.0", true);
	puts("counter support tests passed");
	return 0;
}
