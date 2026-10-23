# Versioned batching kernel installation without GRUB changes

Use the recorded successful production build and all eight debug plus eight
production VM suites. `verify-fused-build.py` binds image/config/source hashes,
complete TAP validation, raw serial logs, VM input hashes and unchanged host
identity. `common.py` rechecks those facts during both preflight and final audit.
The installer requires a clean repository and the exact build-record SHA-256.

`install-no-grub.sh HASH` installs modules with `modules_install`, copies the
versioned kernel/config/System.map, runs `mkinitramfs` directly, and registers
that initramfs for future maintenance. It never runs `make install`,
`update-initramfs`, bootloader hooks, GRUB commands, or reboot.

All new paths must be absent at preflight. The verifier checks GRUB contents
and metadata, old boot files and symlinks, running kernel/boot ID, module paths,
NVMe initramfs modules, and sampled signed module payloads against the tested
build. New evidence is in `native-install-fused-scanner-20261022`; old recovery
builds and installation evidence are preserved. The user controls activation.
