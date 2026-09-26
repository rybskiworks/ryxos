# ryxos

Reusable NixOS modules for Xen guest lifecycle, Lix, a niri desktop, and virtual machines with explicit virtualization capabilities.

`ryxos` consumes generic mechanisms from `nix-tooling`. It does not depend on an orchestration application, personal configuration, fleet, or physical machine. Consumers select their own Nixpkgs and application packages.

This repository is under development. Its examples use disposable virtual machines; they do not provision physical storage.

## Use from another configuration

Select an immutable revision of this flake, then pass your own package set and NixOS evaluator:

```nix
inputs.ryxos.lib.mkSystem {
  inherit nixpkgs pkgs;
  stateVersion = "26.05";
  modules = [
    inputs.ryxos.nixosModules.virtualizingGuest
    { ryxos.virtualization.cpuVendor = "amd"; }
    ./guest-hardware.nix
    ./applications.nix
  ];
}
```

Use `nonvirtualizingGuest` for a guest that must not create children. Neither profile installs an orchestrator. Applications and their exact package derivations belong in `applications.nix` or an equivalent composition above this repository.

The optional `desktop` module enables niri through `ryxos.desktop.enable`. Users, Home Manager, themes, GPUs and secrets remain caller choices. `xenHost` enables the NixOS Xen integration; the consumer supplies a compatible bootloader, hardware and explicit dom0 resource budget. `xenGuest` adds the upstream domU driver profile.

The Xen host profile also preserves the upstream settings while correcting the
pinned [OCaml Xenstore configuration rendering](docs/xenstore-settings.md).

Read [composition boundaries](docs/architecture.md) before selecting a recursive topology. Xen nesting is experimental; each actual hypervisor chain requires its own runtime qualification.

The [independent lab plan](docs/labs.md) separates ordinary Xen guest acceptance
from downstream KVM workload acceptance and the optional combined experiment.

The [Xen domain module](docs/xen-domains.md) provides explicit domain ownership and bounded systemd lifecycle without imposing image or network topology.

The optional [SSH control module](docs/xen-control.md) restricts a dedicated
operator account to declared lifecycle actions. Key enrollment, listeners and
network policy remain explicit caller choices.

For the fixed ordinary-HVM smoke test, create a private evidence directory and
use the [supervised runner](docs/lab-supervisor.md):

```sh
mkdir -m 700 lab-evidence
nix run .#xen-hvm-smoke -- "$PWD/lab-evidence"
```

It requires usable KVM, a systemd user manager and effective CPU/memory controls.
It retains receipts and the driver closure, and cleans up its disposable VM
state. This is an automated test, not an interactive Xen management session.

`nix run .#xen-lifecycle-smoke -- "$PWD/lab-evidence"` uses the same resource
supervisor for the managed disk, generation-change, reset and restricted SSH
fixture. Its SSH exchange stays inside dom0 over loopback with public test keys;
it does not expose a host listener or enroll a real operator key.

`nix run .#xen-control-host-smoke -- "$PWD/lab-evidence"` selects the separate
fixed host SSH fixture. It admits only the restricted loopback forward described
in the [supervisor documentation](docs/lab-supervisor.md), uses public synthetic
keys and requires the port to be free. Its source checks are implemented; native
host-route acceptance remains pending. It is not a production SSH enrollment or
an interactive management session.

## Validation

`just evaluate` checks all configurations without building. `just check` builds the declared checks, including real Xen boot with an ordinary HVM guest. Nested HVM/PVH probes are separate experimental package outputs. Xen checks require a builder with usable KVM and appropriate host CPU capabilities; they refuse a software-emulation fallback.

`nix run .#preview` starts a disposable console system. `nix run .#desktop-preview` starts the niri example with QEMU VirGL. The disposable login is `preview` / `preview`.
