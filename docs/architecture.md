# Composition boundaries

`nix-tooling` supplies mechanisms. `ryxos` composes reusable NixOS capabilities. A consumer supplies application packages, machine identity, hardware, storage, users and deployment topology.

Neither base needs to import a consumer. An orchestration application and `ryxos` can both be inputs of a third configuration without introducing a cycle.

```text
nix-tooling ──► ryxos ──────────┐
                              ├──► private system composition
application package ──────────┘
```

The consumer owns Nixpkgs and passes its evaluator and package set to `lib.mkSystem`. Supplier pins are defaults for the supplier's examples and checks. Use `follows` deliberately from a supplier's input to the selected consumer input, rather than making every project inherit a tooling repository's compiler.

Instantiate the same built guest artifact multiple times at runtime. Do not embed an image's own final output in that image: that would create a build dependency cycle. Recipes and immutable external artifact references are suitable runtime inputs.

## Capabilities and backends

`virtualizingGuest` enables KVM, requests nested support, and verifies the KVM API by creating an empty VM. It requires an explicit CPU vendor and does not install an orchestrator. KSM is optional and disabled by default; sharing memory across workloads is a trust-domain decision.

`nonvirtualizingGuest` forbids the KVM/Xen role, blacklists KVM modules at the kernel level, and checks that KVM cannot create a VM. It does not install an orchestration application. A consuming image must also exclude that application's closure, mounted binaries and control sockets.

A module cannot remove capabilities from an externally supplied kernel. With such a backend, the parent must suppress nested CPU features and KVM access. A configuration flag or missing command in PATH is not proof of isolation. Qualify the actual launch backend with CPU-feature and KVM API probes.

Xen root and KVM child execution are separate backends. A Xen guest that creates KVM children needs Xen nested virtualization, a suitable guest type, and the relevant CPU extensions. Xen classifies nested virtualization as experimental without security support. A successful test does not change that support status.

Testing Xen inside a KVM VM adds a layer absent from a physical Xen host. Record which topology was tested; a failure in the additional layer does not by itself establish that the physical topology is impossible.

## Hardware and installation

Examples use disposable virtual disks. No example selects a physical disk, formats partitions, installs a bootloader on the host, enrolls secrets or changes a running fleet. A physical-machine consumer supplies those choices separately after hardware discovery.

Keep personal desktops, GPU allocation, encrypted secret documents and network topology in the consumer repository. Public examples contain only disposable identities.
