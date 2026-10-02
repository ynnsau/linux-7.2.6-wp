/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM cmdp

#if !defined(_TRACE_CMDP_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_CMDP_H

#include <linux/tracepoint.h>

TRACE_EVENT(cmdp_event,

	TP_PROTO(unsigned long pfn, u64 generation, int old_state, int new_state,
		 const char *reason, unsigned int aliases, int error),
	TP_ARGS(pfn, generation, old_state, new_state, reason, aliases, error),
	TP_STRUCT__entry(
		__field(unsigned long, pfn)
		__field(u64, generation)
		__field(int, old_state)
		__field(int, new_state)
		__string(reason, reason)
		__field(unsigned int, aliases)
		__field(int, error)
	),
	TP_fast_assign(
		__entry->pfn = pfn;
		__entry->generation = generation;
		__entry->old_state = old_state;
		__entry->new_state = new_state;
		__assign_str(reason);
		__entry->aliases = aliases;
		__entry->error = error;
	),
	TP_printk("pfn=%lx gen=%llu state=%d->%d reason=%s aliases=%u error=%d",
		  __entry->pfn, __entry->generation, __entry->old_state,
		  __entry->new_state, __get_str(reason), __entry->aliases,
		  __entry->error)
);

#endif
#include <trace/define_trace.h>
