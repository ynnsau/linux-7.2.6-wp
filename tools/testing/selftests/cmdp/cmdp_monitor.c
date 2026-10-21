// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
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

static volatile sig_atomic_t interrupted;
static unsigned int dirty_mode;
static const char *const reasons[] = { "none", "write", "invalidate", "control_stop",
	"target_exit", "exec", "fork", "error", "thp_change", "scope_change" };
static const char *const skips[] = { "vma", "thp_policy", "nonresident", "page_type",
	"busy", "aliases", "pins", "resource", "other", "swapped" };
_Static_assert(sizeof(reasons) / sizeof(reasons[0]) == CMDP_MON_NR_REASONS,
	       "monitor reason names must match the kernel UAPI");
_Static_assert(sizeof(skips) / sizeof(skips[0]) == CMDP_MON_NR_SKIPS,
	       "monitor skip names must match the kernel UAPI");
#define INIT_REQ(x) do { memset(&(x), 0, sizeof(x)); (x).size = sizeof(x); \
	(x).version = CMDP_MONITOR_VERSION; } while (0)

static uint64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}
static void on_signal(int n) { interrupted = n; }
static uint64_t number(const char *s, uint64_t max)
{
	char *end;
	errno = 0;
	unsigned long long n = strtoull(s, &end, 10);
	if (!*s || *s == '-' || errno || *end || n > max) {
		fprintf(stderr, "invalid integer: %s\n", s);
		exit(2);
	}
	return n;
}
static const char *reason(unsigned int n)
{
	return n < sizeof(reasons) / sizeof(reasons[0]) ? reasons[n] : "unknown";
}
static void pass_json(const char *event, const struct cmdp_monitor_pass *p)
{
	flockfile(stdout);
	printf("{\"event\":\"%s\",\"observed_ns\":%" PRIu64 ",\"pass_id\":%llu,\"start_ns\":%llu,\"end_ns\":%llu,"
	       "\"cursor\":%llu,\"writable_pages\":%llu,\"examined_pages\":%llu,"
	       "\"resident_pages\":%llu,\"resident_pages_scope\":\"inspected_supported_vmas\",\"eligible_pages\":%llu,\"first_arms\":%llu,"
	       "\"rearms\":%llu,\"already_armed\":%llu,\"active\":%llu,"
	       "\"write_revoked\":%llu,\"other_terminal\":%llu,\"active_first\":%llu,\"active_rearm\":%llu,\"complete\":%u,"
	       "\"error\":%d,\"skips\":{", event, now_ns(), p->id, p->start_ns, p->end_ns,
	       p->cursor, p->writable_pages, p->examined_pages, p->resident_pages,
	       p->eligible_pages, p->first_arms, p->rearms, p->already_armed,
	       p->active, p->write_revoked, p->other_terminal, p->active_first, p->active_rearm, p->complete, p->error);
	for (unsigned int i = 0; i < CMDP_MON_NR_SKIPS; i++)
		printf("%s\"%s\":%llu", i ? "," : "", skips[i], p->skipped[i]);
	puts("}}");
	funlockfile(stdout);
}
static int dirty_status_read(int fd)
{
	struct cmdp_monitor_dirty_status d;
	INIT_REQ(d);
	if (ioctl(fd, CMDP_MON_DIRTY_STATUS, &d)) {
		if (errno == ENOTTY && !dirty_mode) return 0;
		return -1;
	}
	flockfile(stdout);
	printf("{\"event\":\"dirty_status\",\"now_ns\":%llu,\"mode\":%u,\"quiet_ms\":%u,"
	       "\"passes_started\":%llu,\"passes_completed\":%llu,\"samples\":%llu,"
	       "\"dirty\":%llu,\"clean\":%llu,\"primed\":%llu,\"unknown\":%llu,"
	       "\"qualified\":%llu,\"late_dirty\":%llu,\"resets\":%llu,\"flushes\":%llu,"
	       "\"reset_ns\":%llu,\"flush_ns\":%llu,\"sample_ns\":%llu,"
	       "\"invalidations\":%llu,\"wp_resets\":%llu,\"resource_skips\":%llu,"
	       "\"valid_observations\":%llu,\"metadata_bytes\":%llu}\n",
	       d.now_ns, d.mode, d.quiet_ms, d.passes_started, d.passes_completed,
	       d.samples, d.dirty, d.clean, d.primed, d.unknown, d.qualified,
	       d.late_dirty, d.resets, d.flushes, d.reset_ns, d.flush_ns, d.sample_ns,
	       d.invalidations, d.wp_resets, d.resource_skips, d.valid_observations,
	       d.metadata_bytes);
	funlockfile(stdout);
	return 0;
}
static int status_read(int fd, struct cmdp_monitor_status *s, const char *event)
{
	INIT_REQ(*s);
	if (ioctl(fd, CMDP_MON_STATUS, s))
		return -1;
	flockfile(stdout);
	printf("{\"event\":\"%s\",\"session_id\":%llu,\"target_pid\":%u,"
	       "\"state\":%u,\"stop_reason\":\"%s\",\"now_ns\":%llu,"
	       "\"attach_ns\":%llu,\"period_ms\":%u,\"flags\":%u,"
	       "\"passes_started\":%llu,\"passes_completed\":%llu,\"overruns\":%llu,"
	       "\"first_arms\":%llu,\"rearms\":%llu,\"active\":%llu,"
	       "\"pre_stop_active\":%llu,\"protected_byte_ms\":%llu,"
	       "\"history_entries\":%llu,\"history_resets\":%llu,"
	       "\"resource_failures\":%llu,\"reports_dropped\":%llu,\"terminals\":{",
	       event, s->session_id, s->target_pid, s->state, reason(s->stop_reason),
	       s->now_ns, s->attach_ns, s->period_ms, s->flags,
	       s->passes_started, s->passes_completed, s->overruns,
	       s->first_arms, s->rearms, s->active, s->pre_stop_active,
	       s->protected_byte_ms, s->history_entries, s->history_resets,
	       s->resource_failures, s->reports_dropped);
	for (unsigned int i = 0; i < CMDP_MON_NR_REASONS; i++)
		printf("%s\"%s\":%llu", i ? "," : "", reason(i), s->terminals[i]);
	puts("}}");
	funlockfile(stdout);
	return dirty_status_read(fd);
}

static const char *scan_phase(unsigned int n)
{
	static const char *const names[] = { "idle", "prepare", "wait_mmap", "scanning" };
	return n < sizeof(names) / sizeof(names[0]) ? names[n] : "unknown";
}

/* Additive ioctl: old kernels can still be observed without progress fields. */
static int progress_read(int fd)
{
	struct cmdp_monitor_progress p;
	INIT_REQ(p);
	if (ioctl(fd, CMDP_MON_PROGRESS, &p)) {
		if (errno == ENOTTY)
			return 0;
		return -1;
	}
	printf("{\"event\":\"progress\",\"now_ns\":%llu,\"scan_phase\":\"%s\","
	       "\"phase_since_ns\":%llu,\"scan_lock_wait_ns\":%llu,\"scan_lock_wait_total_ns\":%llu,"
	       "\"scan_lock_retries\":%llu,\"range_calls\":%llu,\"range_inflight\":%llu,"
	       "\"range_ns\":%llu,\"range_max_ns\":%llu,\"lookup_calls\":%llu,"
	       "\"lookup_nodes\":%llu,\"lookup_max_nodes\":%llu,\"index_lock_max_ns\":%llu,"
	       "\"stop_request_ns\":%llu,\"stop_request_active\":%llu,\"stop_drain_ns\":%llu,"
	       "\"stop_complete_ns\":%llu,\"write_calls\":%llu,\"write_ns\":%llu,\"write_max_ns\":%llu}\n",
	       p.now_ns, scan_phase(p.scan_phase), p.phase_since_ns,
	       p.scan_lock_wait_ns, p.scan_lock_wait_total_ns, p.scan_lock_retries,
	       p.range_calls, p.range_inflight, p.range_ns, p.range_max_ns,
	       p.lookup_calls, p.lookup_nodes, p.lookup_max_nodes, p.index_lock_max_ns,
	       p.stop_request_ns, p.stop_request_active, p.stop_drain_ns, p.stop_complete_ns,
	       p.write_calls, p.write_ns, p.write_max_ns);
	return 0;
}
static void cohorts(int fd, uint64_t from, uint64_t to, const char *event)
{
	for (uint64_t id = from; id <= to; id++) {
		struct cmdp_monitor_cohort c;
		INIT_REQ(c);
		c.pass_id = id;
		if (!ioctl(fd, CMDP_MON_COHORT, &c))
			pass_json(event, &c.pass);
		else
			printf("{\"event\":\"cohort_missing\",\"pass_id\":%" PRIu64 ",\"errno\":%d}\n", id, errno);
	}
}

struct stop_reporter {
	int fd, wake[2], poll_ms, failed;
	uint64_t seen;
};

/* The main thread owns STOP. Keep the session fd open until this joins. */
static void *report_while_stopping(void *arg)
{
	struct stop_reporter *r = arg;
	struct pollfd wake = { .fd = r->wake[0], .events = POLLIN };
	for (;;) {
		struct cmdp_monitor_status s;
		if (status_read(r->fd, &s, "stopping_status")) {
			perror("status during stop");
			r->failed = 1;
			break;
		}
		if (progress_read(r->fd)) {
			perror("progress during stop");
			r->failed = 1;
		}
		if (s.passes_completed > r->seen) {
			cohorts(r->fd, r->seen + 1, s.passes_completed, "sweep");
			r->seen = s.passes_completed;
		}
		cohorts(r->fd, 1, s.passes_started, "cohort_snapshot");
		int n = poll(&wake, 1, r->poll_ms);
		if (n > 0)
			break;
		if (n < 0 && errno != EINTR) {
			perror("reporter poll");
			r->failed = 1;
			break;
		}
	}
	return NULL;
}
static pid_t launch(char **argv)
{
	int pipefd[2], child_errno = 0;
	if (pipe2(pipefd, O_CLOEXEC))
		return -1;
	pid_t pid = fork();
	if (!pid) {
		close(pipefd[0]);
		/* Keep JSON stdout separate from unmodified target output. */
		if (dup2(STDERR_FILENO, STDOUT_FILENO) < 0 ||
		    prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0))
			goto child_error;
		execvp(argv[0], argv);
child_error:
		child_errno = errno;
		if (write(pipefd[1], &child_errno, sizeof(child_errno)) != sizeof(child_errno))
			_exit(126);
		_exit(127);
	}
	close(pipefd[1]);
	if (pid < 0) { close(pipefd[0]); return -1; }
	ssize_t n;
	do { n = read(pipefd[0], &child_errno, sizeof(child_errno)); } while (n < 0 && errno == EINTR);
	close(pipefd[0]);
	if (n != 0) {
		if (n > 0) errno = child_errno;
		int saved = errno;
		kill(pid, SIGKILL);
		waitpid(pid, NULL, 0);
		errno = saved;
		return -1;
	}
	/* CLOEXEC EOF acknowledges successful exec, not application readiness. */
	return pid;
}
static void usage(const char *name)
{
	fprintf(stderr, "Usage: %s [options] -- COMMAND [ARG...]\n"
		"       %s [options] --pid PID\n"
		"  --period-ms N     kernel sweep period (default 10000)\n"
		"  --sweeps N        stop after N completed sweeps (default until exit)\n"
		"  --duration-sec N  stop monitoring after N seconds\n"
		"  --once            one synchronous sweep, then stop\n"
		"  --inventory       scan-only session; do not ARM\n"
		"  --dirty-observe   sample hardware dirty bits; never ARM\n"
		"  --dirty-qualify   ARM only after a valid quiet interval\n"
		"  --quiet-ms N      required quiet interval (default 10000, minimum 10)\n"
		"  --max-pages N     active-page bound (default 8388608)\n"
		"  --max-history N   address-history bound (default 8388608)\n"
		"  --poll-ms N       reporting interval (default 1000)\n"
		"JSONL goes to stdout; launched target stdout/stderr goes to stderr.\n"
		"Launch disables THP before exec. Attach never changes target policy.\n"
		"On a normal limit, monitoring stops and a launched target is allowed to finish.\n", name, name);
}
int main(int argc, char **argv)
{
	static const struct option opts[] = {
		{ "pid", 1, 0, 'p' }, { "period-ms", 1, 0, 'i' },
		{ "sweeps", 1, 0, 'n' }, { "duration-sec", 1, 0, 'd' },
		{ "once", 0, 0, '1' }, { "inventory", 0, 0, 's' },
		{ "max-pages", 1, 0, 'm' }, { "max-history", 1, 0, 'H' },
		{ "dirty-observe", 0, 0, 'o' }, { "dirty-qualify", 0, 0, 'q' },
		{ "quiet-ms", 1, 0, 'Q' },
		{ "poll-ms", 1, 0, 'P' }, { "help", 0, 0, 'h' }, { 0 }
	};
	struct cmdp_monitor_attach a;
	struct cmdp_monitor_status s;
	INIT_REQ(a);
	a.max_pages = 8388608;
	a.max_history = 8388608;
	uint64_t limit = 0, seconds = 0, seen = 0;
	uint64_t launch_ns = 0, exec_ack_ns = 0;
	pid_t pid = 0;
	int fd = -1, pidfd = -1, result = 1, opt, poll_ms = 1000;
	bool own = false, once = false, attached = false, quiet_set = false;
	struct cmdp_monitor_dirty_config dirty_config;
	INIT_REQ(dirty_config);
	while ((opt = getopt_long(argc, argv, "+", opts, NULL)) != -1) {
		switch (opt) {
		case 'p': pid = (pid_t)number(optarg, INT_MAX); break;
		case 'i': a.period_ms = number(optarg, UINT32_MAX); break;
		case 'n': limit = number(optarg, UINT64_MAX); break;
		case 'd': seconds = number(optarg, 86400 * 365); break;
		case '1': once = true; break;
		case 's': a.flags |= CMDP_MONITOR_F_SCAN_ONLY; break;
		case 'o':
		case 'q':
			if (dirty_mode) { fprintf(stderr, "choose one dirty mode\n"); return 2; }
			dirty_mode = opt == 'o' ? CMDP_MON_DIRTY_OBSERVE : CMDP_MON_DIRTY_QUALIFY;
			break;
		case 'Q': dirty_config.quiet_ms = number(optarg, UINT32_MAX); quiet_set = true; break;
		case 'm': a.max_pages = number(optarg, UINT32_MAX); break;
		case 'H': a.max_history = number(optarg, UINT32_MAX); break;
		case 'P': poll_ms = number(optarg, 60000); break;
		case 'h': usage(argv[0]); return 0;
		default: usage(argv[0]); return 2;
		}
	}
	if (!poll_ms || (pid && optind != argc) || (!pid && optind == argc) ||
	    (dirty_mode && (a.flags & CMDP_MONITOR_F_SCAN_ONLY)) ||
	    (quiet_set && (!dirty_mode || dirty_config.quiet_ms < 10))) {
		usage(argv[0]); return 2;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	struct sigaction sa = { .sa_handler = on_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	fd = open(CMDP_MONITOR_DEVICE, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror("open cmdp_monitor"); goto out; }
	if (!pid) {
		launch_ns = now_ns();
		pid = launch(&argv[optind]);
		if (pid < 0) { perror("launch target"); goto out; }
		exec_ack_ns = now_ns();
		own = true;
	}
	pidfd = syscall(SYS_pidfd_open, pid, 0);
	if (pidfd < 0) { perror("pidfd_open"); goto out; }
	a.pidfd = pidfd;
	if (ioctl(fd, CMDP_MON_ATTACH, &a)) { perror("CMDP_MON_ATTACH"); goto out; }
	attached = true;
	if (dirty_mode) {
		dirty_config.mode = dirty_mode;
		if (ioctl(fd, CMDP_MON_DIRTY_CONFIG, &dirty_config)) { perror("CMDP_MON_DIRTY_CONFIG"); goto out; }
	}
	printf("{\"event\":\"controller\",\"pid\":%d,\"launched\":%s,\"thp_disabled_before_exec\":%s,\"version\":%u,\"launch_ns\":%" PRIu64 ",\"exec_ack_ns\":%" PRIu64 "}\n",
	       pid, own ? "true" : "false", own ? "true" : "false", CMDP_MONITOR_VERSION, launch_ns, exec_ack_ns);
	uint64_t begin = now_ns();
	if (ioctl(fd, once ? CMDP_MON_SCAN_ONCE : CMDP_MON_START)) {
		perror("start monitor"); goto out;
	}
	result = 0;
	for (;;) {
		if (status_read(fd, &s, "status")) { perror("status"); result = 1; break; }
		if (progress_read(fd)) { perror("progress"); result = 1; break; }
		if (s.passes_completed > seen) {
			cohorts(fd, seen + 1, s.passes_completed, "sweep");
			seen = s.passes_completed;
		}
		cohorts(fd, 1, s.passes_completed, "cohort_snapshot");
		if (once || interrupted || s.state == CMDP_MON_STOPPED ||
		    (limit && seen >= limit) || (seconds && now_ns() - begin >= seconds * 1000000000ULL))
			break;
		struct pollfd p = { .fd = pidfd, .events = POLLIN };
		int n = poll(&p, 1, poll_ms);
		if (n > 0) break;
		if (n < 0 && errno != EINTR) { perror("poll"); result = 1; break; }
	}
out:
	if (attached) {
		struct stop_reporter reporter = { .fd = fd, .poll_ms = poll_ms, .seen = seen };
		pthread_t reporter_thread;
		bool reporting = false;
		/* This is the observation boundary, before any STOP cleanup. */
		if (status_read(fd, &s, "observation_end")) {
			perror("observation end status"); result = 1;
		} else {
			cohorts(fd, 1, s.passes_started, "cohort_observation_end");
		}
		if (progress_read(fd)) { perror("progress"); result = 1; }
		if (pipe2(reporter.wake, O_CLOEXEC)) {
			perror("reporter pipe"); result = 1;
		} else {
			int err = pthread_create(&reporter_thread, NULL, report_while_stopping, &reporter);
			if (err) {
				fprintf(stderr, "start stop reporter: %s\n", strerror(err));
				close(reporter.wake[0]); close(reporter.wake[1]);
				result = 1;
			} else {
				reporting = true;
			}
		}
		uint64_t stop_start = now_ns();
		printf("{\"event\":\"stop_request\",\"now_ns\":%" PRIu64 "}\n", stop_start);
		int stop_rc = ioctl(fd, CMDP_MON_STOP);
		int stop_errno = stop_rc ? errno : 0;
		uint64_t stop_end = now_ns();
		printf("{\"event\":\"stop_return\",\"now_ns\":%" PRIu64 ",\"elapsed_ns\":%" PRIu64 ",\"errno\":%d}\n",
		       stop_end, stop_end - stop_start, stop_errno);
		if (stop_rc) {
			fprintf(stderr, "stop monitor: %s\n", strerror(stop_errno)); result = 1;
		}
		if (reporting) {
			/* Closing the writer wakes poll with HUP, including after signals. */
			close(reporter.wake[1]);
			pthread_join(reporter_thread, NULL);
			close(reporter.wake[0]);
			if (reporter.failed) result = 1;
		}
		if (progress_read(fd)) { perror("final progress"); result = 1; }
		if (status_read(fd, &s, "final")) { perror("final status"); result = 1; }
		else {
			cohorts(fd, 1, s.passes_started, "cohort_final");
			if (s.active || s.resource_failures || s.reports_dropped ||
			    s.stop_reason == CMDP_MON_ERROR || s.stop_reason == CMDP_MON_FORK ||
			    s.stop_reason == CMDP_MON_EXEC || s.stop_reason == CMDP_MON_THP_CHANGE ||
			    s.stop_reason == CMDP_MON_SCOPE_CHANGE)
				result = 1;
		}
	}
	if (fd >= 0 && close(fd)) result = 1;
	if (pidfd >= 0) close(pidfd);
	if (own) {
		int child_status = 0;
		/* Lifecycle stops report incomplete monitoring, but must not kill the app. */
		if (interrupted || (!attached && result)) kill(pid, SIGTERM);
		while (waitpid(pid, &child_status, 0) < 0) {
			if (errno != EINTR) { result = 1; break; }
			if (interrupted) kill(pid, SIGTERM);
		}
		int child_exit = WIFEXITED(child_status) ? WEXITSTATUS(child_status) :
			128 + (WIFSIGNALED(child_status) ? WTERMSIG(child_status) : 0);
		printf("{\"event\":\"target_exit\",\"exit_code\":%d}\n", child_exit);
		if (child_exit) result = child_exit;
	}
	if (interrupted) result = 128 + interrupted;
	printf("{\"event\":\"controller_exit\",\"exit_code\":%d}\n", result);
	return result;
}
