# Supervised disposable driver runs

`scripts/lab-supervisor.py` runs a reviewed, already built NixOS QEMU test driver.
It neither builds images nor provisions physical disks. The initial interface is
for noninteractive test drivers, not a general command runner or a desktop
launcher.

Package the complete `scripts` directory so `lab-preflight.py` remains beside the
supervisor. Provide pinned Python, systemd, util-linux, coreutils and a Lix/Nix
client on the wrapper's PATH. The `--systemd-run`, `--systemctl`, `--lscpu`,
`--env` and `--nix-store` options permit explicit executable paths. The runner must execute as the normal
operator, with an existing systemd user manager and cgroup v2 delegation.

For a driver with one 4 GiB, two-vCPU VM:

```sh
mkdir -m 700 lab-evidence
ryxos-lab-run \
  --driver /nix/store/<driver-output>/bin/nixos-test-driver \
  --output-dir "$PWD/lab-evidence" \
  --memory-mib 4096 --qemu-overhead-mib 2048 --vcpus 2
```

Declare the sum of the outer driver VMs' memory and vCPUs. The runner reads the
pinned launcher scripts, refuses insufficient budgets, requires KVM without a
software fallback and requires `-no-user-config`. Its current driver contract
also refuses containers, test VLANs and SSH backdoors. It checks the actual QEMU
arguments for exactly `-nic none` under its default `closed` profile and refuses explicit network backends, directory
shares and network/filesystem/host-passthrough devices. These checks recognize
the selected NixOS launcher format; they do not interpret arbitrary shell
programs. Driver source and its guest policies still require review.

An explicit `--network-profile loopback-ssh` admits only one reviewed `dom0`
launcher with a fixed restricted SLIRP backend. Its sole mapping is TCP
`127.0.0.1:22222` on the host to `10.0.2.15:22222` in dom0, with IPv6 disabled
and MAC `52:54:00:72:78:01`. No port, address, path, device or extra-argument
options accompany this profile. A busy port must be refused by the fixture;
the runner never stops its existing owner to make the forward available.

This profile recognizes the pinned q35 HVM launcher's complete device/drive
argument shape. It refuses another NIC/backend/forward, wildcard binding,
unrestricted networking, network environment expansion, QEMU configuration
overrides, host shares, arbitrary firmware/disk paths and extra arguments.
The existing private disk/EFI variables and cleared environment remain in use.
The native test must independently verify the listener's exact QEMU ownership,
authentication, command restrictions, egress denial and listener cleanup.
Argument admission does not itself prove those runtime properties.

The selected policy and fixed mapping are written into the private request and
receipts. The inner verifier re-inspects a loopback driver's immutable admission
before starting it. Public synthetic fixture keys and a loopback bind are for a
disposable cooperative lab; they do not protect it from other local users with
the same public test key. This remains a finite driver interface, without an
interactive session or a general network exception.

Admission checks usable KVM, the already enabled host nested parameter, complete
online SMT groups and free disk/RAM. It reserves two physical cores by default,
selects enough remaining complete groups for the requested vCPUs, and requires
guest memory plus QEMU overhead plus 4 GiB available host RAM. The default disk
reserve is 21 GiB on scratch, evidence and store filesystems. No host module or
resource setting is changed to make admission pass.

The driver enters a uniquely named transient user service. Before launching it,
the service verifies the actual cgroup CPU quota, CPU affinity, memory cap and
zero swap limit. Missing or ineffective mandatory controllers block execution.
CPU-set and I/O weight observations are reported separately: a requested
property is not evidence that the controller is delegated. I/O weight is a
relative scheduling preference, not a bandwidth cap. Affinity is inherited by
the reviewed processes and is not a hostile-code isolation boundary.

The child receives an explicit environment with fresh private HOME, XDG and
temporary directories. It receives no inherited QEMU/kernel/disk/firmware
overrides, SSH socket, tokens, Python search paths or operator configuration.
The systemd user manager is contacted only by the supervisor; its environment
is cleared before the inner verifier and the driver run.

One per-user lock serializes runs. A durable active lease remains if the
supervisor is killed or cleanup cannot be proven. Every admitted run gets a
private evidence directory containing its request, driver log, admission data,
effective controls, resource minima and cleanup result. Before any VM starts,
the existing driver closure is retained by an indirect Nix GC root at
`roots/driver` inside that evidence directory, with builds and substitutions
disabled. The root remains with the evidence after success, refusal or recovery;
it does not depend on an invoking `nix run` process remaining alive. A missing
final inside receipt reports execution as unknown, never as a successful
nonexecution.

The supervisor checks disk/RAM reserves once per second, limits log growth and
uses a finite runtime deadline. SIGINT, SIGTERM, reserve failure and deadline
failure stop only the recorded transient service. Cleanup checks its description,
transient status and cgroup identity, requires the cgroup empty, and removes only
the fresh scratch directory whose device/inode and ownership marker still match.
Evidence remains. Replaced state or uncertain unit ownership is preserved.

After inspecting a retained run, explicitly request recovery:

```sh
ryxos-lab-run --recover "$PWD/lab-evidence/ryxos-lab-<run>"
```

Recovery requires the matching private request and active lease. It cannot adopt
another service or clear an unrelated lease. It applies the same exact-unit and
scratch-identity checks as normal cleanup.

Recovery uses the policy recorded in the request, accepts no network-profile
override, and validates its fixed mapping before cleanup. Existing leases from
the original closed-network runner remain recoverable. Recovery does not start
the driver or require its files to be reopened merely to stop an owned service.

Submission intent is recorded before the launcher process starts. If cancellation
occurs before the unit becomes visible, an initial `not-found` response is not
proof of cleanup: the pending launcher could still create it. The lease and
scratch remain until the recorded launch is settled or the inside verifier has
proved that the unit was created. A run interrupted before either proof requires
inspection; recovery refuses to guess that an unobserved submission cannot run.

Retire an evidence directory and its GC root only after its recorded service and
scratch state are gone and any desired results have been preserved. Recovery
does not remove the root or run garbage collection.

This is cooperative supervision of reviewed disposable labs under one Unix
account. It is not a sandbox for a hostile host-side driver or protection from
another process acting as the same user. Sampled free-space checks can overshoot
between observations; they are not filesystem quotas. SIGKILL cannot run cleanup
or the outer reserve monitor: the service's kernel resource limits and finite
`RuntimeMaxSec` remain, and the lease prevents another supervised run until
explicit recovery. Daemon-side Nix builds need separate limits. Portable tests
do not establish native resource-pressure, cancellation or hypervisor acceptance.
