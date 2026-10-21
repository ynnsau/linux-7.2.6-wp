# bmp build and validation scripts

These scripts record the specific `/home/yans3/cmdp-v2` experiment environment;
they are not a general Linux installer. Run ordinary builds as `yans3`; VM and
installation operations require root. No script reboots bmp.

1. Commit the source tree. `build-host.sh` requires a clean tree and records the
   exact source commit, configuration and build hashes.
2. `prepare-debug-build.sh` prepares a separate debug configuration;
   incremental debug builds use the same committed source. Build the userspace
   initramfs with `build-vm.sh`.
3. Run `run-vm.sh PATH_TO_DEBUG_BZIMAGE debug-final`, then
   `run-vm.sh PATH_TO_PRODUCTION_BZIMAGE production-final`. Both use KVM with
   four virtual CPUs, 4 GiB RAM, no network and an isolated initramfs.
4. Run `collect-vm-reports.py LOG --out periodic/reports/LABEL` for each label.
   The collector requires two complete passing TAP suites, all workload
   reports, BFS verifier PASS, balanced counters and zero final ownership.
5. `record-validation.py` binds successful reports to the exact committed
   production image, config, System.map, initramfs and logs.
6. `install-host.sh` checks that binding, installs modules and kernel, compares
   installed artifacts with the tested build, preserves the previous kernel
   and initrd, and verifies that GRUB still selects the old kernel. Installation
   refuses an unexpected existing artifact for the new release.

Logs and machine-readable records live under `periodic/logs`,
`periodic/records`, and `periodic/reports`. Failed runs are retained. Activation
and host Twitter-scale measurements are a separate step after installation.
