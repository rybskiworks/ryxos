# Xen test machine compatibility

The HVM tests create new disposable guests with the explicit QEMU machine ABI
`xenfv-4.2`. Xen 4.20.3 otherwise requests `xenfv`, an alias removed in QEMU 11
with the older 3.1 machine. The immutable xl configuration supplies
`device_model_args_hvm` and a fixed UUID; `xl create` receives only that file.
The receipt records the declared machine ABI. PVH uses its separate device-model
path.

Persistent HVM declarations must explicitly select and retain a compatible
machine ABI. This test does not establish that existing guests, saved state, or
disks created with a different machine can migrate automatically. A package or
machine change requires separate compatibility qualification.

The immutable configuration derives `extra` from the guest's generated
`boot.kernelParams`, alongside its installed system-profile init path and probe
mode. It requires no command-line correction at launch. In particular, the
systemd initrd uses `root=fstab`; substituting
a root device creates a competing mount unit instead of using NixOS's root
filesystem declaration.

The managed lifecycle fixture additionally imports the public lifecycle and SSH
control modules. It boots an actual NixOS guest over a managed qcow2 overlay,
checks marker persistence across shutdown/start, changes the host declaration
while the guest remains live, and verifies marker removal after explicit reset.
The guest makes a bounded TCP attempt to a documentation-only address and
requires the kernel to report an unreachable network.

The SSH client and server run inside dom0 over loopback, using the pinned
Nixpkgs public test keys. The outer VM has no network interface or host shares.
Authentication, exact lifecycle actions and rejected shell/forwarding requests
are tested separately from any future operator-to-host connection. This fixture
checks registered backing leases across a generation switch; it does not claim
sole-root garbage-collection survival or physical hardware qualification.

The managed fixture embeds its guest backing in dom0. Its test-only package
override releases the populated staging directory after `cptofs` succeeds,
before bootloader installation and raw-to-qcow2 conversion. This avoids retaining
three large copies simultaneously. Source and build-phase fingerprints restrict
the override to the reviewed builder; changes to its pin or image declaration
require another staging-lifetime review. The guest and dom0 system closures are
unaffected. This does not reserve storage or replace the host resource monitor.
