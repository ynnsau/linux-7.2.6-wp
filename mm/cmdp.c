// SPDX-License-Identifier: GPL-2.0
/* Restricted native x86 CMD-P experiment. No device protocol is implemented. */
#include <linux/capability.h>
#include <linux/cmdp.h>
#include <linux/completion.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/init.h>
#include <linux/math64.h>
#include <linux/overflow.h>
#include <linux/pid.h>
#include <linux/ptrace.h>
#include <linux/sched/signal.h>
#include <linux/workqueue.h>
#include <linux/miscdevice.h>
#include <linux/ktime.h>
#include <linux/mm.h>
#include <linux/huge_mm.h>
#include <linux/mmu_notifier.h>
#include <linux/module.h>
#include <linux/pagemap.h>
#include <linux/refcount.h>
#include <linux/rbtree.h>
#include <linux/rmap.h>
#include <linux/sched/mm.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/swap.h>
#include <linux/leafops.h>
#include <linux/uaccess.h>
#include <linux/userfaultfd_k.h>
#include <linux/xarray.h>
#include <uapi/linux/cmdp.h>

#include <asm/tlbflush.h>

#include "internal.h"

#define CREATE_TRACE_POINTS
#include <trace/events/cmdp.h>

#define CMDP_MAX_ALIASES 16

enum cmdp_state {
	CMDP_ARMING,
	CMDP_ACTIVE,
	CMDP_REVOKING,
	CMDP_TERMINAL,
};

enum cmdp_hw_op { CMDP_PUBLISH, CMDP_REVOKE };
enum cmdp_stat {
	ARMS, PUBLISHES, REVOKES, WAITERS, REJECTS, ENTRIES, MANAGED,
	ISOLATED, STALE, ARM_NS, REVOKE_NS,
	DIRTY_FLUSH_BATCHES, DIRTY_FLUSH_PAGES, ARM_FLUSH_BATCHES,
	ARM_FLUSH_PAGES, SCAN_ACTIVE_SKIPS, SCAN_PAGE_LOOKUPS,
	TEST_BATCH_WAITS, TEST_BATCH_WAITING, CMDP_NR_STATS,
};
static const char * const cmdp_stat_names[] = {
	"arms", "publishes", "revokes", "waiters", "rejects", "entries",
	"managed", "isolated", "stale", "arm_ns", "revoke_ns",
	"dirty_flush_batches", "dirty_flush_pages", "arm_flush_batches",
	"arm_flush_pages", "scan_active_skips", "scan_page_lookups",
	"test_batch_waits", "test_batch_waiting",
};
static atomic64_t cmdp_stats[CMDP_NR_STATS];
static atomic64_t cmdp_generation = ATOMIC64_INIT(0);
static DEFINE_XARRAY(cmdp_entries);

struct cmdp_session;
struct cmdp_monitor;
struct cmdp_history;
struct cmdp_cohort;
struct cmdp_alias {
	unsigned long addr;
	struct rb_node node;
	struct cmdp_entry *entry;
	/* Only dereferenced while arming under mmap write lock. */
	struct vm_area_struct *vma;
};

struct cmdp_entry {
	struct page *page;
	unsigned long pfn;
	struct mm_struct *mm;
	struct cmdp_session *session;
	struct cmdp_history *history;
	struct cmdp_cohort *cohort;
	bool accounted;
	bool rearm;
	atomic_t state;
	u64 generation;
	refcount_t refs;
	struct completion terminal;
	struct completion arm_done;
	struct completion hw_completion;
	enum cmdp_hw_op hw_op;
	bool hw_pending;
	int hw_result;
	bool isolated;
	bool registered;
	bool indexed;
	unsigned int nr_aliases;
	unsigned int protected;
	struct cmdp_alias aliases[CMDP_MAX_ALIASES];
};

struct cmdp_session {
	struct mmu_notifier notifier;
	struct mm_struct *mm;
	struct cmdp_monitor *monitor;
	refcount_t refs;
	atomic_t open_files;
	struct mutex commands;
	/* Registry -> address_lock is the only nested lock order. Range lookup
	 * never takes the global PFN registry lock or walks unrelated entries.
	 * Every alias stays indexed until the extra page ownership is gone.
	 */
	spinlock_t address_lock;
	struct rb_root addresses;
	u64 lookup_calls, lookup_nodes, lookup_max_nodes, index_lock_max_ns;
	u64 write_calls, write_ns, write_max_ns;
	/* The registry lock protects closing and cmdp_owner. */
	bool closing;
	bool notifier_registered;
#ifdef CONFIG_CMDP_TEST
	struct page *test_page;
	bool test_pinned;
	unsigned int fail_after;
	unsigned int revoke_delay_ms;
#endif
};
static struct cmdp_session *cmdp_owner;

#define CMDP_MON_BATCH 64
#define CMDP_MON_MAX_COHORTS 4096
#define CMDP_MON_MAX_PAGES (16U * 1024 * 1024)
struct cmdp_history {
	struct cmdp_monitor_page page;
	struct cmdp_monitor_dirty_page dirty;
	u64 observation_seq;
	bool continuous;
};
struct cmdp_cohort {
	struct cmdp_monitor_pass report;
};

/* Transient ownership is bounded by one scan chunk. Never retain a PTE
 * pointer or a PTL across pages. Only trylock additional folios, so a batch
 * cannot wait for a folio while holding an earlier one.
 */
struct cmdp_dirty_reset {
	struct page *page;
	struct cmdp_history *history;
	u64 seq;
	bool valid, dirty;
};
struct cmdp_arm_work {
	struct cmdp_entry *entry;
	u64 start;
	const char *why;
	int error;
	bool folio_locked;
};
struct cmdp_scan_batch {
	unsigned long start, end;
	unsigned int nr_dirty, nr_arm;
	struct cmdp_dirty_reset dirty[CMDP_MON_BATCH];
	struct cmdp_arm_work arm[CMDP_MON_BATCH];
	struct cmdp_monitor_pass counts;
	u64 lookups, active_skips;
};
struct cmdp_monitor {
	/* ctl serializes setup; scan serializes worker and synchronous scans. */
	struct mutex ctl;
	struct mutex scan;
	spinlock_t lock;
	struct delayed_work work;
	struct cmdp_session *session;
	struct xarray history;
	struct xarray cohorts;
	struct cmdp_monitor_status status;
	struct cmdp_monitor_progress progress;
	struct cmdp_monitor_dirty_status dirty;
	struct cmdp_monitor_diagnostics diagnostics;
	struct cmdp_cohort *current_cohort;
	u64 next_lifetime;
	u64 account_ns;
	u64 next_deadline;
	u32 max_pages, max_history;
	bool scanning;
	bool stop_snapshot;
#ifdef CONFIG_CMDP_TEST
	struct cmdp_monitor_test_batch test_batch;
#endif
};
static atomic64_t cmdp_monitor_ids = ATOMIC64_INIT(0);
static void cmdp_monitor_terminal(struct cmdp_entry *e, const char *reason);
static void cmdp_monitor_armed(struct cmdp_entry *e);
static void cmdp_monitor_invalidate_history(struct cmdp_session *s,
		unsigned long start, unsigned long end);
static void cmdp_monitor_invalidate_history_count(struct cmdp_session *s,
		unsigned long start, unsigned long end, u64 *observations, u64 *histories);
static void cmdp_monitor_mark_stop(struct cmdp_monitor *m, unsigned int reason);
static void cmdp_monitor_snapshot_stop(struct cmdp_monitor *m);
static void cmdp_dirty_invalidate_locked(struct cmdp_monitor *m,
		struct cmdp_history *h);
static bool cmdp_dirty_admit(struct cmdp_entry *e, pte_t pte);
static struct page *cmdp_monitor_get_page(struct vm_area_struct *vma,
		unsigned long addr, bool *resident, int *skip);


static void cmdp_session_put(struct cmdp_session *s)
{
	if (refcount_dec_and_test(&s->refs)) {
		mmdrop(s->mm);
		kfree(s);
	}
}

void cmdp_put(struct cmdp_entry *e)
{
	if (refcount_dec_and_test(&e->refs)) {
		WARN_ON_ONCE(e->registered || e->indexed || e->page || e->isolated);
		atomic64_dec(&cmdp_stats[ENTRIES]);
		cmdp_session_put(e->session);
		kfree(e);
	}
}

struct cmdp_entry *cmdp_lookup(struct page *page)
{
	struct cmdp_entry *e = NULL;
	unsigned long flags;

	xa_lock_irqsave(&cmdp_entries, flags);
	if (PageCmdp(page)) {
		e = xa_load(&cmdp_entries, page_to_pfn(page));
		if (WARN_ON_ONCE(!e || e->page != page ||
				 atomic_read(&e->state) == CMDP_TERMINAL))
			e = ERR_PTR(-EIO);
		else
			refcount_inc(&e->refs);
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	return e;
}

bool cmdp_mm_active(struct mm_struct *mm)
{
	unsigned long flags;
	bool active;

	mmap_assert_write_locked(mm);
	xa_lock_irqsave(&cmdp_entries, flags);
	active = false;
	if (cmdp_owner && cmdp_owner->mm == mm) {
		spin_lock(&cmdp_owner->address_lock);
		active = !RB_EMPTY_ROOT(&cmdp_owner->addresses);
		spin_unlock(&cmdp_owner->address_lock);
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	return active;
}

/* Backend completions carry identities, never an unreferenced entry pointer. */
static bool cmdp_hw_complete(unsigned long pfn, u64 generation,
			     enum cmdp_hw_op op, int result)
{
	struct cmdp_entry *e;
	unsigned long flags;
	bool accepted = false;

	xa_lock_irqsave(&cmdp_entries, flags);
	e = xa_load(&cmdp_entries, pfn);
	if (e && e->generation == generation && e->hw_pending &&
	    e->hw_op == op && atomic_read(&e->state) ==
	    (op == CMDP_PUBLISH ? CMDP_ARMING : CMDP_REVOKING)) {
		e->hw_result = result;
		e->hw_pending = false;
		complete_all(&e->hw_completion);
		accepted = true;
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	if (!accepted)
		atomic64_inc(&cmdp_stats[STALE]);
	return accepted;
}

static int cmdp_hw_request(struct cmdp_entry *e, enum cmdp_hw_op op)
{
	unsigned long flags;

	xa_lock_irqsave(&cmdp_entries, flags);
	if (xa_load(&cmdp_entries, e->pfn) != e || e->hw_pending) {
		xa_unlock_irqrestore(&cmdp_entries, flags);
		return -ESTALE;
	}
	reinit_completion(&e->hw_completion);
	e->hw_op = op;
	e->hw_pending = true;
	xa_unlock_irqrestore(&cmdp_entries, flags);

	trace_cmdp_event(e->pfn, e->generation, atomic_read(&e->state),
		atomic_read(&e->state), op == CMDP_PUBLISH ? "publish" : "revoke",
		e->protected, 0);
	atomic64_inc(&cmdp_stats[op == CMDP_PUBLISH ? PUBLISHES : REVOKES]);
#ifdef CONFIG_CMDP_TEST
	if (op == CMDP_REVOKE && READ_ONCE(e->session->revoke_delay_ms))
		msleep(READ_ONCE(e->session->revoke_delay_ms));
#endif
	/* Dummy backend: no hardware access; exercise the completion boundary. */
	cmdp_hw_complete(e->pfn, e->generation, op, 0);
	wait_for_completion(&e->hw_completion);
	return e->hw_result;
}

static int cmdp_hw_publish(struct cmdp_entry *e)
{
	/* TODO: enable PFN/generation only after alias protection and ordering. */
	return cmdp_hw_request(e, CMDP_PUBLISH);
}

static int cmdp_hw_revoke(struct cmdp_entry *e)
{
	/* TODO: stop new pushes and drain every queued/in-flight old push. */
	return cmdp_hw_request(e, CMDP_REVOKE);
}

/* Caller holds address_lock. No allocation or sleeping in this index. */
static int cmdp_index_insert(struct cmdp_entry *e)
{
	struct rb_root *root = &e->session->addresses;
	unsigned int i;

	for (i = 0; i < e->nr_aliases; i++) {
		struct cmdp_alias *alias = &e->aliases[i];
		struct rb_node **link = &root->rb_node, *parent = NULL;

		while (*link) {
			struct cmdp_alias *other = rb_entry(*link, struct cmdp_alias, node);

			parent = *link;
			if (alias->addr < other->addr)
				link = &(*link)->rb_left;
			else if (alias->addr > other->addr)
				link = &(*link)->rb_right;
			else
				goto collision;
		}
		alias->entry = e;
		rb_link_node(&alias->node, parent, link);
		rb_insert_color(&alias->node, root);
	}
	e->indexed = true;
	return 0;
collision:
	while (i)
		rb_erase(&e->aliases[--i].node, root);
	return -EEXIST;
}

static void cmdp_index_remove(struct cmdp_entry *e)
{
	unsigned int i;

	for (i = 0; i < e->nr_aliases; i++)
		rb_erase(&e->aliases[i].node, &e->session->addresses);
	e->indexed = false;
}

/* Exactly one teardown owner; no MM locks are acquired here. */
static void cmdp_finish(struct cmdp_entry *e, const char *reason)
{
	struct page *page = e->page;
	struct cmdp_session *s = e->session;
	unsigned long flags;
	u64 start;
	int old;

	xa_lock_irqsave(&cmdp_entries, flags);
	if (WARN_ON_ONCE(xa_load(&cmdp_entries, e->pfn) != e ||
			((struct cmdp_entry *)xa_load(&cmdp_entries, e->pfn))->generation !=
			e->generation)) {
		xa_unlock_irqrestore(&cmdp_entries, flags);
		return;
	}
	old = atomic_read(&e->state);
	ClearPageCmdp(page);
	__xa_erase(&cmdp_entries, e->pfn);
	e->registered = false;
	atomic_set(&e->state, CMDP_TERMINAL);
	atomic64_dec(&cmdp_stats[MANAGED]);
	xa_unlock_irqrestore(&cmdp_entries, flags);

	cmdp_monitor_terminal(e, reason);
	trace_cmdp_event(e->pfn, e->generation, old, CMDP_TERMINAL,
			reason, e->protected, 0);
	if (e->isolated) {
		folio_putback_lru(page_folio(page));
		e->isolated = false;
		atomic64_dec(&cmdp_stats[ISOLATED]);
	}
	e->page = NULL;
	put_page(page);
	/* Shutdown must still find this entry until page ownership is gone. */
	spin_lock_irqsave(&s->address_lock, flags);
	start = ktime_get_ns();
	cmdp_index_remove(e);
	s->index_lock_max_ns = max(s->index_lock_max_ns, ktime_get_ns() - start);
	spin_unlock_irqrestore(&s->address_lock, flags);
	/* Waiters must not refault until the extra page ownership is gone. */
	complete_all(&e->terminal);
	cmdp_put(e); /* registry */
	cmdp_put(e); /* session address index */
}

static int cmdp_revoke_counted(struct cmdp_entry *e, const char *reason, bool *owner)
{
	int state, ret;
	u64 start;

	state = atomic_cmpxchg(&e->state, CMDP_ACTIVE, CMDP_REVOKING);
	if (state == CMDP_ARMING) {
		/* Only session shutdown can join arming; faults cannot get here. */
		wait_for_completion(&e->arm_done);
		return cmdp_revoke_counted(e, reason, owner);
	}
	if (state != CMDP_ACTIVE) {
		if (state == CMDP_REVOKING) {
			atomic64_inc(&cmdp_stats[WAITERS]);
			trace_cmdp_event(e->pfn, e->generation, state, state,
					"revoke_waiter", e->protected, 0);
		}
		wait_for_completion(&e->terminal);
		return 0;
	}

	if (owner)
		*owner = true;
	start = ktime_get_ns();
	trace_cmdp_event(e->pfn, e->generation, CMDP_ACTIVE, CMDP_REVOKING,
			reason, e->protected, 0);
	ret = cmdp_hw_revoke(e);
	if (ret) {
		/* Fail closed. A real backend must recover/drain before completing. */
		trace_cmdp_event(e->pfn, e->generation, CMDP_REVOKING,
				CMDP_REVOKING, "revoke_failed", e->protected, ret);
		return ret;
	}
	cmdp_finish(e, reason);
	atomic64_add(ktime_get_ns() - start, &cmdp_stats[REVOKE_NS]);
	return 0;
}

static int cmdp_revoke(struct cmdp_entry *e, const char *reason)
{
	return cmdp_revoke_counted(e, reason, NULL);
}

int cmdp_prepare_write(struct cmdp_entry *e)
{
	struct cmdp_session *s = e->session;
	unsigned long flags;
	u64 start = ktime_get_ns(), elapsed;
	int ret;

	/* ARMING owns mmap write and every alias's VMA write exclusion. */
	if (WARN_ON_ONCE(atomic_read(&e->state) == CMDP_ARMING))
		return -EBUSY;
	ret = cmdp_revoke(e, "write");
	elapsed = ktime_get_ns() - start;
	/* The caller's entry reference pins s even if STOP has freed m after
	 * terminal completion. Keep these post-completion counters in s.
	 */
	spin_lock_irqsave(&s->address_lock, flags);
	s->write_calls++;
	s->write_ns += elapsed;
	s->write_max_ns = max(s->write_max_ns, elapsed);
	spin_unlock_irqrestore(&s->address_lock, flags);
	return ret;
}

static struct cmdp_entry *cmdp_find_range(struct cmdp_session *s,
		unsigned long start, unsigned long end)
{
	struct cmdp_entry *found = NULL;
	struct rb_node *node;
	struct cmdp_alias *candidate = NULL;
	unsigned long flags;
	u64 begin, nodes = 0;

	if (start >= end)
		return NULL;
	/* All aliases are page aligned. Lower-bound lookup includes a page
	 * partially overlapping an unaligned start, without addr+PAGE overflow.
	 */
	start &= PAGE_MASK;
	spin_lock_irqsave(&s->address_lock, flags);
	begin = ktime_get_ns();
	node = s->addresses.rb_node;
	while (node) {
		struct cmdp_alias *alias = rb_entry(node, struct cmdp_alias, node);

		nodes++;
		if (alias->addr >= start) {
			candidate = alias;
			node = node->rb_left;
		} else {
			node = node->rb_right;
		}
	}
	if (candidate && candidate->addr < end) {
		found = candidate->entry;
		refcount_inc(&found->refs);
	}
	s->lookup_calls++;
	s->lookup_nodes += nodes;
	s->lookup_max_nodes = max(s->lookup_max_nodes, nodes);
	s->index_lock_max_ns = max(s->index_lock_max_ns, ktime_get_ns() - begin);
	spin_unlock_irqrestore(&s->address_lock, flags);
	return found;
}

static u64 cmdp_range_begin(struct cmdp_session *s)
{
	struct cmdp_monitor *m = s->monitor;
	unsigned long flags;
	u64 now = ktime_get_ns();

	if (m) {
		spin_lock_irqsave(&m->lock, flags);
		m->progress.range_calls++;
		m->progress.range_inflight++;
		spin_unlock_irqrestore(&m->lock, flags);
	}
	return now;
}

/* These addresses are diagnostic labels, never session identities. The event
 * remains PROTECTION_VMA, so the own-PROTECTION_PAGE exemption cannot match.
 */
static char cmdp_numa_owner, cmdp_protection_token, cmdp_test_owner;

void *cmdp_protection_owner(bool prot_numa)
{
	return prot_numa ? &cmdp_numa_owner : &cmdp_protection_token;
}

static unsigned int cmdp_invalidation_source(const struct mmu_notifier_range *range)
{
	if (range->event == MMU_NOTIFY_PROTECTION_VMA) {
		if (range->owner == &cmdp_numa_owner)
			return CMDP_MON_SOURCE_NUMA;
		if (range->owner == &cmdp_protection_token)
			return CMDP_MON_SOURCE_PROTECTION;
		if (range->owner == &cmdp_test_owner)
			return CMDP_MON_SOURCE_TEST;
	}
	if (range->event == MMU_NOTIFY_UNMAP)
		return CMDP_MON_SOURCE_UNMAP;
	if (range->event == MMU_NOTIFY_CLEAR)
		return CMDP_MON_SOURCE_CLEAR;
	return CMDP_MON_SOURCE_UNKNOWN;
}

static void cmdp_range_end(struct cmdp_session *s, u64 start,
		unsigned int source, unsigned long lo, unsigned long hi,
		u64 revoked, u64 observations, u64 histories, int error)
{
	struct cmdp_monitor *m = s->monitor;
	struct cmdp_monitor_invalidation_stats *d;
	unsigned long flags;
	u64 elapsed = ktime_get_ns() - start, session_id = 0;
	bool emit = false;

	if (m) {
		spin_lock_irqsave(&m->lock, flags);
		m->progress.range_inflight--;
		m->progress.range_ns += elapsed;
		m->progress.range_max_ns = max(m->progress.range_max_ns, elapsed);
		if (source >= CMDP_MON_NR_SOURCES)
			source = CMDP_MON_SOURCE_UNKNOWN;
		d = &m->diagnostics.source[source];
		d->calls++;
		d->arm_revoked += revoked;
		d->observations_reset += observations;
		d->histories_reset += histories;
		d->elapsed_ns += elapsed;
		d->max_ns = max(d->max_ns, elapsed);
		if (trace_cmdp_invalidation_enabled()) {
			if (m->diagnostics.trace_records < 4096) {
				m->diagnostics.trace_records++;
				emit = true;
			} else {
				m->diagnostics.trace_records_dropped++;
			}
		}
		session_id = m->status.session_id;
		spin_unlock_irqrestore(&m->lock, flags);
	}
	if (emit)
		trace_cmdp_invalidation(session_id, source, lo, hi, revoked,
				observations, histories, elapsed, error);
}

static int cmdp_invalidate(struct mmu_notifier *mn,
			   const struct mmu_notifier_range *range)
{
	struct cmdp_session *s = container_of(mn, struct cmdp_session, notifier);
	struct cmdp_entry *e;
	u64 start, revoked = 0, observations = 0, histories = 0;
	bool owner;
	int ret = 0;

	/* Our own alias protection notification, outside PTL/rmap locks. */
	if (range->owner == s && range->event == MMU_NOTIFY_PROTECTION_PAGE)
		return 0;

	start = cmdp_range_begin(s);
	cmdp_monitor_invalidate_history_count(s, range->start, range->end,
					      &observations, &histories);
	while ((e = cmdp_find_range(s, range->start, range->end))) {
		if (!mmu_notifier_range_blockable(range)) {
			cmdp_put(e);
			ret = -EAGAIN;
			break;
		}
		/*
		 * Supported zap paths hold mmap/VMA exclusion, not rmap locks.
		 * LRU isolation and v1 exclusions prevent overlapping rmap-driven
		 * migration/reclaim. BLOCKABLE alone is not sufficient for those.
		 */
		owner = false;
		ret = cmdp_revoke_counted(e, "invalidate", &owner);
		revoked += owner;
		if (ret) {
			/* Blockable invalidation cannot veto destruction. Fail closed. */
			wait_for_completion(&e->terminal);
			ret = 0;
		}
		cmdp_put(e);
		cond_resched();
	}
	cmdp_range_end(s, start, cmdp_invalidation_source(range),
		       range->start, range->end, revoked, observations, histories, ret);
	return ret;
}

static void cmdp_release_mm(struct mmu_notifier *mn, struct mm_struct *mm)
{
	struct cmdp_session *s = container_of(mn, struct cmdp_session, notifier);
	struct cmdp_entry *e;
	unsigned long flags;

	if (s->monitor) {
		cmdp_monitor_mark_stop(s->monitor, CMDP_MON_TARGET_EXIT);
		cmdp_monitor_snapshot_stop(s->monitor);
	}
	xa_lock_irqsave(&cmdp_entries, flags);
	s->closing = true;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	while ((e = cmdp_find_range(s, 0, ULONG_MAX))) {
		if (cmdp_revoke(e, "release"))
			wait_for_completion(&e->terminal);
		cmdp_put(e);
		cond_resched();
	}
	if (s->monitor) {
		spin_lock_irqsave(&s->monitor->lock, flags);
		s->monitor->status.state = CMDP_MON_STOPPED;
		if (!s->monitor->progress.stop_complete_ns)
			s->monitor->progress.stop_complete_ns = ktime_get_ns();
		spin_unlock_irqrestore(&s->monitor->lock, flags);
	}
}

static const struct mmu_notifier_ops cmdp_notifier_ops = {
	.invalidate_range_start = cmdp_invalidate,
	.release = cmdp_release_mm,
	/* No end callback: nonblockable start can return -EAGAIN. */
};

static bool cmdp_vma_supported(struct vm_area_struct *vma)
{
	return vma_is_anonymous(vma) && !vma->vm_file &&
		(vma->vm_flags & VM_WRITE) &&
		!(vma->vm_flags & (VM_SHARED | VM_MAYSHARE | VM_HUGETLB |
			VM_PFNMAP | VM_MIXEDMAP | VM_IO | VM_MERGEABLE |
			VM_SHADOW_STACK)) &&
		!userfaultfd_armed(vma) &&
		(!IS_ENABLED(CONFIG_TRANSPARENT_HUGEPAGE) ||
		 ((vma->vm_flags & VM_NOHUGEPAGE) ||
		  mm_flags_test(MMF_DISABLE_THP_COMPLETELY, vma->vm_mm)));
}

struct cmdp_walk {
	struct cmdp_entry *entry;
	int error;
};

static bool cmdp_collect_alias(struct folio *folio, struct vm_area_struct *vma,
			       unsigned long addr, void *arg)
{
	struct cmdp_walk *walk = arg;
	struct cmdp_entry *e = walk->entry;
	DEFINE_FOLIO_VMA_WALK(pvmw, folio, vma, addr, PVMW_SYNC);

	/* Conservatively reject even candidate aliases in another mm. */
	if (vma->vm_mm != e->mm || !cmdp_vma_supported(vma)) {
		walk->error = -EOPNOTSUPP;
		return false;
	}
	while (page_vma_mapped_walk(&pvmw)) {
		if (!pvmw.pte || e->nr_aliases == CMDP_MAX_ALIASES) {
			walk->error = -E2BIG;
			page_vma_mapped_walk_done(&pvmw);
			return false;
		}
		e->aliases[e->nr_aliases++] = (struct cmdp_alias) {
			.addr = pvmw.address, .vma = vma,
		};
	}
	return true;
}

static int cmdp_protect_alias(struct cmdp_entry *e, struct cmdp_alias *alias,
		bool batched)
{
	struct vm_area_struct *vma = alias->vma;
	struct mmu_notifier_range range;
	DEFINE_FOLIO_VMA_WALK(pvmw, page_folio(e->page), vma,
			      alias->addr, PVMW_SYNC);
	pte_t pte;
	int ret = -EAGAIN;

	if (!batched) {
		mmu_notifier_range_init_owner(&range, MMU_NOTIFY_PROTECTION_PAGE, 0,
			e->mm, alias->addr, alias->addr + PAGE_SIZE, e->session);
		mmu_notifier_invalidate_range_start(&range);
	}
	if (!page_vma_mapped_walk(&pvmw))
		goto end;
	if (!pvmw.pte || pvmw.address != alias->addr)
		goto unlock;
	pte = ptep_get(pvmw.pte);
	if (!pte_present(pte) || vm_normal_page(vma, alias->addr, pte) != e->page)
		goto unlock;

	/* Last dirty check is under PTL, immediately before protection. A write
	 * after this check is still safe: the existing ARM flush/ownership
	 * protocol applies, and its later write will revoke protection.
	 */
	if (!cmdp_dirty_admit(e, pte))
		goto unlock;

	if (batched) {
		/* Native x86 cmpxchg preserves hardware A/D updates and converts
		 * D to SAVED_DIRTY without creating a CET shadow-stack PTE.
		 * Publication is forbidden until the covering flush completes.
		 */
		ptep_set_wrprotect(e->mm, alias->addr, pvmw.pte);
	} else {
		pte = ptep_clear_flush(vma, alias->addr, pvmw.pte);
		set_pte_at(e->mm, alias->addr, pvmw.pte, pte_wrprotect(pte));
	}
	e->protected++;
	ret = 0;
unlock:
	page_vma_mapped_walk_done(&pvmw);
end:
	if (!batched)
		mmu_notifier_invalidate_range_end(&range);
	return ret;
}

/* Prepare ownership and all aliases, but do not modify PTEs or publish.
 * A supplied monitor probe reference is consumed on every return path.
 */
static int cmdp_arm_prepare(struct cmdp_session *s, unsigned long addr,
		struct page *page, struct cmdp_history *history,
		struct cmdp_cohort *cohort, struct cmdp_arm_work *w)
{
	struct cmdp_entry *e;
	struct vm_area_struct *vma;
	struct folio *folio;
	struct cmdp_walk walk;
	struct rmap_walk_control rwc = { .rmap_one = cmdp_collect_alias };
	unsigned long flags;
	unsigned int i;
	const char *why = "vma";
	int ret;

	*w = (struct cmdp_arm_work) { .start = ktime_get_ns(), .why = "allocation" };
	e = kzalloc_obj(*e);
	if (!e) {
		if (page)
			put_page(page);
		return w->error = -ENOMEM;
	}
	w->entry = e;
	e->mm = s->mm;
	e->session = s;
	e->history = history;
	e->cohort = cohort;
	e->generation = atomic64_inc_return(&cmdp_generation);
	refcount_set(&e->refs, 1);
	refcount_inc(&s->refs);
	atomic64_inc(&cmdp_stats[ENTRIES]);
	atomic_set(&e->state, CMDP_ARMING);
	init_completion(&e->terminal);
	init_completion(&e->arm_done);
	init_completion(&e->hw_completion);
	mmap_assert_write_locked(s->mm);
	if (READ_ONCE(s->closing)) {
		ret = -ESHUTDOWN;
		goto fail;
	}
	vma = vma_lookup(s->mm, addr);
	if (!vma || !cmdp_vma_supported(vma)) {
		ret = -EOPNOTSUPP;
		goto fail;
	}
	why = "nofault";
	if (page) {
		e->page = page;
		page = NULL; /* consume the probe reference, including all failures */
		ret = 1;
	} else if (s->monitor) {
		bool resident;
		int skip;

		e->page = cmdp_monitor_get_page(vma, addr, &resident, &skip);
		ret = e->page ? 1 : -EFAULT;
		if (skip == CMDP_MON_SKIP_PAGE_TYPE)
			why = "page_type";
	} else {
		ret = get_user_pages_remote(s->mm, addr, 1, FOLL_NOFAULT,
					    &e->page, NULL);
	}
	if (ret != 1) {
		e->page = NULL;
		ret = ret < 0 ? ret : -EFAULT;
		goto fail;
	}
	e->pfn = page_to_pfn(e->page);
	folio = page_folio(e->page);
	why = "page_type";
	ret = -EOPNOTSUPP;
	if (folio_test_large(folio) || !folio_test_anon(folio) ||
	    folio_test_ksm(folio) || folio_test_swapcache(folio) ||
	    PageReserved(e->page) || is_zone_device_page(e->page) ||
	    PageHWPoison(e->page) || !PageAnonExclusive(e->page))
		goto fail;
	/* Putback may still be queued in a per-CPU LRU batch. */
	if (!folio_test_lru(folio))
		lru_add_drain_all();
	why = "folio_busy";
	ret = -EBUSY;
	if (!folio_trylock(folio))
		goto fail;
	w->folio_locked = true;
	if (PageCmdp(e->page))
		goto fail;
	why = "aliases";
	walk = (struct cmdp_walk) { .entry = e };
	rwc.arg = &walk;
	rmap_walk(folio, &rwc);
	ret = walk.error;
	if (ret)
		goto fail;
	ret = -EAGAIN;
	if (!e->nr_aliases || e->nr_aliases != folio_mapcount(folio))
		goto fail;
	/* rmap walk is finished before taking any VMA write exclusion. */
	for (i = 0; i < e->nr_aliases; i++)
		vma_start_write(e->aliases[i].vma);
	if (e->nr_aliases != folio_mapcount(folio))
		goto fail;
	why = "isolate";
	ret = -EBUSY;
	if (!folio_isolate_lru(folio))
		goto fail;
	e->isolated = true;
	atomic64_inc(&cmdp_stats[ISOLATED]);

	/* Reserve allocation before publishing; no sleeping under xa_lock. */
	why = "registry";
	ret = xa_reserve(&cmdp_entries, e->pfn, GFP_KERNEL);
	if (ret)
		goto fail;
	xa_lock_irqsave(&cmdp_entries, flags);
	if (s->closing || xa_load(&cmdp_entries, e->pfn)) {
		ret = -EBUSY;
	} else {
		u64 index_start;

		spin_lock(&s->address_lock);
		index_start = ktime_get_ns();
		ret = cmdp_index_insert(e);
		if (!ret) {
			ret = xa_err(__xa_store(&cmdp_entries, e->pfn, e, GFP_NOWAIT));
			if (ret)
				cmdp_index_remove(e);
		}
		if (!ret) {
			refcount_add(2, &e->refs); /* PFN registry + address index */
			e->registered = true;
			SetPageCmdp(e->page);
			atomic64_inc(&cmdp_stats[MANAGED]);
		}
		s->index_lock_max_ns = max(s->index_lock_max_ns,
					  ktime_get_ns() - index_start);
		spin_unlock(&s->address_lock);
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	xa_release(&cmdp_entries, e->pfn);
	if (ret)
		goto fail;
	trace_cmdp_event(e->pfn, e->generation, CMDP_TERMINAL, CMDP_ARMING,
			"arm", 0, 0);

	w->why = "pte_changed";
	return 0;
fail:
	if (page)
		put_page(page);
	w->why = why;
	return w->error = ret;
}

/* Shared completion/rollback for single-page and batched ARM. A failed
 * protection may leave an ordinary RO PTE, as before; it must never leave
 * registry/LRU ownership or an unsignalled ARMING entry behind.
 */
static int cmdp_arm_finish(struct cmdp_arm_work *w)
{
	struct cmdp_entry *e = w->entry;
	unsigned int i;

	if (!e)
		return w->error;
	if (w->folio_locked)
		folio_unlock(page_folio(e->page));
	if (w->error && e->registered)
		cmdp_finish(e, "arm_abort");
	else if (w->error && e->page) {
		if (e->isolated) {
			folio_putback_lru(page_folio(e->page));
			e->isolated = false;
			atomic64_dec(&cmdp_stats[ISOLATED]);
		}
		put_page(e->page);
		e->page = NULL;
	}
	for (i = 0; i < e->nr_aliases; i++)
		e->aliases[i].vma = NULL;
	complete_all(&e->arm_done);
	if (w->error) {
		atomic64_inc(&cmdp_stats[REJECTS]);
		trace_cmdp_event(e->pfn, e->generation, atomic_read(&e->state),
				CMDP_TERMINAL, w->why, e->protected, w->error);
	}
	atomic64_add(ktime_get_ns() - w->start, &cmdp_stats[ARM_NS]);
	cmdp_put(e);
	return w->error;
}

static void cmdp_arm_check_pins(struct cmdp_arm_work *w)
{
	struct folio *folio = page_folio(w->entry->page);

	if (w->error)
		return;
	/* +2 = transferred manager reference and LRU isolation reference. */
	if (folio_maybe_dma_pinned(folio) ||
	    folio_ref_count(folio) != folio_expected_ref_count(folio) + 2) {
		w->why = "pins_or_refs";
		w->error = -EBUSY;
	}
}

static void cmdp_arm_publish(struct cmdp_arm_work *w)
{
	struct cmdp_entry *e = w->entry;

	if (w->error)
		return;
	w->why = "publish_failed";
	w->error = cmdp_hw_publish(e);
	if (w->error) {
		atomic_set(&e->state, CMDP_REVOKING);
		if (cmdp_hw_revoke(e))
			wait_for_completion(&e->terminal);
		return;
	}
	cmdp_monitor_armed(e);
	atomic_set(&e->state, CMDP_ACTIVE);
	atomic64_inc(&cmdp_stats[ARMS]);
	trace_cmdp_event(e->pfn, e->generation, CMDP_ARMING, CMDP_ACTIVE,
			"armed", e->protected, 0);
}

static int cmdp_arm_locked(struct cmdp_session *s, unsigned long addr,
		struct cmdp_history *history, struct cmdp_cohort *cohort,
		const char **failure)
{
	struct cmdp_arm_work w;
	unsigned int i;
#ifdef CONFIG_CMDP_TEST
	unsigned int fail_after = s->fail_after;

	s->fail_after = 0;
#endif
	if (cmdp_arm_prepare(s, addr, NULL, history, cohort, &w))
		goto finish;
	raw_write_seqcount_begin(&s->mm->write_protect_seq);
	for (i = 0; i < w.entry->nr_aliases; i++) {
		w.error = cmdp_protect_alias(w.entry, &w.entry->aliases[i], false);
		if (w.error)
			break;
#ifdef CONFIG_CMDP_TEST
		if (fail_after && w.entry->protected == fail_after) {
			w.why = "injected_after_alias";
			w.error = -EIO;
			break;
		}
#endif
	}
	smp_mb();
	cmdp_arm_check_pins(&w);
	/* No backend wait inside the fast-FOLL_PIN exclusion sequence. */
	raw_write_seqcount_end(&s->mm->write_protect_seq);
	cmdp_arm_publish(&w);
finish:
	if (failure)
		*failure = w.why;
	return cmdp_arm_finish(&w);
}

static int cmdp_arm(struct cmdp_session *s, unsigned long addr)
{
	int ret = mmap_write_lock_killable(s->mm);

	if (ret)
		return ret;
	ret = cmdp_arm_locked(s, addr, NULL, NULL, NULL);
	mmap_write_unlock(s->mm);
	return ret;
}

#ifdef CONFIG_CMDP_TEST
static void cmdp_test_drop(struct cmdp_session *s)
{
	if (!s->test_page)
		return;
	if (s->test_pinned)
		unpin_user_page(s->test_page);
	else
		put_page(s->test_page);
	s->test_page = NULL;
}

static int cmdp_test_hold(struct cmdp_session *s, unsigned long addr, bool pin)
{
	int ret;

	if (s->test_page)
		return -EBUSY;
	mmap_write_lock(s->mm);
	if (cmdp_mm_active(s->mm)) {
		ret = -EBUSY;
		goto unlock;
	}
	if (pin)
		ret = pin_user_pages(addr, 1, FOLL_WRITE | FOLL_NOFAULT,
				     &s->test_page);
	else
		ret = get_user_pages(addr, 1, FOLL_WRITE | FOLL_NOFAULT,
				     &s->test_page);
	if (ret == 1) {
		s->test_pinned = pin;
		ret = 0;
	} else {
		s->test_page = NULL;
		ret = ret < 0 ? ret : -EFAULT;
	}
unlock:
	mmap_write_unlock(s->mm);
	return ret;
}
#endif

static int cmdp_session_open(struct mm_struct *mm, struct cmdp_session **result,
		struct cmdp_monitor *monitor)
{
	struct cmdp_session *s, *owner;
	unsigned long flags;
	int ret;

	if (!capable(CAP_SYS_ADMIN) || !mm)
		return -EPERM;
	s = kzalloc_obj(*s);
	if (!s)
		return -ENOMEM;
	s->mm = mm;
	s->monitor = monitor;
	mmgrab(mm); /* Structural lifetime only; do not hold mm_users across exit. */
	refcount_set(&s->refs, 1); /* Owner reference. */
	atomic_set(&s->open_files, 1);
	mutex_init(&s->commands);
	spin_lock_init(&s->address_lock);
	s->addresses = RB_ROOT;
	s->notifier.ops = &cmdp_notifier_ops;

	xa_lock_irqsave(&cmdp_entries, flags);
	owner = cmdp_owner;
	if (owner) {
		if (monitor || owner->monitor || owner->mm != mm || owner->closing ||
		    !owner->notifier_registered) {
			ret = -EBUSY;
		} else {
			/* Debugfs fixtures and /dev/cmdp share one mm/session. */
			refcount_inc(&owner->refs);
			atomic_inc(&owner->open_files);
			*result = owner;
			ret = 1;
		}
		xa_unlock_irqrestore(&cmdp_entries, flags);
		cmdp_session_put(s);
		return ret < 0 ? ret : 0;
	}
	cmdp_owner = s;
	xa_unlock_irqrestore(&cmdp_entries, flags);

	ret = mmu_notifier_register(&s->notifier, mm);
	if (ret) {
		xa_lock_irqsave(&cmdp_entries, flags);
		if (cmdp_owner == s)
			cmdp_owner = NULL;
		xa_unlock_irqrestore(&cmdp_entries, flags);
		if (monitor) {
			/* Publication preceded notifier registration. Drain any
			 * custom lifecycle lookup before a failed attachment can
			 * release its controller-owned monitor storage.
			 */
			mmap_write_lock(mm);
			mmap_write_unlock(mm);
		}
		cmdp_session_put(s);
		return ret;
	}
	xa_lock_irqsave(&cmdp_entries, flags);
	s->notifier_registered = true;
	refcount_inc(&s->refs); /* File reference. */
	xa_unlock_irqrestore(&cmdp_entries, flags);
	*result = s;
	return 0;
}

static int cmdp_session_lock(struct cmdp_session *s)
{
	unsigned long flags;
	int ret;

	if (!capable(CAP_SYS_ADMIN) || current->mm != s->mm)
		return -EPERM;
	if (mutex_lock_killable(&s->commands))
		return -EINTR;
	xa_lock_irqsave(&cmdp_entries, flags);
	ret = s->closing ? -ESHUTDOWN : 0;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	if (ret)
		mutex_unlock(&s->commands);
	return ret;
}

static int cmdp_validate_addr(u64 addr)
{
	if (!PAGE_ALIGNED(addr) || addr >= TASK_SIZE_MAX)
		return -EINVAL;
	return 0;
}

static int cmdp_session_arm(struct cmdp_session *s, u64 addr)
{
	int ret = cmdp_session_lock(s);

	if (ret)
		return ret;
	ret = cmdp_validate_addr(addr);
	if (!ret)
		ret = cmdp_arm(s, (unsigned long)addr);
	mutex_unlock(&s->commands);
	return ret;
}

static int cmdp_session_revoke(struct cmdp_session *s, u64 addr)
{
	struct cmdp_entry *e;
	int ret = cmdp_session_lock(s);

	if (ret)
		return ret;
	ret = cmdp_validate_addr(addr);
	if (!ret) {
		e = cmdp_find_range(s, (unsigned long)addr,
				   (unsigned long)addr + PAGE_SIZE);
		ret = e ? cmdp_revoke(e, "command") : -ENOENT;
		if (e)
			cmdp_put(e);
	}
	mutex_unlock(&s->commands);
	return ret;
}

static int cmdp_device_open(struct inode *inode, struct file *file)
{
	struct cmdp_session *s;
	int ret = cmdp_session_open(current->mm, &s, NULL);

	if (!ret)
		file->private_data = s;
	return ret;
}

static long cmdp_device_ioctl(struct file *file, unsigned int cmd,
			      unsigned long arg)
{
	struct cmdp_session *s = file->private_data;
	struct cmdp_page_req req;
	int ret;

	if (cmd != CMDP_IOC_ARM && cmd != CMDP_IOC_REVOKE)
		return -ENOTTY;
	if (copy_from_user(&req, (void __user *)arg, sizeof(req)))
		return -EFAULT;
	if (req.flags)
		return -EINVAL;
	if (cmd == CMDP_IOC_ARM)
		ret = cmdp_session_arm(s, req.addr);
	else
		ret = cmdp_session_revoke(s, req.addr);
	return ret;
}

static int cmdp_session_close(struct cmdp_session *s)
{
	unsigned long flags;
	bool last, unregister;

	xa_lock_irqsave(&cmdp_entries, flags);
	last = atomic_dec_and_test(&s->open_files);
	if (last)
		s->closing = true;
	unregister = last && s->notifier_registered;
	if (unregister)
		s->notifier_registered = false;
	xa_unlock_irqrestore(&cmdp_entries, flags);

	if (last) {
		cmdp_release_mm(&s->notifier, s->mm);
		/* Wait for any invalidation/release callbacks to finish. */
		if (unregister)
			mmu_notifier_unregister(&s->notifier, s->mm);
#ifdef CONFIG_CMDP_TEST
		cmdp_test_drop(s);
#endif
		xa_lock_irqsave(&cmdp_entries, flags);
		if (cmdp_owner == s)
			cmdp_owner = NULL;
		xa_unlock_irqrestore(&cmdp_entries, flags);
		cmdp_session_put(s); /* Owner reference. */
	}
	cmdp_session_put(s); /* File reference. */
	return 0;
}

static int cmdp_device_release(struct inode *inode, struct file *file)
{
	return cmdp_session_close(file->private_data);
}

/* All history/cohort/statistic changes use monitor->lock. Objects are retained
 * until the worker and notifier callbacks have been drained at final close.
 * No monitor lock is held while taking mmap, registry, or PTE locks.
 */
static void cmdp_monitor_account(struct cmdp_monitor *m, u64 now)
{
	u64 delta, add;

	if (m->account_ns && now > m->account_ns && m->status.active) {
		delta = now - m->account_ns;
		add = mul_u64_u64_div_u64(delta,
				m->status.active << PAGE_SHIFT, NSEC_PER_MSEC);
		if (check_add_overflow(m->status.protected_byte_ms, add,
				       &m->status.protected_byte_ms))
			m->status.protected_byte_ms = U64_MAX;
	}
	m->account_ns = now;
}

static void cmdp_monitor_mark_stop(struct cmdp_monitor *m, unsigned int reason)
{
	unsigned long flags;

	spin_lock_irqsave(&m->lock, flags);
	if (m->status.state < CMDP_MON_STOPPING) {
		m->status.state = CMDP_MON_STOPPING;
		m->status.stop_reason = reason;
		m->progress.stop_request_ns = ktime_get_ns();
		m->progress.stop_request_active = m->status.active;
	}
	spin_unlock_irqrestore(&m->lock, flags);
}

static void cmdp_monitor_snapshot_stop(struct cmdp_monitor *m)
{
	unsigned long flags;

	spin_lock_irqsave(&m->lock, flags);
	if (!m->stop_snapshot) {
		m->status.pre_stop_active = m->status.active;
		m->stop_snapshot = true;
		m->progress.stop_drain_ns = ktime_get_ns();
	}
	spin_unlock_irqrestore(&m->lock, flags);
}

/* m->lock protects observation state, including never-ARMED histories. */
static void cmdp_dirty_invalidate_locked(struct cmdp_monitor *m,
		struct cmdp_history *h)
{
	if (h->dirty.valid) {
		h->dirty.valid = 0;
		m->dirty.valid_observations--;
	}
	h->observation_seq++;
}

/* All callers hold m->lock. One reason for every increment of unknown. */
static void cmdp_dirty_unknown(struct cmdp_monitor *m, unsigned int reason)
{
	if (WARN_ON_ONCE(reason >= CMDP_MON_NR_UNKNOWNS))
		reason = CMDP_MON_UNKNOWN_OTHER;
	m->dirty.unknown++;
	m->diagnostics.rejected[reason]++;
}

static unsigned int cmdp_unknown_skip(int skip)
{
	switch (skip) {
	case CMDP_MON_SKIP_NONRESIDENT: return CMDP_MON_UNKNOWN_NONRESIDENT;
	case CMDP_MON_SKIP_SWAPPED: return CMDP_MON_UNKNOWN_SWAPPED;
	case CMDP_MON_SKIP_BUSY: return CMDP_MON_UNKNOWN_PROTNONE;
	case CMDP_MON_SKIP_PAGE_TYPE: return CMDP_MON_UNKNOWN_PAGE_TYPE;
	case CMDP_MON_SKIP_ALIASES: return CMDP_MON_UNKNOWN_ALIASES;
	case CMDP_MON_SKIP_PINS: return CMDP_MON_UNKNOWN_PINS;
	case CMDP_MON_SKIP_RESOURCE: return CMDP_MON_UNKNOWN_RESOURCE;
	default: return CMDP_MON_UNKNOWN_OTHER;
	}
}

static unsigned int cmdp_pte_category(pte_t pte)
{
	if (pte_protnone(pte))
		return CMDP_MON_PTE_PROTNONE;
	if (!pte_present(pte))
		return CMDP_MON_PTE_NONRESIDENT;
	/* Raw D without RW is the CET shadow-stack encoding, unlike the
	 * software saved-dirty bit used by ordinary write protection.
	 */
	if (!(pte_flags(pte) & _PAGE_RW) && (pte_flags(pte) & _PAGE_DIRTY))
		return CMDP_MON_PTE_SHADOW_STACK;
	if (pte_flags(pte) & _PAGE_SAVED_DIRTY)
		return CMDP_MON_PTE_SAVED_DIRTY;
	return (pte_flags(pte) & _PAGE_RW) ? CMDP_MON_PTE_WRITABLE : CMDP_MON_PTE_READONLY;
}

static unsigned int cmdp_unknown_pte(pte_t pte)
{
	switch (cmdp_pte_category(pte)) {
	case CMDP_MON_PTE_NONRESIDENT: return CMDP_MON_UNKNOWN_NONRESIDENT;
	case CMDP_MON_PTE_PROTNONE: return CMDP_MON_UNKNOWN_PROTNONE;
	case CMDP_MON_PTE_READONLY: return CMDP_MON_UNKNOWN_READONLY;
	case CMDP_MON_PTE_SAVED_DIRTY: return CMDP_MON_UNKNOWN_SAVED_DIRTY;
	default: return CMDP_MON_UNKNOWN_PAGE_TYPE;
	}
}

/* Ordinary RW and RO only. Raw RO+D is a CET shadow-stack encoding;
 * pte_write() intentionally accepts it and therefore cannot be used here.
 * RW stores dirty evidence in D; ordinary RO preserves it in SAVED_DIRTY.
 * No permission is restored: the VMA, exclusive folio and final PTE checks
 * remain mandatory, and an unowned RO mapping must establish a fresh epoch.
 */
static bool cmdp_dirty_pte_supported(pte_t pte)
{
	if (!pte_present(pte) || pte_protnone(pte))
		return false;
	return (pte_flags(pte) & _PAGE_RW) ?
		!(pte_flags(pte) & _PAGE_SAVED_DIRTY) :
		!(pte_flags(pte) & _PAGE_DIRTY);
}

static unsigned int cmdp_dirty_pte_bit(pte_t pte)
{
	return (pte_flags(pte) & _PAGE_RW) ?
		_PAGE_BIT_DIRTY : _PAGE_BIT_SAVED_DIRTY;
}

static bool cmdp_dirty_admit(struct cmdp_entry *e, pte_t pte)
{
	struct cmdp_monitor *m = e->session->monitor;
	struct cmdp_history *h = e->history;
	unsigned long flags;
	bool admit;

	if (!m || m->dirty.mode != CMDP_MON_DIRTY_QUALIFY)
		return true;
	spin_lock_irqsave(&m->lock, flags);
	admit = h && h->dirty.valid && h->dirty.pfn == e->pfn &&
		h->dirty.qualified_ns && cmdp_dirty_pte_supported(pte) &&
		!pte_dirty(pte);
	if (!admit && h) {
		if (pte_dirty(pte)) {
			m->dirty.late_dirty++;
			h->dirty.last_dirty_ns = ktime_get_ns();
			h->dirty.last_dirty = 1;
		}
		cmdp_dirty_invalidate_locked(m, h);
	}
	spin_unlock_irqrestore(&m->lock, flags);
	return admit;
}

static void cmdp_monitor_armed(struct cmdp_entry *e)
{
	struct cmdp_monitor *m = e->session->monitor;
	struct cmdp_history *h = e->history;
	unsigned long flags;
	u64 now;
	bool rearm;

	if (!m || !h)
		return;
	spin_lock_irqsave(&m->lock, flags);
	now = ktime_get_ns();
	rearm = h->continuous && h->page.pfn == e->pfn &&
		h->page.state == CMDP_MON_PAGE_OFF_WRITE;
	if (!rearm) {
		h->page.lifetime_id = ++m->next_lifetime;
		h->page.arm_count = 0;
	}
	h->continuous = true;
	cmdp_dirty_invalidate_locked(m, h);
	h->page.pfn = e->pfn;
	h->page.state = CMDP_MON_PAGE_ARMED;
	h->page.reason = CMDP_MON_NONE;
	h->page.arm_generation = e->generation;
	h->page.pass_id = e->cohort->report.id;
	h->page.armed_ns = now;
	h->page.terminal_ns = 0;
	h->page.arm_count++;
	cmdp_monitor_account(m, now);
	m->status.active++;
	e->cohort->report.active++;
	if (rearm) {
		m->status.rearms++;
		e->cohort->report.rearms++;
	} else {
		m->status.first_arms++;
		e->cohort->report.first_arms++;
	}
	e->accounted = true;
	e->rearm = rearm;
	if (rearm)
		e->cohort->report.active_rearm++;
	else
		e->cohort->report.active_first++;
	spin_unlock_irqrestore(&m->lock, flags);
}

static void cmdp_monitor_terminal(struct cmdp_entry *e, const char *why)
{
	struct cmdp_monitor *m = e->session->monitor;
	struct cmdp_history *h = e->history;
	unsigned long flags;
	unsigned int reason;
	u64 now;

	if (!m || !e->accounted)
		return;
	spin_lock_irqsave(&m->lock, flags);
	now = ktime_get_ns();
	if (!strcmp(why, "write"))
		reason = CMDP_MON_WRITE;
	else if (!strcmp(why, "invalidate"))
		reason = CMDP_MON_INVALIDATE;
	else if (m->status.stop_reason)
		reason = m->status.stop_reason;
	else
		reason = CMDP_MON_ERROR;
	cmdp_monitor_account(m, now);
	m->status.active--;
	m->status.terminals[reason]++;
	e->cohort->report.active--;
	if (e->rearm)
		e->cohort->report.active_rearm--;
	else
		e->cohort->report.active_first--;
	if (reason == CMDP_MON_WRITE)
		e->cohort->report.write_revoked++;
	else
		e->cohort->report.other_terminal++;
	cmdp_dirty_invalidate_locked(m, h);
	if (m->dirty.mode && reason == CMDP_MON_WRITE)
		m->dirty.wp_resets++;
	h->page.reason = reason;
	h->page.terminal_ns = now;
	h->page.state = reason == CMDP_MON_WRITE ? CMDP_MON_PAGE_OFF_WRITE :
		reason == CMDP_MON_INVALIDATE ? CMDP_MON_PAGE_INVALIDATED :
		CMDP_MON_PAGE_STOPPED;
	if (reason != CMDP_MON_WRITE)
		h->continuous = false;
	e->accounted = false;
	spin_unlock_irqrestore(&m->lock, flags);
}

static void cmdp_monitor_invalidate_history_count(struct cmdp_session *s,
		unsigned long start, unsigned long end, u64 *observations, u64 *histories)
{
	struct cmdp_monitor *m = s->monitor;
	struct cmdp_history *h;
	unsigned long index, flags;

	if (!m || start >= end)
		return;
	/* No deletions while this session/notifier exists; lockless XA iteration
	 * is safe even when an insertion races an unrelated invalidation.
	 */
	xa_for_each_range(&m->history, index, h, start >> PAGE_SHIFT,
			  (end - 1) >> PAGE_SHIFT) {
		spin_lock_irqsave(&m->lock, flags);
		if (h->dirty.valid) {
			m->dirty.invalidations++;
			if (observations)
				(*observations)++;
		}
		cmdp_dirty_invalidate_locked(m, h);
		if (h->continuous) {
			h->continuous = false;
			m->status.history_resets++;
			if (histories)
				(*histories)++;
		}
		if (h->page.state != CMDP_MON_PAGE_ARMED &&
		    h->page.state != CMDP_MON_PAGE_STOPPED) {
			h->page.state = CMDP_MON_PAGE_INVALIDATED;
		}
		spin_unlock_irqrestore(&m->lock, flags);
	}
}

static void cmdp_monitor_invalidate_history(struct cmdp_session *s,
		unsigned long start, unsigned long end)
{
	cmdp_monitor_invalidate_history_count(s, start, end, NULL, NULL);
}

static struct cmdp_session *cmdp_session_for_mm(struct mm_struct *mm)
{
	struct cmdp_session *s = NULL;
	unsigned long flags;

	mmap_assert_write_locked(mm);
	xa_lock_irqsave(&cmdp_entries, flags);
	if (cmdp_owner && cmdp_owner->mm == mm) {
		s = cmdp_owner;
		refcount_inc(&s->refs);
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	return s;
}

/* Lifecycle callers already exclude new ARM through mmap write locking.
 * Never take commands or join the worker from these paths.
 */
int cmdp_mm_modify(struct mm_struct *mm, unsigned long start, unsigned long end,
		unsigned int source)
{
	struct cmdp_session *s;
	struct cmdp_entry *e;
	u64 begin, revoked = 0, observations = 0, histories = 0;
	bool owner;
	int ret = 0;

	mmap_assert_write_locked(mm);
	s = cmdp_session_for_mm(mm);
	if (!s)
		return 0;
	if (!s->monitor) {
		ret = cmdp_mm_active(mm) ? -EBUSY : 0;
		goto out;
	}
	begin = cmdp_range_begin(s);
	cmdp_monitor_invalidate_history_count(s, start, end, &observations, &histories);
	while ((e = cmdp_find_range(s, start, end))) {
		owner = false;
		ret = cmdp_revoke_counted(e, "invalidate", &owner);
		revoked += owner;
		cmdp_put(e);
		if (ret)
			break;
		cond_resched();
	}
	cmdp_range_end(s, begin, source, start, end, revoked, observations, histories, ret);
out:
	cmdp_session_put(s);
	return ret;
}

void cmdp_mm_stop_locked(struct mm_struct *mm, unsigned int reason)
{
	struct cmdp_session *s;
	struct cmdp_entry *e;
	unsigned long flags;

	mmap_assert_write_locked(mm);
	s = cmdp_session_for_mm(mm);
	if (!s)
		return;
	if (s->monitor) {
		cmdp_monitor_mark_stop(s->monitor, reason);
		cmdp_monitor_snapshot_stop(s->monitor);
		xa_lock_irqsave(&cmdp_entries, flags);
		s->closing = true;
		xa_unlock_irqrestore(&cmdp_entries, flags);
		while ((e = cmdp_find_range(s, 0, ULONG_MAX))) {
			cmdp_revoke(e, "stop");
			cmdp_put(e);
			cond_resched();
		}
		spin_lock_irqsave(&s->monitor->lock, flags);
		s->monitor->status.state = CMDP_MON_STOPPED;
		if (!s->monitor->progress.stop_complete_ns)
			s->monitor->progress.stop_complete_ns = ktime_get_ns();
		spin_unlock_irqrestore(&s->monitor->lock, flags);
	}
	cmdp_session_put(s);
}

void cmdp_mm_stop(struct mm_struct *mm, unsigned int reason)
{
	if (!mm)
		return;
	mmap_write_lock(mm);
	cmdp_mm_stop_locked(mm, reason);
	mmap_write_unlock(mm);
}

/* No-fault, no-touch lookup for the monitor's resident base-page scope.
 * GUP would add FOLL_TOUCH and queue an LRU activation reference, making
 * our own scan look like a pinned page to the strict ARM refcount check.
 * The PTE lock stabilizes the mapping reference while get_page adds ours.
 * Never allocate or split page tables, change A/D bits, or fault pages in.
 */
static struct page *cmdp_monitor_get_page(struct vm_area_struct *vma,
		unsigned long addr, bool *resident, int *skip)
{
	struct mm_struct *mm = vma->vm_mm;
	pgd_t *pgd = pgd_offset(mm, addr);
	p4d_t *p4d;
	pud_t *pud;
	pmd_t *pmd;
	pte_t *ptep, pte;
	spinlock_t *ptl;
	struct page *page = NULL;

	mmap_assert_write_locked(mm);
	*resident = false;
	*skip = CMDP_MON_SKIP_NONRESIDENT;
	if (pgd_none(*pgd) || pgd_bad(*pgd))
		return NULL;
	p4d = p4d_offset(pgd, addr);
	if (p4d_none(*p4d) || p4d_bad(*p4d) || p4d_leaf(*p4d))
		return NULL;
	pud = pud_offset(p4d, addr);
	if (pud_leaf(*pud)) {
		*resident = pud_present(*pud);
		*skip = CMDP_MON_SKIP_PAGE_TYPE;
		return NULL;
	}
	if (pud_none(*pud) || pud_bad(*pud))
		return NULL;
	pmd = pmd_offset(pud, addr);
	if (pmd_leaf(*pmd)) {
		*resident = pmd_present(*pmd);
		*skip = CMDP_MON_SKIP_PAGE_TYPE;
		return NULL;
	}
	if (pmd_none(*pmd) || pmd_bad(*pmd))
		return NULL;
	ptep = pte_offset_map_lock(mm, pmd, addr, &ptl);
	if (!ptep)
		return NULL;
	pte = ptep_get(ptep);
	if (pte_protnone(pte)) {
		/* NUMA hint faults have a separate permission-restoration path. */
		*resident = true;
		*skip = CMDP_MON_SKIP_BUSY;
	} else if (pte_present(pte)) {
		*resident = true;
		page = vm_normal_page(vma, addr, pte);
		if (page) {
			get_page(page);
			*skip = -1;
		} else {
			*skip = CMDP_MON_SKIP_PAGE_TYPE;
		}
	} else if (softleaf_is_swap(softleaf_from_pte(pte))) {
		*skip = CMDP_MON_SKIP_SWAPPED;
	}
	pte_unmap_unlock(ptep, ptl);
	return page;
}

static int cmdp_monitor_probe(struct vm_area_struct *vma, unsigned long addr,
		bool *resident, struct page **result)
{
	struct page *page = NULL;
	struct folio *folio;
	int ret;

	page = cmdp_monitor_get_page(vma, addr, resident, &ret);
	if (!page)
		return ret;

	folio = page_folio(page);
	ret = -1;
	if (folio_test_large(folio) || !folio_test_anon(folio) ||
	    folio_test_ksm(folio) || folio_test_swapcache(folio) ||
	    PageReserved(page) || is_zone_device_page(page) ||
	    PageHWPoison(page) || !PageAnonExclusive(page))
		ret = CMDP_MON_SKIP_PAGE_TYPE;
	else if (folio_mapcount(folio) != 1)
		ret = CMDP_MON_SKIP_ALIASES;
	else if (!PageCmdp(page) && (folio_maybe_dma_pinned(folio) ||
		 folio_ref_count(folio) != folio_expected_ref_count(folio) + 1))
		ret = CMDP_MON_SKIP_PINS;
	if (ret < 0)
		*result = page;
	else
		put_page(page);
	return ret;
}

/* Publish one sample under m->lock. Reset epochs become visible only
 * after the shared synchronous flush, never at the earlier bit reset.
 */
static int cmdp_dirty_sample_record(struct cmdp_monitor *m,
		struct cmdp_history *h, unsigned long pfn, u64 seq, u64 now,
		bool dirty, bool reset, bool valid)
{
	int qualified = 0;

	m->dirty.samples++;
	h->dirty.samples++;
	h->dirty.last_sample_ns = now;
	h->dirty.last_dirty = dirty;
	if (dirty) {
		m->dirty.dirty++;
		h->dirty.dirty++;
		h->dirty.last_dirty_ns = now;
	} else {
		m->dirty.clean++;
	}
	if (reset) {
		m->dirty.resets++;
		/* ABI compatibility: flushes counts pages covered by a completed
		 * reset flush. debugfs dirty_flush_batches counts actual calls.
		 */
		m->dirty.flushes++;
		h->dirty.resets++;
	}
	if (seq != h->observation_seq) {
		cmdp_dirty_invalidate_locked(m, h);
		cmdp_dirty_unknown(m, CMDP_MON_UNKNOWN_SEQUENCE);
	} else if (reset) {
		if (!h->dirty.valid)
			m->dirty.valid_observations++;
		if (!valid)
			m->dirty.primed++;
		h->dirty.valid = 1;
		h->dirty.pfn = pfn;
		h->dirty.epoch_id++;
		h->dirty.quiet_since_ns = now;
		h->dirty.qualified_ns = 0;
	} else {
		qualified = now - h->dirty.quiet_since_ns >=
			(u64)m->dirty.quiet_ms * NSEC_PER_MSEC;
		if (qualified && !h->dirty.qualified_ns) {
			h->dirty.qualified_ns = now;
			m->dirty.qualified++;
		}
	}
	return qualified;
}

/* Folio locks exclude reclaim/migration; mmap write + VMA write exclude
 * mapping changes and per-VMA faults. Drop each PTL before walking the next
 * page, but keep every reset folio locked/referenced until flush + epoch
 * publication. No PTE pointer survives its PTL. User stores still run; the
 * reset-to-flush interval is deliberately outside the certified window.
 */
static int cmdp_monitor_sample_dirty(struct cmdp_monitor *m,
		struct vm_area_struct *vma, unsigned long addr, struct page **pagep,
		struct cmdp_history *h, struct cmdp_scan_batch *b)
{
	struct page *page = *pagep;
	struct folio *folio = page_folio(page);
	struct page_vma_mapped_walk pvmw;
	unsigned long flags, pfn = page_to_pfn(page);
	u64 start = ktime_get_ns(), seq, now, reset_ns = 0;
	bool dirty, reset, valid;
	int ret = -EAGAIN;
	unsigned int rejected = CMDP_MON_UNKNOWN_MAPPING;
	pte_t pte;

	mmap_assert_write_locked(vma->vm_mm);
	vma_start_write(vma);
	if (!folio_trylock(folio)) {
		rejected = CMDP_MON_UNKNOWN_FOLIO_BUSY;
		goto unknown;
	}
	/* Revalidate after acquiring the folio lock. The probe's reference is
	 * reused, not an excuse to omit type, alias, pin or final PTE checks.
	 */
	if (folio_test_large(folio) || !folio_test_anon(folio) ||
	    folio_test_ksm(folio) || folio_test_swapcache(folio) ||
	    PageReserved(page) || is_zone_device_page(page) ||
	    PageHWPoison(page) || !PageAnonExclusive(page)) {
		rejected = CMDP_MON_UNKNOWN_PAGE_TYPE;
		goto unlock_folio;
	}
	if (PageCmdp(page)) {
		rejected = CMDP_MON_UNKNOWN_OWNED;
		goto unlock_folio;
	}
	if (folio_mapcount(folio) != 1) {
		rejected = CMDP_MON_UNKNOWN_ALIASES;
		goto unlock_folio;
	}
	if (folio_maybe_dma_pinned(folio) ||
	    folio_ref_count(folio) != folio_expected_ref_count(folio) + 1) {
		rejected = CMDP_MON_UNKNOWN_PINS;
		goto unlock_folio;
	}
	pvmw = (struct page_vma_mapped_walk) {
		.pfn = pfn, .nr_pages = 1, .pgoff = folio_pgoff(folio),
		.vma = vma, .address = addr, .flags = PVMW_SYNC,
	};
	if (!page_vma_mapped_walk(&pvmw))
		goto unlock_folio;
	if (!pvmw.pte || pvmw.address != addr)
		goto unlock_pte;
	pte = ptep_get(pvmw.pte);
	if (!cmdp_dirty_pte_supported(pte)) {
		rejected = cmdp_unknown_pte(pte);
		goto unlock_pte;
	}
	if (vm_normal_page(vma, addr, pte) != page)
		goto unlock_pte;
	dirty = pte_dirty(pte);
	spin_lock_irqsave(&m->lock, flags);
	seq = h->observation_seq;
	valid = h->dirty.valid && h->dirty.pfn == pfn;
	spin_unlock_irqrestore(&m->lock, flags);
	reset = !valid || dirty;
	if (reset) {
		struct cmdp_dirty_reset *r = &b->dirty[b->nr_dirty++];
		u64 before = ktime_get_ns();

		folio_mark_dirty(folio);
		dirty |= test_and_clear_bit(cmdp_dirty_pte_bit(pte),
					   (unsigned long *)&pvmw.pte->pte);
		reset_ns = ktime_get_ns() - before;
		spin_lock_irqsave(&m->lock, flags);
		/* Do not expose the obsolete certificate while a reset is pending.
		 * An external notifier still invalidates the saved sequence.
		 */
		cmdp_dirty_invalidate_locked(m, h);
		h->dirty.qualified_ns = 0;
		*r = (struct cmdp_dirty_reset) {
			.page = page, .history = h, .seq = seq + 1,
			.valid = valid, .dirty = dirty,
		};
		m->dirty.reset_ns += reset_ns;
		m->dirty.sample_ns += ktime_get_ns() - start;
		spin_unlock_irqrestore(&m->lock, flags);
		page_vma_mapped_walk_done(&pvmw);
		*pagep = NULL; /* batch owns the locked folio and probe reference */
		return 0;
	}
	now = ktime_get_ns();
	spin_lock_irqsave(&m->lock, flags);
	ret = cmdp_dirty_sample_record(m, h, pfn, seq, now, dirty, false, valid);
	m->dirty.sample_ns += ktime_get_ns() - start;
	spin_unlock_irqrestore(&m->lock, flags);
	page_vma_mapped_walk_done(&pvmw);
	folio_unlock(folio);
	return ret;
unlock_pte:
	page_vma_mapped_walk_done(&pvmw);
unlock_folio:
	folio_unlock(folio);
unknown:
	spin_lock_irqsave(&m->lock, flags);
	cmdp_dirty_invalidate_locked(m, h);
	cmdp_dirty_unknown(m, rejected);
	m->dirty.sample_ns += ktime_get_ns() - start;
	spin_unlock_irqrestore(&m->lock, flags);
	return ret;
}

/* Test-only bounded pauses are disabled unless explicitly configured on a
 * privileged monitor. Production scanning has no pause or injected failure.
 */
static struct cmdp_monitor_test_batch cmdp_batch_test_take(struct cmdp_monitor *m,
		struct cmdp_scan_batch *b, unsigned int phase)
{
	struct cmdp_monitor_test_batch test = { };

#ifdef CONFIG_CMDP_TEST
	if (m->test_batch.phase == phase && m->test_batch.addr >= b->start &&
	    m->test_batch.addr < b->end) {
		test = m->test_batch;
		m->test_batch.phase = 0;
	}
#endif
	return test;
}

static void cmdp_batch_test_wait(struct cmdp_monitor_test_batch *test)
{
#ifdef CONFIG_CMDP_TEST
	if (test->phase && test->delay_ms) {
		atomic64_inc(&cmdp_stats[TEST_BATCH_WAITS]);
		atomic64_set(&cmdp_stats[TEST_BATCH_WAITING], test->phase);
		msleep(test->delay_ms);
		atomic64_set(&cmdp_stats[TEST_BATCH_WAITING], 0);
	}
#endif
}

static void cmdp_monitor_flush_dirty(struct cmdp_monitor *m,
		struct cmdp_scan_batch *b)
{
	unsigned long flags;
	unsigned int i;
	u64 start, now, flush_ns;
	struct cmdp_monitor_test_batch test;

	if (!b->nr_dirty)
		return;
	test = cmdp_batch_test_take(m, b, CMDP_MON_TEST_BATCH_DIRTY);
	cmdp_batch_test_wait(&test);
	start = ktime_get_ns();
	/* Includes architectural secondary-TLB invalidation, just as the old
	 * per-page helper did. Required even for initially clean new epochs.
	 */
	flush_tlb_mm_range(m->session->mm, b->start, b->end, PAGE_SHIFT, false);
	now = ktime_get_ns();
	flush_ns = now - start;
	atomic64_inc(&cmdp_stats[DIRTY_FLUSH_BATCHES]);
	atomic64_add(b->nr_dirty, &cmdp_stats[DIRTY_FLUSH_PAGES]);
	spin_lock_irqsave(&m->lock, flags);
	m->dirty.flush_ns += flush_ns;
	for (i = 0; i < b->nr_dirty; i++) {
		struct cmdp_dirty_reset *r = &b->dirty[i];

		cmdp_dirty_sample_record(m, r->history, page_to_pfn(r->page),
				r->seq, now, r->dirty, true, r->valid);
	}
	/* Count shared flush/publication work once, not once per page. */
	m->dirty.sample_ns += ktime_get_ns() - start;
	spin_unlock_irqrestore(&m->lock, flags);
	for (i = 0; i < b->nr_dirty; i++) {
		folio_unlock(page_folio(b->dirty[i].page));
		put_page(b->dirty[i].page);
	}
	b->nr_dirty = 0;
}

static unsigned int cmdp_monitor_failure(int ret, const char *why)
{
	if (ret == -ENOMEM || ret == -ENOSPC)
		return CMDP_MON_SKIP_RESOURCE;
	if (!strcmp(why, "nofault"))
		return CMDP_MON_SKIP_NONRESIDENT;
	if (!strcmp(why, "page_type"))
		return CMDP_MON_SKIP_PAGE_TYPE;
	if (!strcmp(why, "aliases"))
		return CMDP_MON_SKIP_ALIASES;
	if (!strcmp(why, "pins_or_refs"))
		return CMDP_MON_SKIP_PINS;
	if (ret == -EBUSY || ret == -EAGAIN)
		return CMDP_MON_SKIP_BUSY;
	return CMDP_MON_SKIP_OTHER;
}

static void cmdp_monitor_arm_result(struct cmdp_monitor *m,
		struct cmdp_scan_batch *b, struct cmdp_arm_work *w,
		struct cmdp_history *h)
{
	unsigned long flags;
	unsigned int skip;
	int ret = cmdp_arm_finish(w);

	if (!ret)
		return;
	skip = cmdp_monitor_failure(ret, w->why);
	b->counts.skipped[skip]++;
	spin_lock_irqsave(&m->lock, flags);
	if (m->dirty.mode)
		cmdp_dirty_invalidate_locked(m, h);
	if (skip == CMDP_MON_SKIP_RESOURCE)
		m->status.resource_failures++;
	spin_unlock_irqrestore(&m->lock, flags);
}

static void cmdp_monitor_flush_arm(struct cmdp_monitor *m,
		struct cmdp_scan_batch *b)
{
	struct cmdp_session *s = m->session;
	struct mmu_notifier_range range;
	unsigned int i, protected = 0;
	struct cmdp_monitor_test_batch test;

	if (!b->nr_arm)
		return;
	test = cmdp_batch_test_take(m, b, CMDP_MON_TEST_BATCH_ARM);
	/* Monitor candidates have exactly one alias, checked against this
	 * bounded VMA range during preparation. No PTL/rmap lock is held here.
	 * Legacy multi-alias ARM keeps its existing synchronous per-alias path.
	 */
	mmu_notifier_range_init_owner(&range, MMU_NOTIFY_PROTECTION_PAGE, 0,
			s->mm, b->start, b->end, s);
	mmu_notifier_invalidate_range_start(&range);
	raw_write_seqcount_begin(&s->mm->write_protect_seq);
	for (i = 0; i < b->nr_arm; i++) {
		struct cmdp_arm_work *w = &b->arm[i];

		w->error = cmdp_protect_alias(w->entry, &w->entry->aliases[0], true);
		protected += w->entry->protected;
		if (test.abort_after && protected >= test.abort_after)
			break;
	}
	cmdp_batch_test_wait(&test);
	if (test.abort_after) {
		for (i = 0; i < b->nr_arm; i++) {
			b->arm[i].why = "injected_batch_abort";
			b->arm[i].error = -EIO;
		}
	}
	if (protected) {
		flush_tlb_mm_range(s->mm, b->start, b->end, PAGE_SHIFT, false);
		atomic64_inc(&cmdp_stats[ARM_FLUSH_BATCHES]);
		atomic64_add(protected, &cmdp_stats[ARM_FLUSH_PAGES]);
	}
	/* All permission changes and shootdowns precede every final ref/pin
	 * check. Complete all checks before ending the fast-FOLL_PIN sequence;
	 * only then may any backend publication or wait occur.
	 */
	smp_mb();
	for (i = 0; i < b->nr_arm; i++)
		cmdp_arm_check_pins(&b->arm[i]);
	raw_write_seqcount_end(&s->mm->write_protect_seq);
	mmu_notifier_invalidate_range_end(&range);
	for (i = 0; i < b->nr_arm; i++) {
		struct cmdp_arm_work *w = &b->arm[i];
		struct cmdp_history *h = w->entry->history;

		if (READ_ONCE(s->closing) ||
		    READ_ONCE(m->status.state) >= CMDP_MON_STOPPING) {
			w->why = "stopping";
			w->error = -ESHUTDOWN;
		}
		cmdp_arm_publish(w);
		cmdp_monitor_arm_result(m, b, w, h);
	}
	b->nr_arm = 0;
}

/* Only scan-local fields are accumulated here. Admission/terminal counters
 * still update at their exact ownership transitions under m->lock.
 */
static void cmdp_monitor_batch_counts(struct cmdp_monitor *m,
		struct cmdp_cohort *c, struct cmdp_scan_batch *b)
{
	unsigned long flags;
	unsigned int i;

	spin_lock_irqsave(&m->lock, flags);
	c->report.writable_pages += b->counts.writable_pages;
	c->report.examined_pages += b->counts.examined_pages;
	c->report.resident_pages += b->counts.resident_pages;
	c->report.eligible_pages += b->counts.eligible_pages;
	c->report.already_armed += b->counts.already_armed;
	for (i = 0; i < ARRAY_SIZE(c->report.skipped); i++)
		c->report.skipped[i] += b->counts.skipped[i];
	spin_unlock_irqrestore(&m->lock, flags);
	atomic64_add(b->lookups, &cmdp_stats[SCAN_PAGE_LOOKUPS]);
	atomic64_add(b->active_skips, &cmdp_stats[SCAN_ACTIVE_SKIPS]);
}

static void cmdp_monitor_phase(struct cmdp_monitor *m, unsigned int phase)
{
	unsigned long flags;

	spin_lock_irqsave(&m->lock, flags);
	m->progress.scan_phase = phase;
	m->progress.phase_since_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
}

/* This kernel has no mmap_write_trylock wrapper. Match mmap_write_lock's
 * tracing and per-VMA sequence protocol; a bare down_write_trylock would
 * not pair correctly with mmap_write_unlock's vma_end_write_all().
 */
static bool cmdp_mmap_write_trylock(struct mm_struct *mm)
{
	bool acquired;

	__mmap_lock_trace_start_locking(mm, true);
	acquired = down_write_trylock(&mm->mmap_lock) != 0;
	if (acquired)
		mm_lock_seqcount_begin(mm);
	__mmap_lock_trace_acquire_returned(mm, true, acquired);
	return acquired;
}

/* A worker cannot receive the controller's signal. Never sleep indefinitely
 * in mmap_write_lock(): STOP must be able to join this scanner before it
 * performs the separately reported, mandatory ownership drain.
 */
static bool cmdp_monitor_scan_lock(struct cmdp_monitor *m)
{
	struct cmdp_session *s = m->session;
	unsigned long flags;
	u64 start = ktime_get_ns();
	bool acquired = false;

	cmdp_monitor_phase(m, CMDP_MON_SCAN_WAIT_MMAP);
	for (;;) {
		if (READ_ONCE(s->closing) ||
		    READ_ONCE(m->status.state) >= CMDP_MON_STOPPING)
			break;
		if (cmdp_mmap_write_trylock(s->mm)) {
			acquired = true;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		m->progress.scan_lock_retries++;
		spin_unlock_irqrestore(&m->lock, flags);
		schedule_timeout_uninterruptible(1);
	}
	spin_lock_irqsave(&m->lock, flags);
	m->progress.scan_lock_wait_total_ns += ktime_get_ns() - start;
	m->progress.scan_phase = acquired ? CMDP_MON_SCAN_SCANNING : CMDP_MON_SCAN_IDLE;
	m->progress.phase_since_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
	return acquired;
}

static int cmdp_monitor_scan(struct cmdp_monitor *m)
{
	struct cmdp_session *s = m->session;
	struct cmdp_cohort *c;
	struct cmdp_scan_batch *b;
	struct page *page;
	struct cmdp_history *h;
	struct vm_area_struct *vma;
	unsigned long addr = 0, flags;
	unsigned int batch, skip;
	bool resident;
	int ret = 0, probe;

	c = kzalloc_obj(*c);
	b = kzalloc_obj(*b);
	if (!c || !b) {
		kfree(c);
		kfree(b);
		spin_lock_irqsave(&m->lock, flags);
		m->status.resource_failures++;
		spin_unlock_irqrestore(&m->lock, flags);
		return -ENOMEM;
	}
	spin_lock_irqsave(&m->lock, flags);
	if (m->status.state >= CMDP_MON_STOPPING ||
	    m->status.passes_started >= CMDP_MON_MAX_COHORTS) {
		ret = m->status.state >= CMDP_MON_STOPPING ? -ESHUTDOWN : -ENOSPC;
		if (ret == -ENOSPC)
			m->status.resource_failures++;
		spin_unlock_irqrestore(&m->lock, flags);
		kfree(c);
		kfree(b);
		return ret;
	}
	c->report.id = m->status.passes_started + 1;
	c->report.start_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
	ret = xa_err(xa_store(&m->cohorts, c->report.id, c, GFP_KERNEL));
	if (ret) {
		spin_lock_irqsave(&m->lock, flags);
		m->status.resource_failures++;
		spin_unlock_irqrestore(&m->lock, flags);
		kfree(c);
		kfree(b);
		return ret;
	}
	spin_lock_irqsave(&m->lock, flags);
	m->status.passes_started++;
	if (m->dirty.mode)
		m->dirty.passes_started++;
	m->current_cohort = c;
	spin_unlock_irqrestore(&m->lock, flags);
	/* Pending allocation/putback batches also own transient references.
	 * Flush once per pass; unlike remote GUP, our lookup adds no new ones.
	 */
	cmdp_monitor_phase(m, CMDP_MON_SCAN_PREPARE);
	lru_add_drain_all();
	while (addr < TASK_SIZE_MAX) {
		if (READ_ONCE(m->status.state) >= CMDP_MON_STOPPING) {
			ret = -ESHUTDOWN;
			break;
		}
		if (!mmget_not_zero(s->mm)) {
			cmdp_monitor_mark_stop(m, CMDP_MON_TARGET_EXIT);
			ret = -ESRCH;
			break;
		}
		if (!cmdp_monitor_scan_lock(m)) {
			mmput(s->mm);
			ret = -ESHUTDOWN;
			break;
		}
		b->nr_dirty = b->nr_arm = 0;
		b->counts = (struct cmdp_monitor_pass) { };
		b->lookups = b->active_skips = 0;
		vma = NULL;
		for (batch = 0; batch < CMDP_MON_BATCH && addr < TASK_SIZE_MAX; batch++) {
			page = NULL;
			if (READ_ONCE(s->closing) ||
			    READ_ONCE(m->status.state) >= CMDP_MON_STOPPING) {
				ret = -ESHUTDOWN;
				break;
			}
			if (vma && addr >= b->end)
				break;
			if (!vma)
				vma = find_vma(s->mm, addr);
			if (!vma) {
				addr = TASK_SIZE_MAX;
				break;
			}
			addr = max(addr, vma->vm_start);
			if (!batch) {
				b->start = addr;
				b->end = min(vma->vm_end, addr + CMDP_MON_BATCH * PAGE_SIZE);
			}
			if (!(vma->vm_flags & VM_WRITE)) {
				addr = vma->vm_end;
				continue;
			}
			if (!cmdp_vma_supported(vma)) {
				u64 pages = (vma->vm_end - addr) >> PAGE_SHIFT;

				skip = vma_is_anonymous(vma) && !vma->vm_file &&
					!(vma->vm_flags & (VM_SHARED | VM_MAYSHARE |
					VM_HUGETLB | VM_PFNMAP | VM_MIXEDMAP | VM_IO |
					VM_MERGEABLE | VM_SHADOW_STACK)) && !userfaultfd_armed(vma) ?
					CMDP_MON_SKIP_THP_POLICY : CMDP_MON_SKIP_VMA;
				b->counts.writable_pages += pages;
				b->counts.examined_pages += pages;
				b->counts.skipped[skip] += pages;
				if (m->dirty.mode)
					cmdp_monitor_invalidate_history(s, addr, vma->vm_end);
				addr = vma->vm_end;
				continue;
			}
			/* With VMA fault exclusion acquired, an ARMED history still
			 * owns an isolated resident eligible folio. Lifecycle hooks
			 * remove that state before mapping/permission changes. Count
			 * it directly; no page-table walk or reference is needed.
			 */
			vma_start_write(vma);
			h = xa_load(&m->history, addr >> PAGE_SHIFT);
			spin_lock_irqsave(&m->lock, flags);
			if (h && h->page.state == CMDP_MON_PAGE_ARMED) {
				spin_unlock_irqrestore(&m->lock, flags);
				b->counts.writable_pages++;
				b->counts.examined_pages++;
				b->counts.resident_pages++;
				b->counts.eligible_pages++;
				b->counts.already_armed++;
				b->active_skips++;
				goto next;
			}
			spin_unlock_irqrestore(&m->lock, flags);
			b->lookups++;
			probe = cmdp_monitor_probe(vma, addr, &resident, &page);
			b->counts.writable_pages++;
			b->counts.examined_pages++;
			b->counts.resident_pages += resident;
			if (probe >= 0)
				b->counts.skipped[probe]++;
			else
				b->counts.eligible_pages++;
			if (probe >= 0 || (m->status.flags & CMDP_MONITOR_F_SCAN_ONLY)) {
				if (m->dirty.mode) {
					cmdp_monitor_invalidate_history(s, addr, addr + PAGE_SIZE);
					spin_lock_irqsave(&m->lock, flags);
					cmdp_dirty_unknown(m, cmdp_unknown_skip(probe));
					spin_unlock_irqrestore(&m->lock, flags);
				}
				goto next;
			}
			spin_lock_irqsave(&m->lock, flags);
			if ((!m->dirty.mode && m->status.active + b->nr_arm >= m->max_pages) ||
			    (!h && m->status.history_entries >= m->max_history)) {
				b->counts.skipped[CMDP_MON_SKIP_RESOURCE]++;
				m->status.resource_failures++;
				if (m->dirty.mode) {
					m->dirty.resource_skips++;
					cmdp_dirty_unknown(m, CMDP_MON_UNKNOWN_RESOURCE);
				}
				spin_unlock_irqrestore(&m->lock, flags);
				goto next;
			}
			spin_unlock_irqrestore(&m->lock, flags);
			if (!h) {
				h = kzalloc_obj(*h);
				if (!h)
					goto resource;
				h->page.addr = addr;
				h->dirty.addr = addr;
				probe = xa_err(xa_store(&m->history, addr >> PAGE_SHIFT,
							h, GFP_KERNEL));
				if (probe) {
					kfree(h);
					goto resource;
				}
				spin_lock_irqsave(&m->lock, flags);
				m->status.history_entries++;
				spin_unlock_irqrestore(&m->lock, flags);
			}
			if (m->dirty.mode) {
				probe = cmdp_monitor_sample_dirty(m, vma, addr, &page, h, b);
				if (probe <= 0 || m->dirty.mode == CMDP_MON_DIRTY_OBSERVE)
					goto next;
				spin_lock_irqsave(&m->lock, flags);
				if (m->status.active + b->nr_arm >= m->max_pages) {
					cmdp_dirty_invalidate_locked(m, h);
					spin_unlock_irqrestore(&m->lock, flags);
					goto resource;
				}
				spin_unlock_irqrestore(&m->lock, flags);
			}
			probe = cmdp_arm_prepare(s, addr, page, h, c, &b->arm[b->nr_arm]);
			page = NULL;
			if (!probe) {
				struct cmdp_arm_work *w = &b->arm[b->nr_arm];
				struct cmdp_entry *e = w->entry;

				/* The batched notifier/flush must cover every alias. */
				if (e->nr_aliases != 1 || e->aliases[0].vma != vma ||
				    e->aliases[0].addr != addr) {
					w->why = "aliases";
					w->error = -EOPNOTSUPP;
					probe = w->error;
				}
			}
			if (probe)
				cmdp_monitor_arm_result(m, b, &b->arm[b->nr_arm], h);
			else
				b->nr_arm++;
			goto next;
resource:
			spin_lock_irqsave(&m->lock, flags);
			if (m->dirty.mode) {
				m->dirty.resource_skips++;
				cmdp_dirty_unknown(m, CMDP_MON_UNKNOWN_RESOURCE);
			}
			b->counts.skipped[CMDP_MON_SKIP_RESOURCE]++;
			m->status.resource_failures++;
			spin_unlock_irqrestore(&m->lock, flags);
next:
			if (page)
				put_page(page);
			addr += PAGE_SIZE;
		}
		/* Also drain partial chunks on STOP/error before dropping mmap.
		 * No deferred reset, ARMING entry or folio lock may escape here.
		 */
		cmdp_monitor_flush_dirty(m, b);
		cmdp_monitor_flush_arm(m, b);
		cmdp_monitor_batch_counts(m, c, b);
		mmap_write_unlock(s->mm);
		/* May run release notifier here. Never hold a monitor lock. */
		mmput(s->mm);
		spin_lock_irqsave(&m->lock, flags);
		c->report.cursor = addr;
		spin_unlock_irqrestore(&m->lock, flags);
		if (ret)
			break;
		cond_resched();
	}
	spin_lock_irqsave(&m->lock, flags);
	c->report.end_ns = ktime_get_ns();
	c->report.complete = !ret && addr >= TASK_SIZE_MAX;
	c->report.error = ret;
	if (c->report.complete) {
		m->status.passes_completed++;
		if (m->dirty.mode)
			m->dirty.passes_completed++;
	}
	m->status.last_pass = c->report;
	m->progress.scan_phase = CMDP_MON_SCAN_IDLE;
	m->progress.phase_since_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
	kfree(b);
	return ret;
}

static void cmdp_monitor_work(struct work_struct *work)
{
	struct cmdp_monitor *m = container_of(to_delayed_work(work),
					     struct cmdp_monitor, work);
	unsigned long flags;
	u64 now, period, missed = 0, delay = 0;
	bool again = false;
	int ret;

	mutex_lock(&m->scan);
	spin_lock_irqsave(&m->lock, flags);
	if (m->status.state != CMDP_MON_RUNNING) {
		spin_unlock_irqrestore(&m->lock, flags);
		mutex_unlock(&m->scan);
		return;
	}
	m->scanning = true;
	spin_unlock_irqrestore(&m->lock, flags);
	ret = cmdp_monitor_scan(m);
	if (ret && ret != -ESHUTDOWN && ret != -ESRCH)
		cmdp_monitor_mark_stop(m, CMDP_MON_ERROR);
	spin_lock_irqsave(&m->lock, flags);
	m->scanning = false;
	if (m->status.state == CMDP_MON_RUNNING) {
		now = ktime_get_ns();
		period = (u64)m->status.period_ms * NSEC_PER_MSEC;
		m->next_deadline += period;
		if (m->next_deadline <= now) {
			missed = div64_u64(now - m->next_deadline, period) + 1;
			m->next_deadline += missed * period;
		}
		m->status.overruns += missed;
		delay = m->next_deadline - now;
		again = true;
	}
	spin_unlock_irqrestore(&m->lock, flags);
	mutex_unlock(&m->scan);
	/* Queue under ctl so STOP cannot miss a racing self-requeue. */
	if (again) {
		mutex_lock(&m->ctl);
		if (READ_ONCE(m->status.state) == CMDP_MON_RUNNING)
			schedule_delayed_work(&m->work, nsecs_to_jiffies(delay));
		mutex_unlock(&m->ctl);
	}
	/* STOP owns cleanup after joining us. Re-entering mmap_write_lock here
	 * on cancellation would recreate the uninterruptible join dependency.
	 */
	if (ret && ret != -ESHUTDOWN && ret != -ESRCH && m->session)
		cmdp_mm_stop(m->session->mm, m->status.stop_reason ?: CMDP_MON_ERROR);
}

static void cmdp_monitor_stop(struct cmdp_monitor *m, unsigned int reason)
{
	struct cmdp_session *s = m->session;
	unsigned long flags;

	mutex_lock(&m->ctl);
	cmdp_monitor_mark_stop(m, reason);
	mutex_unlock(&m->ctl);
	cancel_delayed_work_sync(&m->work);
	mutex_lock(&m->scan);
	if (s) {
		/* mmgrab alone does not permit walking dead page tables. The
		 * notifier has already drained ownership if no live mm remains.
		 */
		if (mmget_not_zero(s->mm)) {
			cmdp_mm_stop(s->mm, reason);
			mmput(s->mm);
		} else {
			cmdp_monitor_snapshot_stop(m);
		}
	}
	spin_lock_irqsave(&m->lock, flags);
	m->status.state = CMDP_MON_STOPPED;
	if (!m->progress.stop_complete_ns)
		m->progress.stop_complete_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
	mutex_unlock(&m->scan);
}

static int cmdp_monitor_attach_target(struct cmdp_monitor *m,
		const struct cmdp_monitor_attach *a)
{
	struct task_struct *task, *group, *thread;
	struct mm_struct *mm;
	unsigned int pidflags;
	unsigned long flags;
	bool shared = false;
	int ret;

	if (a->size != sizeof(*a) || a->version != CMDP_MONITOR_VERSION ||
	    a->reserved || (a->flags & ~CMDP_MONITOR_F_SCAN_ONLY) ||
	    (a->period_ms && a->period_ms < 10) || a->period_ms > 3600000 ||
	    a->max_pages > CMDP_MON_MAX_PAGES || a->max_history > CMDP_MON_MAX_PAGES)
		return -EINVAL;
	if (m->session || m->status.state != CMDP_MON_UNATTACHED)
		return -EBUSY;
	task = pidfd_get_task(a->pidfd, &pidflags);
	if (IS_ERR(task))
		return PTR_ERR(task);
	/* Require a different mm: monitor copy_to_user buffers must not be ARM
	 * targets, and an external controller must not deadlock on its own scan.
	 */
	mm = mm_access(task, PTRACE_MODE_ATTACH_REALCREDS);
	if (IS_ERR_OR_NULL(mm)) {
		ret = mm ? PTR_ERR(mm) : -ESRCH;
		goto task_out;
	}
	if (mm == current->mm) {
		ret = -EINVAL;
		goto mm_out;
	}
	m->status.target_pid = task_tgid_vnr(task);
	m->status.flags = a->flags;
	m->status.period_ms = a->period_ms ?: 10000;
	m->max_pages = a->max_pages ?: 1048576;
	m->max_history = a->max_history ?: 1048576;
	/* Initialize before session publication: a concurrent lifecycle hook
	 * must never have its terminal state overwritten by attachment.
	 */
	spin_lock_irqsave(&m->lock, flags);
	m->status.session_id = atomic64_inc_return(&cmdp_monitor_ids);
	m->status.attach_ns = ktime_get_ns();
	m->account_ns = m->status.attach_ns;
	m->status.state = CMDP_MON_ATTACHED;
	spin_unlock_irqrestore(&m->lock, flags);
	ret = cmdp_session_open(mm, &m->session, m);
	if (ret) {
		spin_lock_irqsave(&m->lock, flags);
		memset(&m->status, 0, sizeof(m->status));
		memset(&m->progress, 0, sizeof(m->progress));
		m->account_ns = 0;
		m->stop_snapshot = false;
		spin_unlock_irqrestore(&m->lock, flags);
		goto mm_out;
	}
	/* Threads in the selected process share the mm. A foreign TGID sharing
	 * it (CLONE_VM without CLONE_THREAD) is outside this first-stage scope.
	 * The post-publication clone hook closes the concurrent-clone race.
	 */
	rcu_read_lock();
	for_each_process_thread(group, thread) {
		if (same_thread_group(task, thread))
			continue;
		task_lock(thread);
		shared = thread->mm == mm;
		task_unlock(thread);
		if (shared)
			goto shared_found;
	}
shared_found:
	rcu_read_unlock();
	if (shared) {
		cmdp_mm_stop(mm, CMDP_MON_FORK);
		ret = -EBUSY;
	}
mm_out:
	mmput(mm);
task_out:
	put_task_struct(task);
	return ret;
}

/* ctl pins the session; mmap/VMA write excludes mapping changes and faults.
 * PTL and the registry then give a single-address ownership/PTE snapshot.
 */
static int cmdp_monitor_page_snapshot(struct cmdp_monitor *m,
		struct cmdp_monitor_page_snapshot *out)
{
	struct mm_struct *mm;
	struct vm_area_struct *vma;
	struct cmdp_history *h;
	struct cmdp_entry *e;
	struct page *page;
	pgd_t *pgd;
	p4d_t *p4d;
	pud_t *pud;
	pmd_t *pmd;
	pte_t *ptep, pte;
	spinlock_t *ptl;
	unsigned long flags, addr = out->addr;
	int ret = 0;

	if (!m->session || !mmget_not_zero(m->session->mm))
		return -ESRCH;
	mm = m->session->mm;
	if (!cmdp_mmap_write_trylock(mm)) {
		ret = -EAGAIN;
		goto put;
	}
	memset(out, 0, sizeof(*out));
	out->size = sizeof(*out);
	out->version = CMDP_MONITOR_VERSION;
	out->addr = addr;
	out->category = CMDP_MON_PTE_UNMAPPED;
	vma = vma_lookup(mm, addr);
	if (!vma)
		goto history;
	vma_start_write(vma);
	out->category = CMDP_MON_PTE_NONRESIDENT;
	pgd = pgd_offset(mm, addr);
	if (pgd_none(*pgd) || pgd_bad(*pgd))
		goto history;
	p4d = p4d_offset(pgd, addr);
	if (p4d_leaf(*p4d))
		goto unsupported;
	if (p4d_none(*p4d) || p4d_bad(*p4d))
		goto history;
	pud = pud_offset(p4d, addr);
	if (pud_leaf(*pud))
		goto unsupported;
	if (pud_none(*pud) || pud_bad(*pud))
		goto history;
	pmd = pmd_offset(pud, addr);
	if (pmd_leaf(*pmd))
		goto unsupported;
	if (pmd_none(*pmd) || pmd_bad(*pmd))
		goto history;
	ptep = pte_offset_map_lock(mm, pmd, addr, &ptl);
	if (!ptep)
		goto history;
	pte = ptep_get(ptep);
	out->pte_flags = pte_flags(pte);
	out->category = cmdp_pte_category(pte);
	page = pte_present(pte) ? vm_normal_page(vma, addr, pte) : NULL;
	if (page) {
		out->pfn = page_to_pfn(page);
		e = cmdp_lookup(page);
		if (e && !IS_ERR(e)) {
			out->owned = e->session == m->session;
			if (out->owned)
				out->arm_generation = e->generation;
			cmdp_put(e);
		} else if (IS_ERR(e)) {
			ret = PTR_ERR(e);
		}
	}
	/* Sample history while PTE remains locked for the coherent normal-page
	 * case. Other cases still have mmap/VMA exclusion, with no owned entry.
	 */
	spin_lock_irqsave(&m->lock, flags);
	h = xa_load(&m->history, addr >> PAGE_SHIFT);
	if (h) {
		out->history_state = h->page.state;
		out->observation_valid = h->dirty.valid;
		out->epoch_id = h->dirty.epoch_id;
		out->quiet_since_ns = h->dirty.quiet_since_ns;
		out->last_sample_ns = h->dirty.last_sample_ns;
		out->qualified_ns = h->dirty.qualified_ns;
	}
	out->now_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
	pte_unmap_unlock(ptep, ptl);
	goto unlock;
unsupported:
	out->category = CMDP_MON_PTE_UNSUPPORTED;
history:
	spin_lock_irqsave(&m->lock, flags);
	h = xa_load(&m->history, addr >> PAGE_SHIFT);
	if (h) {
		out->history_state = h->page.state;
		out->observation_valid = h->dirty.valid;
		out->epoch_id = h->dirty.epoch_id;
		out->quiet_since_ns = h->dirty.quiet_since_ns;
		out->last_sample_ns = h->dirty.last_sample_ns;
		out->qualified_ns = h->dirty.qualified_ns;
	}
	out->now_ns = ktime_get_ns();
	spin_unlock_irqrestore(&m->lock, flags);
unlock:
	mmap_write_unlock(mm);
put:
	mmput(mm);
	return ret;
}

#ifdef CONFIG_CMDP_TEST
/* Exercise the actual admission/harvesting helpers on a local scalar only.
 * Unsupported/CET encodings are never installed in an application PTE.
 */
static long cmdp_monitor_test_pte(void __user *up)
{
	struct cmdp_monitor_test_pte req;
	pteval_t bits = 0;
	pte_t pte;

	if (copy_from_user(&req, up, sizeof(req)))
		return -EFAULT;
	if (req.size != sizeof(req) || req.version != CMDP_MONITOR_VERSION ||
	    (req.flags & ~CMDP_MON_TEST_PTE_MASK) ||
	    ((req.flags & CMDP_MON_TEST_PTE_PRESENT) &&
	     (req.flags & CMDP_MON_TEST_PTE_PROTNONE)))
		return -EINVAL;
	if (req.flags & CMDP_MON_TEST_PTE_PRESENT) bits |= _PAGE_PRESENT;
	if (req.flags & CMDP_MON_TEST_PTE_RW) bits |= _PAGE_RW;
	if (req.flags & CMDP_MON_TEST_PTE_DIRTY) bits |= _PAGE_DIRTY;
	if (req.flags & CMDP_MON_TEST_PTE_SAVED_DIRTY) bits |= _PAGE_SAVED_DIRTY;
	if (req.flags & CMDP_MON_TEST_PTE_PROTNONE) bits |= _PAGE_PROTNONE;
	pte = __pte(bits);
	req.supported = cmdp_dirty_pte_supported(pte);
	req.dirty = pte_dirty(pte);
	if (req.supported)
		__clear_bit(cmdp_dirty_pte_bit(pte), (unsigned long *)&pte.pte);
	bits = pte_flags(pte);
	req.cleared_flags = 0;
	if (bits & _PAGE_PRESENT) req.cleared_flags |= CMDP_MON_TEST_PTE_PRESENT;
	if (bits & _PAGE_RW) req.cleared_flags |= CMDP_MON_TEST_PTE_RW;
	if (bits & _PAGE_DIRTY) req.cleared_flags |= CMDP_MON_TEST_PTE_DIRTY;
	if (bits & _PAGE_SAVED_DIRTY) req.cleared_flags |= CMDP_MON_TEST_PTE_SAVED_DIRTY;
	if (bits & _PAGE_PROTNONE) req.cleared_flags |= CMDP_MON_TEST_PTE_PROTNONE;
	return copy_to_user(up, &req, sizeof(req)) ? -EFAULT : 0;
}

static long cmdp_monitor_test_invalidate(struct cmdp_monitor *m, void __user *up)
{
	struct cmdp_monitor_test_invalidate req;
	struct mmu_notifier_range range;
	struct mm_struct *mm = NULL;
	struct vm_area_struct *vma;
	unsigned long addr;
	int ret;

	if (copy_from_user(&req, up, sizeof(req)))
		return -EFAULT;
	if (req.size != sizeof(req) || req.version != CMDP_MONITOR_VERSION ||
	    req.reserved[0] || req.reserved[1] || cmdp_validate_addr(req.start) ||
	    req.end <= req.start || req.end > TASK_SIZE_MAX ||
	    (req.end & ~PAGE_MASK) || req.end - req.start > 1024UL * PAGE_SIZE)
		return -EINVAL;
	mutex_lock(&m->ctl);
	if (m->session && READ_ONCE(m->status.state) < CMDP_MON_STOPPING &&
	    mmget_not_zero(m->session->mm))
		mm = m->session->mm;
	mutex_unlock(&m->ctl);
	if (!mm)
		return -ESRCH;
	ret = mmap_write_lock_killable(mm);
	if (ret)
		goto put;
	if (READ_ONCE(m->status.state) >= CMDP_MON_STOPPING) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	for (addr = req.start; addr < req.end; addr = min(req.end, vma->vm_end)) {
		vma = vma_lookup(mm, addr);
		if (!vma) {
			ret = -EINVAL;
			goto unlock;
		}
		vma_start_write(vma);
	}
	/* Deliberately external: not the session owner and not PROTECTION_PAGE.
	 * Like a skipped NUMA PTE, start/end occur without changing the PTE.
	 */
	mmu_notifier_range_init_owner(&range, MMU_NOTIFY_PROTECTION_VMA, 0,
			mm, req.start, req.end, &cmdp_test_owner);
	mmu_notifier_invalidate_range_start(&range);
	mmu_notifier_invalidate_range_end(&range);
unlock:
	mmap_write_unlock(mm);
put:
	mmput(mm);
	return ret;
}

static long cmdp_monitor_test_hold_mmap(struct cmdp_monitor *m, void __user *up)
{
	struct cmdp_monitor_test_hold hold;
	struct mm_struct *mm = NULL;
	unsigned long flags;
	int ret;

	if (copy_from_user(&hold, up, sizeof(hold)))
		return -EFAULT;
	if (hold.size != sizeof(hold) || hold.version != CMDP_MONITOR_VERSION ||
	    hold.reserved || !hold.milliseconds || hold.milliseconds > 2000)
		return -EINVAL;
	mutex_lock(&m->ctl);
	if (m->session && READ_ONCE(m->status.state) < CMDP_MON_STOPPING &&
	    mmget_not_zero(m->session->mm))
		mm = m->session->mm;
	mutex_unlock(&m->ctl);
	if (!mm)
		return -ESRCH;
	ret = mmap_read_lock_killable(mm);
	if (ret)
		goto put;
	spin_lock_irqsave(&m->lock, flags);
	m->progress.test_mmap_readers++;
	spin_unlock_irqrestore(&m->lock, flags);
	msleep(hold.milliseconds);
	spin_lock_irqsave(&m->lock, flags);
	m->progress.test_mmap_readers--;
	spin_unlock_irqrestore(&m->lock, flags);
	mmap_read_unlock(mm);
put:
	mmput(mm);
	return ret;
}
#endif

static long cmdp_monitor_ioctl(struct file *file, unsigned int cmd,
		unsigned long arg)
{
	struct cmdp_monitor *m = file->private_data;
	void __user *up = (void __user *)arg;
	/* Each ioctl uses one payload; do not add their sizes to the stack. */
	union {
		struct cmdp_monitor_attach a;
		struct cmdp_monitor_status status;
		struct cmdp_monitor_page page;
		struct cmdp_monitor_cohort cohort;
		struct cmdp_monitor_progress progress;
		struct cmdp_monitor_dirty_config dirty_config;
		struct cmdp_monitor_dirty_status dirty_status;
		struct cmdp_monitor_dirty_page dirty_page;
		struct cmdp_monitor_diagnostics diagnostics;
		struct cmdp_monitor_page_snapshot snapshot;
#ifdef CONFIG_CMDP_TEST
		struct cmdp_monitor_test_batch test_batch;
#endif
	} data;
	struct cmdp_history *h;
	struct cmdp_cohort *c;
	unsigned long flags;
	int ret = 0;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
#ifdef CONFIG_CMDP_TEST
	if (cmd == CMDP_MON_TEST_PTE)
		return cmdp_monitor_test_pte(up);
	if (cmd == CMDP_MON_TEST_HOLD_MMAP)
		return cmdp_monitor_test_hold_mmap(m, up);
	if (cmd == CMDP_MON_TEST_INVALIDATE)
		return cmdp_monitor_test_invalidate(m, up);
#endif
	if (cmd == CMDP_MON_STOP) {
		mutex_lock(&m->ctl);
		cmdp_monitor_mark_stop(m, CMDP_MON_CONTROL_STOP);
		mutex_unlock(&m->ctl);
		cmdp_monitor_stop(m, CMDP_MON_CONTROL_STOP);
		return 0;
	}
	mutex_lock(&m->ctl);
	switch (cmd) {
#ifdef CONFIG_CMDP_TEST
	case CMDP_MON_TEST_BATCH:
		if (copy_from_user(&data.test_batch, up, sizeof(data.test_batch))) {
			ret = -EFAULT;
			break;
		}
		if (data.test_batch.size != sizeof(data.test_batch) ||
		    data.test_batch.version != CMDP_MONITOR_VERSION ||
		    data.test_batch.phase < CMDP_MON_TEST_BATCH_DIRTY ||
		    data.test_batch.phase > CMDP_MON_TEST_BATCH_ARM ||
		    data.test_batch.delay_ms > 1000 || data.test_batch.reserved ||
		    cmdp_validate_addr(data.test_batch.addr) ||
		    data.test_batch.abort_after > CMDP_MON_BATCH ||
		    (data.test_batch.abort_after &&
		     data.test_batch.phase != CMDP_MON_TEST_BATCH_ARM)) {
			ret = -EINVAL;
			break;
		}
		if (!m->session || m->status.state != CMDP_MON_ATTACHED || m->scanning) {
			ret = -EBUSY;
			break;
		}
		m->test_batch = data.test_batch;
		break;
#endif
	case CMDP_MON_ATTACH:
		if (copy_from_user(&data.a, up, sizeof(data.a))) {
			ret = -EFAULT;
			break;
		}
		ret = cmdp_monitor_attach_target(m, &data.a);
		break;
	case CMDP_MON_DIRTY_CONFIG:
		if (copy_from_user(&data.dirty_config, up, sizeof(data.dirty_config))) {
			ret = -EFAULT;
			break;
		}
		if (data.dirty_config.size != sizeof(data.dirty_config) ||
		    data.dirty_config.version != CMDP_MONITOR_VERSION ||
		    data.dirty_config.mode > CMDP_MON_DIRTY_QUALIFY ||
		    (data.dirty_config.quiet_ms && data.dirty_config.quiet_ms < 10) ||
		    data.dirty_config.quiet_ms > 3600000 ||
		    data.dirty_config.reserved[0] || data.dirty_config.reserved[1] ||
		    (data.dirty_config.mode && (m->status.flags & CMDP_MONITOR_F_SCAN_ONLY))) {
			ret = -EINVAL;
			break;
		}
		if (!m->session || m->status.state != CMDP_MON_ATTACHED ||
		    m->scanning || m->status.passes_started) {
			ret = -EBUSY;
			break;
		}
		m->dirty.mode = data.dirty_config.mode;
		m->dirty.quiet_ms = data.dirty_config.quiet_ms ?: 10000;
		break;
	case CMDP_MON_DIRTY_STATUS:
		if (copy_from_user(&data.dirty_status, up, sizeof(data.dirty_status))) {
			ret = -EFAULT;
			break;
		}
		if (data.dirty_status.size != sizeof(data.dirty_status) ||
		    data.dirty_status.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.dirty_status = m->dirty;
		data.dirty_status.size = sizeof(data.dirty_status);
		data.dirty_status.version = CMDP_MONITOR_VERSION;
		data.dirty_status.now_ns = ktime_get_ns();
		data.dirty_status.metadata_bytes = m->status.history_entries * sizeof(*h);
		/* STOP invalidates the session observation contract immediately,
		 * without a potentially unbounded walk of never-ARMED histories.
		 */
		if (m->status.state >= CMDP_MON_STOPPING)
			data.dirty_status.valid_observations = 0;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.dirty_status, sizeof(data.dirty_status)))
			ret = -EFAULT;
		break;
	case CMDP_MON_DIRTY_PAGE:
		if (copy_from_user(&data.dirty_page, up, sizeof(data.dirty_page))) {
			ret = -EFAULT;
			break;
		}
		if (data.dirty_page.size != sizeof(data.dirty_page) ||
		    data.dirty_page.version != CMDP_MONITOR_VERSION ||
		    cmdp_validate_addr(data.dirty_page.addr)) {
			ret = -EINVAL;
			break;
		}
		h = xa_load(&m->history, data.dirty_page.addr >> PAGE_SHIFT);
		if (!h) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.dirty_page = h->dirty;
		data.dirty_page.size = sizeof(data.dirty_page);
		data.dirty_page.version = CMDP_MONITOR_VERSION;
		if (m->status.state >= CMDP_MON_STOPPING)
			data.dirty_page.valid = 0;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.dirty_page, sizeof(data.dirty_page)))
			ret = -EFAULT;
		break;
	case CMDP_MON_START:
	case CMDP_MON_SCAN_ONCE:
		spin_lock_irqsave(&m->lock, flags);
		if (!m->session || m->status.state != CMDP_MON_ATTACHED || m->scanning) {
			spin_unlock_irqrestore(&m->lock, flags);
			ret = -EBUSY;
			break;
		}
		if (cmd == CMDP_MON_START) {
			m->status.state = CMDP_MON_RUNNING;
			m->next_deadline = ktime_get_ns();
		} else {
			m->scanning = true;
		}
		spin_unlock_irqrestore(&m->lock, flags);
		if (cmd == CMDP_MON_START) {
			schedule_delayed_work(&m->work, 0);
			break;
		}
		/* STOP may mark cancellation while this ioctl performs its scan. */
		mutex_lock(&m->scan);
		mutex_unlock(&m->ctl);
		ret = cmdp_monitor_scan(m);
		spin_lock_irqsave(&m->lock, flags);
		m->scanning = false;
		spin_unlock_irqrestore(&m->lock, flags);
		mutex_unlock(&m->scan);
		if (ret && ret != -ESHUTDOWN && ret != -ESRCH)
			cmdp_monitor_stop(m, CMDP_MON_ERROR);
		return ret;
	case CMDP_MON_STATUS:
		if (copy_from_user(&data.status, up, sizeof(data.status))) {
			ret = -EFAULT;
			break;
		}
		if (data.status.size != sizeof(data.status) ||
		    data.status.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		cmdp_monitor_account(m, ktime_get_ns());
		data.status = m->status;
		data.status.size = sizeof(data.status);
		data.status.version = CMDP_MONITOR_VERSION;
		data.status.now_ns = ktime_get_ns();
		if (m->current_cohort)
			data.status.last_pass = m->current_cohort->report;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.status, sizeof(data.status)))
			ret = -EFAULT;
		break;
	case CMDP_MON_PROGRESS:
		if (copy_from_user(&data.progress, up, sizeof(data.progress))) {
			ret = -EFAULT;
			break;
		}
		if (data.progress.size != sizeof(data.progress) ||
		    data.progress.version != CMDP_MONITOR_VERSION || data.progress.reserved) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.progress = m->progress;
		data.progress.size = sizeof(data.progress);
		data.progress.version = CMDP_MONITOR_VERSION;
		data.progress.now_ns = ktime_get_ns();
		if (data.progress.scan_phase == CMDP_MON_SCAN_WAIT_MMAP)
			data.progress.scan_lock_wait_ns =
				data.progress.now_ns - data.progress.phase_since_ns;
		spin_unlock_irqrestore(&m->lock, flags);
		if (m->session) {
			struct cmdp_session *s = m->session;

			spin_lock_irqsave(&s->address_lock, flags);
			data.progress.lookup_calls = s->lookup_calls;
			data.progress.lookup_nodes = s->lookup_nodes;
			data.progress.lookup_max_nodes = s->lookup_max_nodes;
			data.progress.index_lock_max_ns = s->index_lock_max_ns;
			data.progress.write_calls = s->write_calls;
			data.progress.write_ns = s->write_ns;
			data.progress.write_max_ns = s->write_max_ns;
			spin_unlock_irqrestore(&s->address_lock, flags);
		}
		if (copy_to_user(up, &data.progress, sizeof(data.progress)))
			ret = -EFAULT;
		break;
	case CMDP_MON_DIAGNOSTICS:
		if (copy_from_user(&data.diagnostics, up, sizeof(data.diagnostics))) {
			ret = -EFAULT;
			break;
		}
		if (data.diagnostics.size != sizeof(data.diagnostics) ||
		    data.diagnostics.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.diagnostics = m->diagnostics;
		data.diagnostics.size = sizeof(data.diagnostics);
		data.diagnostics.version = CMDP_MONITOR_VERSION;
		data.diagnostics.now_ns = ktime_get_ns();
		data.diagnostics.unknown = m->dirty.unknown;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.diagnostics, sizeof(data.diagnostics)))
			ret = -EFAULT;
		break;
	case CMDP_MON_PAGE_SNAPSHOT:
		if (copy_from_user(&data.snapshot, up, sizeof(data.snapshot))) {
			ret = -EFAULT;
			break;
		}
		if (data.snapshot.size != sizeof(data.snapshot) ||
		    data.snapshot.version != CMDP_MONITOR_VERSION ||
		    cmdp_validate_addr(data.snapshot.addr)) {
			ret = -EINVAL;
			break;
		}
		ret = cmdp_monitor_page_snapshot(m, &data.snapshot);
		if (!ret && copy_to_user(up, &data.snapshot, sizeof(data.snapshot)))
			ret = -EFAULT;
		break;
	case CMDP_MON_PAGE:
		if (copy_from_user(&data.page, up, sizeof(data.page))) {
			ret = -EFAULT;
			break;
		}
		if (data.page.size != sizeof(data.page) ||
		    data.page.version != CMDP_MONITOR_VERSION ||
		    cmdp_validate_addr(data.page.addr)) {
			ret = -EINVAL;
			break;
		}
		h = xa_load(&m->history, data.page.addr >> PAGE_SHIFT);
		if (!h) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.page = h->page;
		data.page.size = sizeof(data.page);
		data.page.version = CMDP_MONITOR_VERSION;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.page, sizeof(data.page)))
			ret = -EFAULT;
		break;
	case CMDP_MON_COHORT:
		if (copy_from_user(&data.cohort, up, sizeof(data.cohort))) {
			ret = -EFAULT;
			break;
		}
		if (data.cohort.size != sizeof(data.cohort) ||
		    data.cohort.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		c = xa_load(&m->cohorts, data.cohort.pass_id);
		if (!c) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		data.cohort.pass = c->report;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &data.cohort, sizeof(data.cohort)))
			ret = -EFAULT;
		break;
	default:
		ret = -ENOTTY;
	}
	mutex_unlock(&m->ctl);
	return ret;
}

static int cmdp_monitor_open(struct inode *inode, struct file *file)
{
	struct cmdp_monitor *m;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	m = kzalloc_obj(*m);
	if (!m)
		return -ENOMEM;
	mutex_init(&m->ctl);
	mutex_init(&m->scan);
	spin_lock_init(&m->lock);
	xa_init(&m->history);
	xa_init(&m->cohorts);
	INIT_DELAYED_WORK(&m->work, cmdp_monitor_work);
	file->private_data = m;
	return 0;
}

static int cmdp_monitor_release(struct inode *inode, struct file *file)
{
	struct cmdp_monitor *m = file->private_data;
	void *entry;
	unsigned long index;

	cmdp_monitor_stop(m, CMDP_MON_CONTROL_STOP);
	if (m->session) {
		unsigned long flags;

		/* Custom lifecycle helpers hold mmap write from lookup through
		 * their final session put. Detach under that same exclusion so
		 * none can retain s->monitor past the subsequent unregister/free.
		 * mmgrab keeps this lock alive even after the target's last exit.
		 */
		mmap_write_lock(m->session->mm);
		xa_lock_irqsave(&cmdp_entries, flags);
		if (cmdp_owner == m->session)
			cmdp_owner = NULL;
		xa_unlock_irqrestore(&cmdp_entries, flags);
		mmap_write_unlock(m->session->mm);
		cmdp_session_close(m->session);
	}
	xa_for_each(&m->history, index, entry) {
		kfree(entry);
		cond_resched();
	}
	xa_destroy(&m->history);
	xa_for_each(&m->cohorts, index, entry) {
		kfree(entry);
		cond_resched();
	}
	xa_destroy(&m->cohorts);
	kfree(m);
	return 0;
}

static const struct file_operations cmdp_monitor_fops = {
	.owner = THIS_MODULE,
	.open = cmdp_monitor_open,
	.unlocked_ioctl = cmdp_monitor_ioctl,
	.release = cmdp_monitor_release,
};

static struct miscdevice cmdp_monitor_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "cmdp_monitor",
	.fops = &cmdp_monitor_fops,
	.mode = 0600,
};

static long cmdp_control_write(struct file *file, const char __user *buf,
				  size_t count, loff_t *ppos)
{
	struct cmdp_session *s = file->private_data;
	struct cmdp_entry *e;
	char text[96], command[24], extra;
	unsigned long value;
	int ret, fields;

	if (!count || count >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, count))
		return -EFAULT;
	text[count] = '\0';
	fields = sscanf(text, "%23s %lx %c", command, &value, &extra);
	if (fields != 2)
		return -EINVAL;
	if (!strcmp(command, "arm"))
		ret = cmdp_session_arm(s, value);
	else if (!strcmp(command, "revoke"))
		ret = cmdp_session_revoke(s, value);
#ifdef CONFIG_CMDP_TEST
	else {
		ret = cmdp_session_lock(s);
		if (ret)
			return ret;
		if (!strcmp(command, "test_pin") || !strcmp(command, "test_get")) {
			ret = cmdp_validate_addr(value);
			if (!ret)
				ret = cmdp_test_hold(s, value,
						    !strcmp(command, "test_pin"));
		} else if (!strcmp(command, "test_drop")) {
			cmdp_test_drop(s);
			ret = 0;
		} else if (!strcmp(command, "test_fail_after")) {
			s->fail_after = min_t(unsigned long, value,
					      CMDP_MAX_ALIASES);
			ret = 0;
		} else if (!strcmp(command, "test_delay_ms")) {
			WRITE_ONCE(s->revoke_delay_ms,
				   min_t(unsigned long, value, 1000));
			ret = 0;
		} else if (!strcmp(command, "test_stale")) {
			ret = cmdp_validate_addr(value);
			if (!ret) {
				e = cmdp_find_range(s, value, value + PAGE_SIZE);
				if (!e)
					ret = -ENOENT;
				else {
					ret = cmdp_hw_complete(e->pfn,
							e->generation - 1,
							CMDP_REVOKE, 0) ? -EIO : 0;
					cmdp_put(e);
				}
			}
		} else {
			ret = -EINVAL;
		}
		mutex_unlock(&s->commands);
	}
#else
	else
		ret = -EINVAL;
#endif
	return ret ? ret : count;
}

static int cmdp_control_open(struct inode *inode, struct file *file)
{
	struct cmdp_session *s;
	int ret = cmdp_session_open(current->mm, &s, NULL);

	if (!ret)
		file->private_data = s;
	return ret;
}

static int cmdp_control_release(struct inode *inode, struct file *file)
{
	return cmdp_session_close(file->private_data);
}

static const struct file_operations cmdp_control_fops = {
	.owner = THIS_MODULE,
	.open = cmdp_control_open,
	.write = cmdp_control_write,
	.release = cmdp_control_release,
};

static const struct file_operations cmdp_device_fops = {
	.owner = THIS_MODULE,
	.open = cmdp_device_open,
	.unlocked_ioctl = cmdp_device_ioctl,
	.release = cmdp_device_release,
};

static struct miscdevice cmdp_misc_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "cmdp",
	.fops = &cmdp_device_fops,
	.mode = 0600,
};

static int cmdp_stats_show(struct seq_file *m, void *unused)
{
	unsigned int i;

	for (i = 0; i < CMDP_NR_STATS; i++)
		seq_printf(m, "%s %lld\n", cmdp_stat_names[i],
			   atomic64_read(&cmdp_stats[i]));
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(cmdp_stats);

static int __init cmdp_init(void)
{
	struct dentry *dir = debugfs_create_dir("cmdp", NULL);
	int ret;

	ret = misc_register(&cmdp_misc_device);
	if (ret)
		return ret;
	ret = misc_register(&cmdp_monitor_device);
	if (ret) {
		misc_deregister(&cmdp_misc_device);
		return ret;
	}
	debugfs_create_file("control", 0600, dir, NULL, &cmdp_control_fops);
	debugfs_create_file("stats", 0400, dir, NULL, &cmdp_stats_fops);
	return 0;
}
late_initcall(cmdp_init);
