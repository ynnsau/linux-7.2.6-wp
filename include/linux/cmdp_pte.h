/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_CMDP_PTE_H
#define _LINUX_CMDP_PTE_H

#include <linux/mmzone.h>

/* Native x86, order-0 normal user mappings only. */
static inline pte_t cmdp_preserve_write_protect(pte_t pte)
{
	struct page *page;
	unsigned long pfn;

	if (!pte_present(pte) || !pte_write(pte) || pte_special(pte) ||
	    !(pte_flags(pte) & _PAGE_USER))
		return pte;

	pfn = pte_pfn(pte);
	if (!pfn_valid(pfn))
		return pte;

	page = pfn_to_page(pfn);
	if (PageCompound(page) || PageReserved(page) ||
	    is_zone_device_page(page))
		return pte;

	if (PageCmdp(page))
		return pte_wrprotect(pte);

	return pte;
}

static inline bool cmdp_ptes_need_write_protect(pte_t pte, unsigned int nr)
{
	while (nr--) {
		if (!pte_same(pte, cmdp_preserve_write_protect(pte)))
			return true;
		pte = pte_advance_pfn(pte, 1);
	}

	return false;
}

#endif /* _LINUX_CMDP_PTE_H */
