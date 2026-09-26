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
