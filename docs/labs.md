# Independent virtualization labs

The first acceptance target is a repeatable development lab, not a physical
workstation installation or a hostile-code containment boundary.

Qualify two paths independently:

```text
Current Linux/KVM → QEMU → Xen/NixOS dom0 → ordinary NixOS HVM guest
Current Linux/KVM → QEMU → downstream NixOS Branch → one KVM workload
```

The second path belongs in a downstream composition that supplies its own
orchestrator and runtime. RyxOS does not import that application. A combined
KVM → Xen → KVM experiment is optional and has its own result; it is not a
prerequisite for either basic lab. Its extra layer is absent from a physical
Xen host, so failure there does not establish that bare-metal Xen → KVM fails.

## Current interfaces

`nix run .#xen-lab-preflight` performs read-only admission checks: actual KVM API
access, host nesting, CPU topology, complete SMT groups, memory and disk reserves,
and the available Nix client. Use `-- --nix /absolute/path/to/nix` when the client
is not on PATH. Its proposed CPU allocation is not applied. The report explicitly
distinguishes available host controllers from verified cgroup delegation and
enforced limits. It neither loads modules nor changes services or hardware.

The ordinary checks include Xen dom0 boot and an HVM boot probe with nesting
disabled. Explicit experimental HVM/PVH drivers retain the nested KVM probe.
An empty KVM object is only API evidence; it is not evidence of a booted workload.
Test drivers require hardware acceleration and use serial control. No host
directory shares, operator certificates, network interfaces or physical disks
are supplied to the Xen fixture. The synthetic guest disk uses a disposable
QEMU snapshot over an immutable store image.

The proposed complete operator commands `xen-lab`, `xen-lab-image` and
`xen-lab-smoke` are not release interfaces yet. A driver or a passing boot probe
must not be advertised as the complete lifecycle and control workflow below.

## Release acceptance still required

The declared Xen guest must support bounded readiness, normal stop, repeated
start/stop, and reset after shutdown. A reset removes only that declaration's
owned disposable overlay and must erase a previously written test marker.
Running status comes from Xen identity checks, not a systemd oneshot state.
Backing images and their exact declarations need leases for as long as a domain
or overlay references them. Changing a host generation must not silently restart
guests, and rolling back Nix configuration does not roll back guest data.

Keep automated serial control separate from operator SSH. The latter needs a
dedicated lab key and a root-owned forced-command dispatcher accepting only
fixed status/start/stop/reset requests for declared domains. Do not evaluate shell
text or forward raw xl arguments, paths or device definitions. Disable agent,
X11 and TCP forwarding, tunnels, user rc files and PTYs; recovery access is a
separate explicit path. A Branch never receives this administration key.

For optional SSH connectivity, use one loopback host forward with restricted
QEMU user networking, then test both permitted control and denied egress. The
automated networkless topology remains valid. Production credentials, live fleet
state and host agent sockets do not belong in either lab.

Constrain the real QEMU process using a verified host cgroup: CPU quota and a
topology-derived affinity set, memory plus measured overhead, and available I/O
controls. Guest vCPU/RAM settings and polling free-space guards do not replace
these controls. Bound builds separately and initially run one lab at a time.
Sparse disk capacity is not reserved host storage. Keep KSM disabled.

Record pinned package versions, acceleration, boot entry, topology, known-command
result, lifecycle/reset result, network denial, effective host limits and cleanup.
Report each gate as passed, failed or not run. Synthetic tests cannot qualify the
native lifecycle, resource enforcement or physical hardware.

## Deferred capabilities

GPU assignment, Qubes components, display relays, generalized remote execution,
production signing services, live RAM snapshots and arbitrary recursion are
separate projects. No lab command installs a physical host or chooses a real disk.

Xen classifies nested HVM as experimental without security support. On AMD,
do not save or migrate a parent VM while its nested guests run. Use shutdown
and disposable disk overlays for reset. See [Xen's pinned support statement](https://github.com/xen-project/xen/blob/RELEASE-4.20.3/SUPPORT.md#x86nested-hvm)
and [the KVM nested-guest documentation](https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html).
