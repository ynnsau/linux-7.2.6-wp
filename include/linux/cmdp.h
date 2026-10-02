/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_CMDP_H
#define _LINUX_CMDP_H

#include <linux/types.h>

struct page;
struct mm_struct;
struct cmdp_entry;

#ifdef CONFIG_CMDP
/* Caller stabilizes page; the returned entry owns a reference. May return ERR_PTR. */
struct cmdp_entry *cmdp_lookup(struct page *page);
void cmdp_put(struct cmdp_entry *entry);
/* May sleep. Caller must not hold PTL, rmap, folio or registry locks. */
int cmdp_prepare_write(struct cmdp_entry *entry);
/* Caller holds mmap write lock; rejects unsupported mm-wide operations. */
bool cmdp_mm_active(struct mm_struct *mm);
#else
static inline struct cmdp_entry *cmdp_lookup(struct page *page) { return NULL; }
static inline void cmdp_put(struct cmdp_entry *entry) { }
static inline int cmdp_prepare_write(struct cmdp_entry *entry) { return 0; }
static inline bool cmdp_mm_active(struct mm_struct *mm) { return false; }
#endif

#endif
