/* SPDX-License-Identifier: GPL-2.0 */
#ifndef CMDP_SNAPSHOT_CONTROL_H
#define CMDP_SNAPSHOT_CONTROL_H

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* Deliberately small, line-oriented interface; labels are safe JSON strings. */
#define SNAPSHOT_LABEL_MAX 96
struct snapshot_control {
	int fd;
	size_t used;
	char line[sizeof("snapshot ") + SNAPSHOT_LABEL_MAX];
};
typedef int (*snapshot_callback)(const char *label, void *arg);

static int snapshot_control_open(struct snapshot_control *c, int fd)
{
	int flags = fcntl(fd, F_GETFL);
	if (flags < 0 || (flags & O_ACCMODE) == O_WRONLY) {
		if (flags >= 0) errno = EBADF;
		return -1;
	}
	c->fd = fcntl(fd, F_DUPFD_CLOEXEC, 3);
	if (c->fd < 0) return -1;
	if (fcntl(c->fd, F_SETFL, flags | O_NONBLOCK)) {
		int saved = errno;
		close(c->fd);
		c->fd = -1;
		errno = saved;
		return -1;
	}
	c->used = 0;
	return 0;
}

static int snapshot_control_line(struct snapshot_control *c,
				 snapshot_callback callback, void *arg)
{
	const char *label = c->line + strlen("snapshot ");
	if (c->used <= strlen("snapshot ") ||
	    strncmp(c->line, "snapshot ", strlen("snapshot ")) ||
	    strlen(label) > SNAPSHOT_LABEL_MAX) goto invalid;
	for (const char *p = label; *p; p++) {
		if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
		      (*p >= '0' && *p <= '9') || strchr("_.:-", *p)))
			goto invalid;
	}
	return callback(label, arg);
invalid:
	fprintf(stderr, "control: expected 'snapshot LABEL' (1..%d safe label characters)\n",
		SNAPSHOT_LABEL_MAX);
	errno = EPROTO;
	return -1;
}

static int snapshot_control_read(struct snapshot_control *c,
				 snapshot_callback callback, void *arg)
{
	char bytes[1024];
	ssize_t count;
	while ((count = read(c->fd, bytes, sizeof(bytes))) > 0) {
		for (ssize_t i = 0; i < count; i++) {
			if (bytes[i] == '\n') {
				c->line[c->used] = 0;
				if (snapshot_control_line(c, callback, arg)) return -1;
				c->used = 0;
			} else if (!bytes[i] || c->used == sizeof(c->line) - 1) {
				fprintf(stderr, "control: NUL byte or overlong command\n");
				errno = EPROTO;
				return -1;
			} else {
				c->line[c->used++] = bytes[i];
			}
		}
	}
	if (!count) {
		close(c->fd);
		c->fd = -1;
		if (c->used) {
			fprintf(stderr, "control: incomplete command at EOF\n");
			errno = EPROTO;
			return -1;
		}
		return 0;
	}
	return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR ? 0 : -1;
}

static uint64_t snapshot_clock_ns(clockid_t clock)
{
	struct timespec t;
	if (clock_gettime(clock, &t)) {
		perror("clock_gettime");
		return 0;
	}
	return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}

/* The realtime read lies between the two monotonic timestamps. */
static void snapshot_boundary(const char *event, const char *label, bool ok)
{
	uint64_t mono = snapshot_clock_ns(CLOCK_MONOTONIC);
	uint64_t real = snapshot_clock_ns(CLOCK_REALTIME);
	uint64_t end = snapshot_clock_ns(CLOCK_MONOTONIC);
	printf("{\"event\":\"%s\",\"label\":\"%s\",\"monotonic_ns\":%" PRIu64
	       ",\"realtime_ns\":%" PRIu64 ",\"anchor_end_ns\":%" PRIu64
	       ",\"ok\":%s}\n", event, label, mono, real, end, ok ? "true" : "false");
}
#endif
