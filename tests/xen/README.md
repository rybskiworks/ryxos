# Xen test machine compatibility

The HVM tests create new disposable guests with the explicit QEMU machine ABI
`xenfv-4.2`. Xen 4.20.3 otherwise requests `xenfv`, an alias removed in QEMU 11
with the older 3.1 machine. The test uses the supported
`device_model_args_hvm` override on `xl create` and records the selected machine
in its receipt. PVH uses its separate device-model path.

Persistent HVM declarations must explicitly select and retain a compatible
machine ABI. This test does not establish that existing guests, saved state, or
disks created with a different machine can migrate automatically. A package or
machine change requires separate compatibility qualification.
