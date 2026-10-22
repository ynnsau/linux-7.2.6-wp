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

/* One bounded summary per external range, not one record per page. */
TRACE_EVENT(cmdp_invalidation,
	TP_PROTO(u64 session_id, unsigned int source, unsigned long start,
		 unsigned long end, u64 revoked, u64 observations, u64 histories,
		 u64 elapsed_ns, int error),
	TP_ARGS(session_id, source, start, end, revoked, observations, histories,
		elapsed_ns, error),
	TP_STRUCT__entry(
		__field(u64, session_id)
		__field(unsigned int, source)
		__field(unsigned long, start)
		__field(unsigned long, end)
		__field(u64, revoked)
		__field(u64, observations)
		__field(u64, histories)
		__field(u64, elapsed_ns)
		__field(int, error)
	),
	TP_fast_assign(
		__entry->session_id = session_id;
		__entry->source = source;
		__entry->start = start;
		__entry->end = end;
		__entry->revoked = revoked;
		__entry->observations = observations;
		__entry->histories = histories;
		__entry->elapsed_ns = elapsed_ns;
		__entry->error = error;
	),
	TP_printk("session=%llu source=%u start=%lx end=%lx revoked=%llu observations=%llu histories=%llu ns=%llu error=%d",
		__entry->session_id, __entry->source, __entry->start, __entry->end,
		__entry->revoked, __entry->observations, __entry->histories,
		__entry->elapsed_ns, __entry->error)
);

#endif
#include <trace/define_trace.h>
