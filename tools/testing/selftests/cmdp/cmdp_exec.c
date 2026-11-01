// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

/* Apply the complete process policy before the workload creates its mappings.
 * This policy survives exec and fork, without changing the global THP policy.
 */
int main(int argc, char **argv)
{
	int policy, saved_errno;

	if (argc < 3 || strcmp(argv[1], "--") || !argv[2][0]) {
		fprintf(stderr, "Usage: %s -- COMMAND [ARG ...]\n", argv[0]);
		return 2;
	}

	if (prctl(PR_SET_THP_DISABLE, 1UL, 0UL, 0UL, 0UL) < 0) {
		perror("cmdp_exec: PR_SET_THP_DISABLE");
		return 125;
	}
	policy = prctl(PR_GET_THP_DISABLE, 0UL, 0UL, 0UL, 0UL);
	if (policy < 0) {
		perror("cmdp_exec: PR_GET_THP_DISABLE");
		return 125;
	}
	if (policy != 1) {
		fprintf(stderr, "cmdp_exec: complete THP disable required (got %d)\n",
			policy);
		return 125;
	}

	execvp(argv[2], &argv[2]);
	saved_errno = errno;
	fprintf(stderr, "cmdp_exec: exec %s: %s\n", argv[2],
		strerror(saved_errno));
	return saved_errno == ENOENT ? 127 : 126;
}
