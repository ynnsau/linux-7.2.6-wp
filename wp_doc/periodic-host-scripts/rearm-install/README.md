# Recovery kernel installation without GRUB changes

These scripts prepare and verify the versioned `7.2.6-cmdp-rearm-production`
installation on BMP. Running them is a separate, authorized operation after
review of the completed build and all six production VM suites. This directory
does not schedule, invoke, or perform a reboot.

The source, build and installation record paths are explicit in `common.py` and
the shell drivers. Commit, image hash and module count come from the final
`records/build-verification.json`; the operator must supply that reviewed
file's SHA-256. The preflight independently checks the file hash, clean source
HEAD, source hashes, artifact bytes/hashes, module paths and six actual VM
reports, serial logs, command lines and input hashes. It rejects a missing,
changed, failed or stale report rather than trusting its `pass` field alone.

Before invoking the driver, create the installation record directory as the
ordinary user and review the final build record:

```sh
mkdir -p /home/yans3/cmdp-v2/periodic/native-install-rearm-recovery-20261021
sha256sum /home/yans3/cmdp-v2/periodic/rearm-recovery-20261021/records/build-verification.json
```

After review, pass the resulting **explicit digest** to the driver under root.
Do not use a command substitution that silently approves whatever record is
present at execution time.

```sh
bash /home/yans3/cmdp-v2/linux-rearm-recovery/wp_doc/periodic-host-scripts/rearm-install/install-driver.sh REVIEWED_SHA256
```

Preflight requires zero CMD-P host ownership and absent destination image,
module and initramfs registration paths. The driver refuses repeat attempts
with an existing `install.started` receipt; the installer also serializes with
`flock` and refuses an existing before-snapshot. Any failed/partial attempt
requires inspection, not automatic deletion or overwrite.

Installation uses `modules_install INSTALL_MOD_STRIP=1`, versioned file copies,
direct `mkinitramfs`, and a versioned initramfs registration file. It does not
use `make install`, `update-initramfs`, kernel post-install hooks, or GRUB
commands. It leaves existing boot symlinks alone.

Verification requires unchanged GRUB bytes and recorded metadata, prior boot
files/links, running kernel, boot ID, and CMD-P counters. Kernel log growth may
contain only ordinary perf sampling-rate adjustment notices. It checks exact
installed module paths/counts; five storage/network module signatures against
the certificate embedded in the installed kernel; signed payload bytes against
the corresponding stripped build modules; and extracted NVMe initramfs module
bytes against those installed modules. The initramfs SHA-1 registration is
checked independently. Private signing-key bytes are neither copied nor logged.

Evidence goes under `native-install-rearm-recovery-20261021`. A successful run
ends with `installation-verification.json` containing `pass: true`, an exit
status of zero, and `install-complete`. Keep the extracted initramfs until
verification is reviewed. GRUB selection and reboot remain manual user actions.
