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
	ISOLATED, STALE, ARM_NS, REVOKE_NS, CMDP_NR_STATS,
};
static const char * const cmdp_stat_names[] = {
	"arms", "publishes", "revokes", "waiters", "rejects", "entries",
	"managed", "isolated", "stale", "arm_ns", "revoke_ns",
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
	struct cmdp_cohort *current_cohort;
	u64 next_lifetime;
	u64 account_ns;
	u64 next_deadline;
	u32 max_pages, max_history;
	bool scanning;
	bool stop_snapshot;
};
static atomic64_t cmdp_monitor_ids = ATOMIC64_INIT(0);
static void cmdp_monitor_terminal(struct cmdp_entry *e, const char *reason);
static void cmdp_monitor_armed(struct cmdp_entry *e);
static void cmdp_monitor_invalidate_history(struct cmdp_session *s,
		unsigned long start, unsigned long end);
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

static int cmdp_revoke(struct cmdp_entry *e, const char *reason)
{
	int state, ret;
	u64 start;

	state = atomic_cmpxchg(&e->state, CMDP_ACTIVE, CMDP_REVOKING);
	if (state == CMDP_ARMING) {
		/* Only session shutdown can join arming; faults cannot get here. */
		wait_for_completion(&e->arm_done);
		return cmdp_revoke(e, reason);
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

static void cmdp_range_end(struct cmdp_session *s, u64 start)
{
	struct cmdp_monitor *m = s->monitor;
	unsigned long flags;
	u64 elapsed = ktime_get_ns() - start;

	if (m) {
		spin_lock_irqsave(&m->lock, flags);
		m->progress.range_inflight--;
		m->progress.range_ns += elapsed;
		m->progress.range_max_ns = max(m->progress.range_max_ns, elapsed);
		spin_unlock_irqrestore(&m->lock, flags);
	}
}

static int cmdp_invalidate(struct mmu_notifier *mn,
			   const struct mmu_notifier_range *range)
{
	struct cmdp_session *s = container_of(mn, struct cmdp_session, notifier);
	struct cmdp_entry *e;
	u64 start;
	int ret = 0;

	/* Our own alias protection notification, outside PTL/rmap locks. */
	if (range->owner == s && range->event == MMU_NOTIFY_PROTECTION_PAGE)
		return 0;

	start = cmdp_range_begin(s);
	cmdp_monitor_invalidate_history(s, range->start, range->end);
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
		ret = cmdp_revoke(e, "invalidate");
		if (ret) {
			/* Blockable invalidation cannot veto destruction. Fail closed. */
			wait_for_completion(&e->terminal);
			ret = 0;
		}
		cmdp_put(e);
		cond_resched();
	}
	cmdp_range_end(s, start);
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

static int cmdp_protect_alias(struct cmdp_entry *e, struct cmdp_alias *alias)
{
	struct vm_area_struct *vma = alias->vma;
	struct mmu_notifier_range range;
	DEFINE_FOLIO_VMA_WALK(pvmw, page_folio(e->page), vma,
			      alias->addr, PVMW_SYNC);
	pte_t pte;
	int ret = -EAGAIN;

	mmu_notifier_range_init_owner(&range, MMU_NOTIFY_PROTECTION_PAGE, 0,
			e->mm, alias->addr, alias->addr + PAGE_SIZE, e->session);
	mmu_notifier_invalidate_range_start(&range);
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

	/* Atomic clear preserves hardware A/D updates; flush before reinstall. */
	pte = ptep_clear_flush(vma, alias->addr, pvmw.pte);
	set_pte_at(e->mm, alias->addr, pvmw.pte, pte_wrprotect(pte));
	e->protected++;
	ret = 0;
unlock:
	page_vma_mapped_walk_done(&pvmw);
end:
	mmu_notifier_invalidate_range_end(&range);
	return ret;
}

static int cmdp_arm_locked(struct cmdp_session *s, unsigned long addr,
		struct cmdp_history *history, struct cmdp_cohort *cohort,
		const char **failure)
{
	struct cmdp_entry *e;
	struct vm_area_struct *vma;
	struct folio *folio = NULL;
	struct cmdp_walk walk;
	struct rmap_walk_control rwc = { .rmap_one = cmdp_collect_alias };
	unsigned long flags;
	unsigned int i;
	u64 start = ktime_get_ns();
	const char *why = "vma";
	bool seq_started = false, folio_locked = false;
	int ret;
#ifdef CONFIG_CMDP_TEST
	unsigned int fail_after = s->fail_after;

	s->fail_after = 0;
#endif

	e = kzalloc_obj(*e);
	if (!e)
		return -ENOMEM;
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
		goto put;
	}
	vma = vma_lookup(s->mm, addr);
	if (!vma || !cmdp_vma_supported(vma)) {
		ret = -EOPNOTSUPP;
		goto unlock_mm;
	}
	why = "nofault";
	if (s->monitor) {
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
		goto unlock_mm;
	}
	e->pfn = page_to_pfn(e->page);
	folio = page_folio(e->page);
	why = "page_type";
	ret = -EOPNOTSUPP;
	if (folio_test_large(folio) || !folio_test_anon(folio) ||
	    folio_test_ksm(folio) || folio_test_swapcache(folio) ||
	    PageReserved(e->page) || is_zone_device_page(e->page) ||
	    PageHWPoison(e->page) || !PageAnonExclusive(e->page))
		goto cleanup;
	/* Putback may still be queued in a per-CPU LRU batch. */
	if (!folio_test_lru(folio))
		lru_add_drain_all();
	why = "folio_busy";
	ret = -EBUSY;
	if (!folio_trylock(folio))
		goto cleanup;
	folio_locked = true;
	if (PageCmdp(e->page))
		goto cleanup;
	why = "aliases";
	walk = (struct cmdp_walk) { .entry = e };
	rwc.arg = &walk;
	rmap_walk(folio, &rwc);
	ret = walk.error;
	if (ret)
		goto cleanup;
	ret = -EAGAIN;
	if (!e->nr_aliases || e->nr_aliases != folio_mapcount(folio))
		goto cleanup;
	/* rmap walk is finished before taking any VMA write exclusion. */
	for (i = 0; i < e->nr_aliases; i++)
		vma_start_write(e->aliases[i].vma);
	if (e->nr_aliases != folio_mapcount(folio))
		goto cleanup;
	why = "isolate";
	ret = -EBUSY;
	if (!folio_isolate_lru(folio))
		goto cleanup;
	e->isolated = true;
	atomic64_inc(&cmdp_stats[ISOLATED]);

	/* Reserve allocation before publishing; no sleeping under xa_lock. */
	why = "registry";
	ret = xa_reserve(&cmdp_entries, e->pfn, GFP_KERNEL);
	if (ret)
		goto cleanup;
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
		goto cleanup;
	trace_cmdp_event(e->pfn, e->generation, CMDP_TERMINAL, CMDP_ARMING,
			"arm", 0, 0);

	raw_write_seqcount_begin(&s->mm->write_protect_seq);
	seq_started = true;
	why = "pte_changed";
	for (i = 0; i < e->nr_aliases; i++) {
		ret = cmdp_protect_alias(e, &e->aliases[i]);
		if (ret)
			goto cleanup;
#ifdef CONFIG_CMDP_TEST
		if (fail_after && e->protected == fail_after) {
			why = "injected_after_alias";
			ret = -EIO;
			goto cleanup;
		}
#endif
	}
	/* PTE clear/flush before counts, paired with fast-GUP pin ordering. */
	smp_mb();
	why = "pins_or_refs";
	ret = -EBUSY;
	if (folio_maybe_dma_pinned(folio) ||
	    folio_ref_count(folio) != folio_expected_ref_count(folio) + 2)
		goto cleanup;
	/* +2 = manager GUP reference and the LRU isolation reference. */
	/*
	 * write_protect_seq only covers PTE modification versus fast FOLL_PIN.
	 * Once aliases are RO, TLBs are flushed and the final pin/reference
	 * check has completed, end the sequence before any hardware wait.
	 */
	raw_write_seqcount_end(&s->mm->write_protect_seq);
	seq_started = false;

	why = "publish_failed";
	ret = cmdp_hw_publish(e);
	if (ret) {
		/* Even partial publication must be drained before removing membership. */
		atomic_set(&e->state, CMDP_REVOKING);
		if (cmdp_hw_revoke(e))
			wait_for_completion(&e->terminal); /* future backend recovery */
		goto cleanup;
	}
	cmdp_monitor_armed(e);
	atomic_set(&e->state, CMDP_ACTIVE);
	atomic64_inc(&cmdp_stats[ARMS]);
	trace_cmdp_event(e->pfn, e->generation, CMDP_ARMING, CMDP_ACTIVE,
			"armed", e->protected, 0);
cleanup:
	/* Exactly one end for every path following sequence begin. */
	if (seq_started)
		raw_write_seqcount_end(&s->mm->write_protect_seq);
	if (folio_locked)
		folio_unlock(folio);
	if (ret && e->registered)
		cmdp_finish(e, "arm_abort");
	else if (ret && e->page) {
		if (e->isolated) {
			folio_putback_lru(folio);
			e->isolated = false;
			atomic64_dec(&cmdp_stats[ISOLATED]);
		}
		put_page(e->page);
		e->page = NULL;
	}
unlock_mm:
	/* VMA pointers must never be used by ACTIVE callbacks. */
	for (i = 0; i < e->nr_aliases; i++)
		e->aliases[i].vma = NULL;
	complete_all(&e->arm_done);
put:
	if (failure)
		*failure = why;
	if (ret) {
		atomic64_inc(&cmdp_stats[REJECTS]);
		trace_cmdp_event(e->pfn, e->generation, atomic_read(&e->state),
				CMDP_TERMINAL, why, e->protected, ret);
	}
	atomic64_add(ktime_get_ns() - start, &cmdp_stats[ARM_NS]);
	cmdp_put(e);
	return ret;
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

/* Ordinary RW only: pte_write() also accepts CET shadow-stack encodings. */
static bool cmdp_dirty_pte_supported(pte_t pte)
{
	return pte_present(pte) && !pte_protnone(pte) &&
		(pte_flags(pte) & _PAGE_RW) &&
		!(pte_flags(pte) & _PAGE_SAVED_DIRTY);
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

static void cmdp_monitor_invalidate_history(struct cmdp_session *s,
		unsigned long start, unsigned long end)
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
		if (h->dirty.valid)
			m->dirty.invalidations++;
		cmdp_dirty_invalidate_locked(m, h);
		if (h->continuous) {
			h->continuous = false;
			m->status.history_resets++;
		}
		if (h->page.state != CMDP_MON_PAGE_ARMED &&
		    h->page.state != CMDP_MON_PAGE_STOPPED) {
			h->page.state = CMDP_MON_PAGE_INVALIDATED;
		}
		spin_unlock_irqrestore(&m->lock, flags);
	}
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
int cmdp_mm_modify(struct mm_struct *mm, unsigned long start, unsigned long end)
{
	struct cmdp_session *s;
	struct cmdp_entry *e;
	u64 begin;
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
	cmdp_monitor_invalidate_history(s, start, end);
	while ((e = cmdp_find_range(s, start, end))) {
		ret = cmdp_revoke(e, "invalidate");
		cmdp_put(e);
		if (ret)
			break;
		cond_resched();
	}
	cmdp_range_end(s, begin);
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

static int cmdp_monitor_probe(struct cmdp_session *s, unsigned long addr,
		bool *resident, unsigned long *pfn)
{
	struct page *page = NULL;
	struct folio *folio;
	struct vm_area_struct *vma = vma_lookup(s->mm, addr);
	int ret;

	page = cmdp_monitor_get_page(vma, addr, resident, &ret);
	if (!page)
		return ret;
	*pfn = page_to_pfn(page);
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
	put_page(page);
	return ret;
}

/* Return 1 only for a clean, qualified candidate; 0 means keep observing.
 * CMDP is native X86_64 (!XEN_PV). No generic PTE replacement is used here:
 * locked bit-test-and-reset preserves A, permissions, PFN and software bits
 * while hardware can update A/D concurrently. The ordinary RW/saved-dirty
 * checks above make _PAGE_BIT_DIRTY the sole supported dirty encoding.
 *
 * The folio lock excludes reclaim/migration until epoch publication. mmap
 * write plus vma_start_write excludes mapping changes and per-VMA faults.
 * The PTL then stabilizes the exact PTE through reset, synchronous shootdown
 * and publication. An invalidation sequence additionally rejects evidence
 * disturbed by a notifier. These locks do NOT stop user stores: the reset to
 * flush-completion interval is deliberately outside the certified window.
 */
static int cmdp_monitor_sample_dirty(struct cmdp_monitor *m,
		struct vm_area_struct *vma, unsigned long addr, unsigned long pfn,
		struct cmdp_history *h)
{
	struct page *page;
	struct folio *folio;
	struct page_vma_mapped_walk pvmw;
	unsigned long flags;
	u64 start = ktime_get_ns(), seq, now, reset_ns = 0, flush_ns = 0;
	bool resident, dirty, reset, valid;
	int skip, ret = -EAGAIN;
	pte_t pte;

	mmap_assert_write_locked(vma->vm_mm);
	vma_start_write(vma);
	page = cmdp_monitor_get_page(vma, addr, &resident, &skip);
	if (!page)
		goto unknown;
	folio = page_folio(page);
	if (page_to_pfn(page) != pfn || !folio_trylock(folio))
		goto put;
	/* Revalidate the probe after acquiring the folio lock. Do not retain
	 * page ownership between samples or change LRU/reclaim eligibility.
	 */
	if (folio_test_large(folio) || !folio_test_anon(folio) ||
	    folio_test_ksm(folio) || folio_test_swapcache(folio) ||
	    PageReserved(page) || is_zone_device_page(page) ||
	    PageHWPoison(page) || !PageAnonExclusive(page) || PageCmdp(page) ||
	    folio_mapcount(folio) != 1 || folio_maybe_dma_pinned(folio) ||
	    folio_ref_count(folio) != folio_expected_ref_count(folio) + 1)
		goto unlock_folio;
	pvmw = (struct page_vma_mapped_walk) {
		.pfn = pfn, .nr_pages = 1, .pgoff = folio_pgoff(folio),
		.vma = vma, .address = addr, .flags = PVMW_SYNC,
	};
	if (!page_vma_mapped_walk(&pvmw))
		goto unlock_folio;
	if (!pvmw.pte || pvmw.address != addr)
		goto unlock_pte;
	pte = ptep_get(pvmw.pte);
	if (!cmdp_dirty_pte_supported(pte) ||
	    vm_normal_page(vma, addr, pte) != page)
		goto unlock_pte;
	dirty = pte_dirty(pte);
	spin_lock_irqsave(&m->lock, flags);
	seq = h->observation_seq;
	valid = h->dirty.valid && h->dirty.pfn == pfn;
	spin_unlock_irqrestore(&m->lock, flags);
	reset = !valid || dirty;
	if (reset) {
		u64 before = ktime_get_ns();

		/* Preserve Linux dirty/reclaim accounting before discarding D.
		 * Conservative marking also covers a store racing the initial read.
		 */
		folio_mark_dirty(folio);
		dirty |= test_and_clear_bit(_PAGE_BIT_DIRTY,
					   (unsigned long *)&pvmw.pte->pte);
		reset_ns = ktime_get_ns() - before;
		before = ktime_get_ns();
		/* Required even when the initial D read was zero: an old cached
		 * dirty translation must not survive into a new observation epoch.
		 * This also invokes the architecture secondary-TLB notification.
		 */
		flush_tlb_page(vma, addr);
		flush_ns = ktime_get_ns() - before;
	}
	now = ktime_get_ns();
	spin_lock_irqsave(&m->lock, flags);
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
		m->dirty.flushes++;
		m->dirty.reset_ns += reset_ns;
		m->dirty.flush_ns += flush_ns;
		h->dirty.resets++;
	}
	if (seq != h->observation_seq) {
		cmdp_dirty_invalidate_locked(m, h);
		m->dirty.unknown++;
		ret = 0;
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
		ret = 0;
	} else {
		ret = now - h->dirty.quiet_since_ns >=
			(u64)m->dirty.quiet_ms * NSEC_PER_MSEC;
		if (ret && !h->dirty.qualified_ns) {
			h->dirty.qualified_ns = now;
			m->dirty.qualified++;
		}
	}
	m->dirty.sample_ns += ktime_get_ns() - start;
	spin_unlock_irqrestore(&m->lock, flags);
	page_vma_mapped_walk_done(&pvmw);
	folio_unlock(folio);
	put_page(page);
	return ret;
unlock_pte:
	page_vma_mapped_walk_done(&pvmw);
unlock_folio:
	folio_unlock(folio);
put:
	put_page(page);
unknown:
	spin_lock_irqsave(&m->lock, flags);
	cmdp_dirty_invalidate_locked(m, h);
	m->dirty.unknown++;
	m->dirty.sample_ns += ktime_get_ns() - start;
	spin_unlock_irqrestore(&m->lock, flags);
	return ret;
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
	struct cmdp_history *h;
	struct vm_area_struct *vma;
	unsigned long addr = 0, flags, pfn = 0;
	unsigned int batch, skip;
	bool resident;
	int ret = 0, probe;
	const char *why;

	c = kzalloc_obj(*c);
	if (!c) {
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
		for (batch = 0; batch < CMDP_MON_BATCH && addr < TASK_SIZE_MAX; batch++) {
			if (READ_ONCE(s->closing) ||
			    READ_ONCE(m->status.state) >= CMDP_MON_STOPPING) {
				ret = -ESHUTDOWN;
				break;
			}
			vma = find_vma(s->mm, addr);
			if (!vma) {
				addr = TASK_SIZE_MAX;
				break;
			}
			addr = max(addr, vma->vm_start);
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
				spin_lock_irqsave(&m->lock, flags);
				c->report.writable_pages += pages;
				c->report.examined_pages += pages;
				c->report.skipped[skip] += pages;
				spin_unlock_irqrestore(&m->lock, flags);
				if (m->dirty.mode)
					cmdp_monitor_invalidate_history(s, addr, vma->vm_end);
				addr = vma->vm_end;
				continue;
			}
			probe = cmdp_monitor_probe(s, addr, &resident, &pfn);
			spin_lock_irqsave(&m->lock, flags);
			c->report.writable_pages++;
			c->report.examined_pages++;
			c->report.resident_pages += resident;
			if (probe >= 0)
				c->report.skipped[probe]++;
			else
				c->report.eligible_pages++;
			spin_unlock_irqrestore(&m->lock, flags);
			if (probe >= 0 || (m->status.flags & CMDP_MONITOR_F_SCAN_ONLY)) {
				if (m->dirty.mode) {
					cmdp_monitor_invalidate_history(s, addr, addr + PAGE_SIZE);
					spin_lock_irqsave(&m->lock, flags);
					m->dirty.unknown++;
					spin_unlock_irqrestore(&m->lock, flags);
				}
				goto next;
			}
			h = xa_load(&m->history, addr >> PAGE_SHIFT);
			spin_lock_irqsave(&m->lock, flags);
			if (h && h->page.state == CMDP_MON_PAGE_ARMED) {
				c->report.already_armed++;
				spin_unlock_irqrestore(&m->lock, flags);
				goto next;
			}
			if ((!m->dirty.mode && m->status.active >= m->max_pages) ||
			    (!h && m->status.history_entries >= m->max_history)) {
				c->report.skipped[CMDP_MON_SKIP_RESOURCE]++;
				m->status.resource_failures++;
				if (m->dirty.mode) {
					m->dirty.resource_skips++;
					m->dirty.unknown++;
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
				probe = cmdp_monitor_sample_dirty(m, vma, addr, pfn, h);
				if (probe <= 0 || m->dirty.mode == CMDP_MON_DIRTY_OBSERVE)
					goto next;
				spin_lock_irqsave(&m->lock, flags);
				if (m->status.active >= m->max_pages) {
					cmdp_dirty_invalidate_locked(m, h);
					spin_unlock_irqrestore(&m->lock, flags);
					goto resource;
				}
				spin_unlock_irqrestore(&m->lock, flags);
			}
			why = "allocation";
			probe = cmdp_arm_locked(s, addr, h, c, &why);
			if (probe) {
				skip = cmdp_monitor_failure(probe, why);
				spin_lock_irqsave(&m->lock, flags);
				if (m->dirty.mode)
					cmdp_dirty_invalidate_locked(m, h);
				c->report.skipped[skip]++;
				if (skip == CMDP_MON_SKIP_RESOURCE)
					m->status.resource_failures++;
				spin_unlock_irqrestore(&m->lock, flags);
			}
			goto next;
resource:
			spin_lock_irqsave(&m->lock, flags);
			if (m->dirty.mode) {
				m->dirty.resource_skips++;
				m->dirty.unknown++;
			}
			c->report.skipped[CMDP_MON_SKIP_RESOURCE]++;
			m->status.resource_failures++;
			spin_unlock_irqrestore(&m->lock, flags);
next:
			addr += PAGE_SIZE;
		}
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

#ifdef CONFIG_CMDP_TEST
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
	struct cmdp_monitor_attach a;
	struct cmdp_monitor_status status;
	struct cmdp_monitor_page page;
	struct cmdp_monitor_cohort cohort;
	struct cmdp_monitor_progress progress;
	struct cmdp_monitor_dirty_config dirty_config;
	struct cmdp_monitor_dirty_status dirty_status;
	struct cmdp_monitor_dirty_page dirty_page;
	struct cmdp_history *h;
	struct cmdp_cohort *c;
	unsigned long flags;
	int ret = 0;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
#ifdef CONFIG_CMDP_TEST
	if (cmd == CMDP_MON_TEST_HOLD_MMAP)
		return cmdp_monitor_test_hold_mmap(m, up);
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
	case CMDP_MON_ATTACH:
		if (copy_from_user(&a, up, sizeof(a))) {
			ret = -EFAULT;
			break;
		}
		ret = cmdp_monitor_attach_target(m, &a);
		break;
	case CMDP_MON_DIRTY_CONFIG:
		if (copy_from_user(&dirty_config, up, sizeof(dirty_config))) {
			ret = -EFAULT;
			break;
		}
		if (dirty_config.size != sizeof(dirty_config) ||
		    dirty_config.version != CMDP_MONITOR_VERSION ||
		    dirty_config.mode > CMDP_MON_DIRTY_QUALIFY ||
		    (dirty_config.quiet_ms && dirty_config.quiet_ms < 10) ||
		    dirty_config.quiet_ms > 3600000 ||
		    dirty_config.reserved[0] || dirty_config.reserved[1] ||
		    (dirty_config.mode && (m->status.flags & CMDP_MONITOR_F_SCAN_ONLY))) {
			ret = -EINVAL;
			break;
		}
		if (!m->session || m->status.state != CMDP_MON_ATTACHED ||
		    m->scanning || m->status.passes_started) {
			ret = -EBUSY;
			break;
		}
		m->dirty.mode = dirty_config.mode;
		m->dirty.quiet_ms = dirty_config.quiet_ms ?: 10000;
		break;
	case CMDP_MON_DIRTY_STATUS:
		if (copy_from_user(&dirty_status, up, sizeof(dirty_status))) {
			ret = -EFAULT;
			break;
		}
		if (dirty_status.size != sizeof(dirty_status) ||
		    dirty_status.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		dirty_status = m->dirty;
		dirty_status.size = sizeof(dirty_status);
		dirty_status.version = CMDP_MONITOR_VERSION;
		dirty_status.now_ns = ktime_get_ns();
		dirty_status.metadata_bytes = m->status.history_entries * sizeof(*h);
		/* STOP invalidates the session observation contract immediately,
		 * without a potentially unbounded walk of never-ARMED histories.
		 */
		if (m->status.state >= CMDP_MON_STOPPING)
			dirty_status.valid_observations = 0;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &dirty_status, sizeof(dirty_status)))
			ret = -EFAULT;
		break;
	case CMDP_MON_DIRTY_PAGE:
		if (copy_from_user(&dirty_page, up, sizeof(dirty_page))) {
			ret = -EFAULT;
			break;
		}
		if (dirty_page.size != sizeof(dirty_page) ||
		    dirty_page.version != CMDP_MONITOR_VERSION ||
		    cmdp_validate_addr(dirty_page.addr)) {
			ret = -EINVAL;
			break;
		}
		h = xa_load(&m->history, dirty_page.addr >> PAGE_SHIFT);
		if (!h) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		dirty_page = h->dirty;
		dirty_page.size = sizeof(dirty_page);
		dirty_page.version = CMDP_MONITOR_VERSION;
		if (m->status.state >= CMDP_MON_STOPPING)
			dirty_page.valid = 0;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &dirty_page, sizeof(dirty_page)))
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
		if (copy_from_user(&status, up, sizeof(status))) {
			ret = -EFAULT;
			break;
		}
		if (status.size != sizeof(status) || status.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		cmdp_monitor_account(m, ktime_get_ns());
		status = m->status;
		status.size = sizeof(status);
		status.version = CMDP_MONITOR_VERSION;
		status.now_ns = ktime_get_ns();
		if (m->current_cohort)
			status.last_pass = m->current_cohort->report;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &status, sizeof(status)))
			ret = -EFAULT;
		break;
	case CMDP_MON_PROGRESS:
		if (copy_from_user(&progress, up, sizeof(progress))) {
			ret = -EFAULT;
			break;
		}
		if (progress.size != sizeof(progress) ||
		    progress.version != CMDP_MONITOR_VERSION || progress.reserved) {
			ret = -EINVAL;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		progress = m->progress;
		progress.size = sizeof(progress);
		progress.version = CMDP_MONITOR_VERSION;
		progress.now_ns = ktime_get_ns();
		if (progress.scan_phase == CMDP_MON_SCAN_WAIT_MMAP)
			progress.scan_lock_wait_ns = progress.now_ns - progress.phase_since_ns;
		spin_unlock_irqrestore(&m->lock, flags);
		if (m->session) {
			struct cmdp_session *s = m->session;

			spin_lock_irqsave(&s->address_lock, flags);
			progress.lookup_calls = s->lookup_calls;
			progress.lookup_nodes = s->lookup_nodes;
			progress.lookup_max_nodes = s->lookup_max_nodes;
			progress.index_lock_max_ns = s->index_lock_max_ns;
			progress.write_calls = s->write_calls;
			progress.write_ns = s->write_ns;
			progress.write_max_ns = s->write_max_ns;
			spin_unlock_irqrestore(&s->address_lock, flags);
		}
		if (copy_to_user(up, &progress, sizeof(progress)))
			ret = -EFAULT;
		break;
	case CMDP_MON_PAGE:
		if (copy_from_user(&page, up, sizeof(page))) {
			ret = -EFAULT;
			break;
		}
		if (page.size != sizeof(page) || page.version != CMDP_MONITOR_VERSION ||
		    cmdp_validate_addr(page.addr)) {
			ret = -EINVAL;
			break;
		}
		h = xa_load(&m->history, page.addr >> PAGE_SHIFT);
		if (!h) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		page = h->page;
		page.size = sizeof(page);
		page.version = CMDP_MONITOR_VERSION;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &page, sizeof(page)))
			ret = -EFAULT;
		break;
	case CMDP_MON_COHORT:
		if (copy_from_user(&cohort, up, sizeof(cohort))) {
			ret = -EFAULT;
			break;
		}
		if (cohort.size != sizeof(cohort) || cohort.version != CMDP_MONITOR_VERSION) {
			ret = -EINVAL;
			break;
		}
		c = xa_load(&m->cohorts, cohort.pass_id);
		if (!c) {
			ret = -ENOENT;
			break;
		}
		spin_lock_irqsave(&m->lock, flags);
		cohort.pass = c->report;
		spin_unlock_irqrestore(&m->lock, flags);
		if (copy_to_user(up, &cohort, sizeof(cohort)))
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
