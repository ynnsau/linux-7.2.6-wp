// SPDX-License-Identifier: GPL-2.0
/* Restricted native x86 CMD-P experiment. No device protocol is implemented. */
#include <linux/capability.h>
#include <linux/cmdp.h>
#include <linux/completion.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/init.h>
#include <linux/ktime.h>
#include <linux/mm.h>
#include <linux/mmu_notifier.h>
#include <linux/module.h>
#include <linux/pagemap.h>
#include <linux/refcount.h>
#include <linux/rmap.h>
#include <linux/sched/mm.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/swap.h>
#include <linux/uaccess.h>
#include <linux/userfaultfd_k.h>
#include <linux/xarray.h>

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
struct cmdp_alias {
	unsigned long addr;
	/* Only dereferenced while arming under mmap write lock. */
	struct vm_area_struct *vma;
};

struct cmdp_entry {
	struct page *page;
	unsigned long pfn;
	struct mm_struct *mm;
	struct cmdp_session *session;
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
	struct list_head link;
	unsigned int nr_aliases;
	unsigned int protected;
	struct cmdp_alias aliases[CMDP_MAX_ALIASES];
};

struct cmdp_session {
	struct mmu_notifier notifier;
	struct mm_struct *mm;
	refcount_t refs;
	struct mutex commands;
	/* The registry lock also protects this list, closing and cmdp_owner. */
	struct list_head entries;
	bool closing;
#ifdef CONFIG_CMDP_TEST
	struct page *test_page;
	bool test_pinned;
	unsigned int fail_after;
	unsigned int revoke_delay_ms;
#endif
};
static struct cmdp_session *cmdp_owner;

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
		WARN_ON_ONCE(e->registered || e->page || e->isolated);
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
	active = cmdp_owner && cmdp_owner->mm == mm &&
		 !list_empty(&cmdp_owner->entries);
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

/* Exactly one teardown owner; no MM locks are acquired here. */
static void cmdp_finish(struct cmdp_entry *e, const char *reason)
{
	struct page *page = e->page;
	unsigned long flags;
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
	xa_lock_irqsave(&cmdp_entries, flags);
	list_del_init(&e->link);
	xa_unlock_irqrestore(&cmdp_entries, flags);
	/* Waiters must not refault until the extra page ownership is gone. */
	complete_all(&e->terminal);
	cmdp_put(e); /* registry */
	cmdp_put(e); /* session association list */
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
	/* ARMING owns mmap write and every alias's VMA write exclusion. */
	if (WARN_ON_ONCE(atomic_read(&e->state) == CMDP_ARMING))
		return -EBUSY;
	return cmdp_revoke(e, "write");
}

static bool cmdp_overlaps(struct cmdp_entry *e, unsigned long start,
			  unsigned long end)
{
	unsigned int i;

	for (i = 0; i < e->nr_aliases; i++)
		if (e->aliases[i].addr < end &&
		    e->aliases[i].addr + PAGE_SIZE > start)
			return true;
	return false;
}

static struct cmdp_entry *cmdp_find_range(struct cmdp_session *s,
		unsigned long start, unsigned long end)
{
	struct cmdp_entry *e, *found = NULL;
	unsigned long flags;

	xa_lock_irqsave(&cmdp_entries, flags);
	list_for_each_entry(e, &s->entries, link) {
		if (cmdp_overlaps(e, start, end)) {
			refcount_inc(&e->refs);
			found = e;
			break;
		}
	}
	xa_unlock_irqrestore(&cmdp_entries, flags);
	return found;
}

static int cmdp_invalidate(struct mmu_notifier *mn,
			   const struct mmu_notifier_range *range)
{
	struct cmdp_session *s = container_of(mn, struct cmdp_session, notifier);
	struct cmdp_entry *e;
	int ret;

	/* Our own alias protection notification, outside PTL/rmap locks. */
	if (range->owner == s && range->event == MMU_NOTIFY_PROTECTION_PAGE)
		return 0;

	while ((e = cmdp_find_range(s, range->start, range->end))) {
		if (!mmu_notifier_range_blockable(range)) {
			cmdp_put(e);
			return -EAGAIN;
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
		}
		cmdp_put(e);
	}
	return 0;
}

static void cmdp_release_mm(struct mmu_notifier *mn, struct mm_struct *mm)
{
	struct cmdp_session *s = container_of(mn, struct cmdp_session, notifier);
	struct cmdp_entry *e;
	unsigned long flags;

	xa_lock_irqsave(&cmdp_entries, flags);
	s->closing = true;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	while ((e = cmdp_find_range(s, 0, ULONG_MAX))) {
		if (cmdp_revoke(e, "release"))
			wait_for_completion(&e->terminal);
		cmdp_put(e);
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
			VM_PFNMAP | VM_MIXEDMAP | VM_IO | VM_MERGEABLE)) &&
		!userfaultfd_armed(vma) &&
		(!IS_ENABLED(CONFIG_TRANSPARENT_HUGEPAGE) ||
		 (vma->vm_flags & VM_NOHUGEPAGE));
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

static int cmdp_arm(struct cmdp_session *s, unsigned long addr)
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
	e->generation = atomic64_inc_return(&cmdp_generation);
	refcount_set(&e->refs, 1);
	refcount_inc(&s->refs);
	atomic64_inc(&cmdp_stats[ENTRIES]);
	atomic_set(&e->state, CMDP_ARMING);
	init_completion(&e->terminal);
	init_completion(&e->arm_done);
	init_completion(&e->hw_completion);
	INIT_LIST_HEAD(&e->link);
	ret = mmap_write_lock_killable(s->mm);
	if (ret)
		goto put;
	vma = vma_lookup(s->mm, addr);
	if (!vma || !cmdp_vma_supported(vma)) {
		ret = -EOPNOTSUPP;
		goto unlock_mm;
	}
	why = "nofault";
	ret = get_user_pages(addr, 1, FOLL_NOFAULT, &e->page);
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
		ret = xa_err(__xa_store(&cmdp_entries, e->pfn, e, GFP_NOWAIT));
		if (!ret) {
			refcount_add(2, &e->refs); /* table + session list */
			list_add_tail(&e->link, &s->entries);
			e->registered = true;
			SetPageCmdp(e->page);
			atomic64_inc(&cmdp_stats[MANAGED]);
		}
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
	mmap_write_unlock(s->mm);
put:
	if (ret) {
		atomic64_inc(&cmdp_stats[REJECTS]);
		trace_cmdp_event(e->pfn, e->generation, atomic_read(&e->state),
				CMDP_TERMINAL, why, e->protected, ret);
	}
	atomic64_add(ktime_get_ns() - start, &cmdp_stats[ARM_NS]);
	cmdp_put(e);
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

static ssize_t cmdp_control_write(struct file *file, const char __user *buf,
				  size_t count, loff_t *ppos)
{
	struct cmdp_session *s = file->private_data;
	struct cmdp_entry *e;
	char text[96], command[24], extra;
	unsigned long value, flags;
	int ret, fields;

	if (!capable(CAP_SYS_ADMIN) || current->mm != s->mm)
		return -EPERM;
	if (!count || count >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, count))
		return -EFAULT;
	text[count] = '\0';
	fields = sscanf(text, "%23s %lx %c", command, &value, &extra);
	if (fields != 2)
		return -EINVAL;
	if (mutex_lock_killable(&s->commands))
		return -EINTR;
	xa_lock_irqsave(&cmdp_entries, flags);
	ret = s->closing ? -ESHUTDOWN : 0;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	if (ret)
		goto out;

	if (!strcmp(command, "arm") || !strcmp(command, "revoke")) {
		if (!PAGE_ALIGNED(value) || value >= TASK_SIZE_MAX) {
			ret = -EINVAL;
			goto out;
		}
		if (!strcmp(command, "arm")) {
			ret = cmdp_arm(s, value);
		} else {
			e = cmdp_find_range(s, value, value + PAGE_SIZE);
			ret = e ? cmdp_revoke(e, "command") : -ENOENT;
			if (e)
				cmdp_put(e);
		}
#ifdef CONFIG_CMDP_TEST
	} else if (!strcmp(command, "test_pin") || !strcmp(command, "test_get")) {
		ret = cmdp_test_hold(s, value, !strcmp(command, "test_pin"));
	} else if (!strcmp(command, "test_drop")) {
		cmdp_test_drop(s);
	} else if (!strcmp(command, "test_fail_after")) {
		s->fail_after = min_t(unsigned long, value, CMDP_MAX_ALIASES);
	} else if (!strcmp(command, "test_delay_ms")) {
		WRITE_ONCE(s->revoke_delay_ms, min_t(unsigned long, value, 1000));
	} else if (!strcmp(command, "test_stale")) {
		e = cmdp_find_range(s, value, value + PAGE_SIZE);
		if (!e) {
			ret = -ENOENT;
		} else {
			ret = cmdp_hw_complete(e->pfn, e->generation - 1,
					       CMDP_REVOKE, 0) ? -EIO : 0;
			cmdp_put(e);
		}
#endif
	} else {
		ret = -EINVAL;
	}
out:
	mutex_unlock(&s->commands);
	return ret ? ret : count;
}

static int cmdp_control_open(struct inode *inode, struct file *file)
{
	struct cmdp_session *s;
	unsigned long flags;
	int ret;

	if (!capable(CAP_SYS_ADMIN) || !current->mm)
		return -EPERM;
	s = kzalloc_obj(*s);
	if (!s)
		return -ENOMEM;
	s->mm = current->mm;
	mmgrab(s->mm); /* mm_count, never an indefinite mm_users reference */
	refcount_set(&s->refs, 1);
	mutex_init(&s->commands);
	INIT_LIST_HEAD(&s->entries);
	s->notifier.ops = &cmdp_notifier_ops;
	xa_lock_irqsave(&cmdp_entries, flags);
	ret = cmdp_owner ? -EBUSY : 0;
	if (!ret)
		cmdp_owner = s;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	if (ret)
		goto put;
	ret = mmu_notifier_register(&s->notifier, s->mm);
	if (ret) {
		xa_lock_irqsave(&cmdp_entries, flags);
		cmdp_owner = NULL;
		xa_unlock_irqrestore(&cmdp_entries, flags);
		goto put;
	}
	file->private_data = s;
	return 0;
put:
	cmdp_session_put(s);
	return ret;
}

static int cmdp_control_release(struct inode *inode, struct file *file)
{
	struct cmdp_session *s = file->private_data;
	unsigned long flags;

	cmdp_release_mm(&s->notifier, s->mm);
	/* Outside callbacks/locks: waits for in-flight notifier invocations. */
	mmu_notifier_unregister(&s->notifier, s->mm);
#ifdef CONFIG_CMDP_TEST
	cmdp_test_drop(s);
#endif
	xa_lock_irqsave(&cmdp_entries, flags);
	cmdp_owner = NULL;
	xa_unlock_irqrestore(&cmdp_entries, flags);
	cmdp_session_put(s);
	return 0;
}

static const struct file_operations cmdp_control_fops = {
	.owner = THIS_MODULE,
	.open = cmdp_control_open,
	.write = cmdp_control_write,
	.release = cmdp_control_release,
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

	debugfs_create_file("control", 0600, dir, NULL, &cmdp_control_fops);
	debugfs_create_file("stats", 0400, dir, NULL, &cmdp_stats_fops);
	return 0;
}
late_initcall(cmdp_init);
