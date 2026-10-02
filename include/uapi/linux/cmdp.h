/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_CMDP_H
#define _UAPI_LINUX_CMDP_H

#include <linux/ioctl.h>
#include <linux/types.h>

struct cmdp_page_req {
	__aligned_u64 addr;
	__aligned_u64 flags;
};

#define CMDP_IOC_MAGIC	'C'
#define CMDP_IOC_ARM	_IOW(CMDP_IOC_MAGIC, 1, struct cmdp_page_req)
#define CMDP_IOC_REVOKE	_IOW(CMDP_IOC_MAGIC, 2, struct cmdp_page_req)

#endif /* _UAPI_LINUX_CMDP_H */
