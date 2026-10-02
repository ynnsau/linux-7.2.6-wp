# Write Protect for CMD-P v2

See [PLAN.md](PLAN.md) for the consolidated research plan, source map,
prototype scope, and measurement milestones. The original goals follow.

Confirmed requirement: CMD-P needs the underlying physical page to remain
read-only against **all writers** while advertised. Protecting one process's
virtual mapping is insufficient; see the physical-page contract in the plan.

## Overview
This write protect study aims to explore how we can address the read-only pitfall of the CMD-P prefetcher on the Linux kernel side. 

## Problem
The CMD-P prefetcher can only prefetch to read-only memory addresses. In CMD-P v1, this is done by having the programmer to manually identify read-only regions and only do CMD-P hint to prefetch those regions. 

In CMD-P v2, we would like to have the Linux kernel 1) identify the pages that have certain duration of read-only operations, 2) set them to write protect, 3) inform the CMD-P hardware that those addresses are safe to write protect. 

## Steps
### Understanding write-protect
1. How is write-protect currently handled in the kernel for x86. 
2. Who sets pages to write-protect and when do they set it.
3. When will a page remove the write protection. 

### Implement and testing 
1. Can we set a specific page to be write-protected from userspace via syscall?
2. Can we set a specific page to be write-protected from kernel space via kernel module? 
3. Can we set and handle such write-protect as a "CMD-P" specific write-protect and does not perform copy-on-write, but instead perform some MMIO operation that sets and clear some hardware structure on CMD-P (do not worry about how to do the MMIO for now, we can do a `pr_info` as a dummy operation).
4. After having step 1-3, can we measure the time it takes to a) set write protect, b) handling write-protect. To understand the WP overhead, we can start simple and use the `rdtscp` or `ebpf` hook and monitor write-protect triggering function duration. Please use LLM to look into either one of them. 
