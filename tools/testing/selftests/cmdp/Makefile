# SPDX-License-Identifier: GPL-2.0
CFLAGS += -O2 -Wall -Wextra -pthread
LDLIBS += -pthread
TEST_GEN_PROGS := cmdp_test cmdp_bench cmdp_monitor_test cmdp_range_test cmdp_dirty_test cmdp_recovery_test
TEST_GEN_PROGS_EXTENDED := cmdp_monitor cmdp_fixture
TEST_FILES := scripts MONITOR.md RANGE-REGRESSION.md DIRTY-SAMPLING.md RECOVERY.md
include ../lib.mk
