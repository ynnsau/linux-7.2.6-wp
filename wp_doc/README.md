# Write Protect for CMD-P v2

See [PLAN.md](PLAN.md) for the consolidated research plan, source map,
prototype scope, and measurement milestones. The original goals follow.

The restricted, manually armed kernel v1 is implemented; its architecture,
test interface, scope, and validation are recorded in the
[CMD-P selftest README](../tools/testing/selftests/cmdp/README.md). The plan
retains the broader research goals and the original checklist below. Measured
software-path timings are in
[BENCHMARK.md](../tools/testing/selftests/cmdp/BENCHMARK.md).

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
1. How is write-protect currently handled in the kernel for x86. **DONE**
2. Who sets pages to write-protect and when do they set it. **DONE**
3. When will a page remove the write protection. **DONE**

### Implement and testing 
1. Can we set a specific page to be write-protected from userspace via syscall?
   **DONE** with ioctl(2) on `/dev/cmdp`; ioctl is the syscall interface, so no
   global Linux syscall number was allocated.
2. Can we set a specific page to be write-protected from kernel space via kernel module?
   **DONE** through the in-tree MM implementation. A module alone could not
   provide the required `do_wp_page()` integration.
3. Can we set and handle such write-protect as a "CMD-P" specific write-protect
   and avoid forced copy-on-write while using dummy publish/revoke operations?
   **DONE** for the restricted prototype. Normal Linux COW/reuse decisions run
   after CMD-P revoke; real MMIO remains future work.
4. Can we measure the time to set write-protect and handle its first write?
   **DONE** for the software path using fenced RDTSCP timing and kernel counter
   deltas. See the benchmark report; physical hardware timing remains future work.
