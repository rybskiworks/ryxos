# Declarative Xen domains

Import `nixosModules.xenDomains` into an explicitly configured Xen host. The module manages lifecycle only; the caller supplies reviewed `xl` configurations, images, storage, networking and resource budgets.

```nix
{ pkgs, inputs, ... }:
{
  imports = [ inputs.ryxos.nixosModules.xenDomains ];
  systemd.services.xendomains.enable = false;
  ryxos.xenDomains = {
    enable = true;
    domains.example = {
      uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123";
      configFile = ./example.cfg;
      autoStart = false;
      shutdownTimeoutSec = 60;
    };
  };
}
```

The host must separately enable `virtualisation.xen.enable` with a compatible boot configuration. `example.cfg` must declare the matching name and UUID, and explicitly set `on_poweroff`, `on_reboot` and `on_crash` to `destroy`. Each domain needs its own UUID. Configuration files enter the public-readable Nix store; keep credentials elsewhere.

Use `systemctl start ryxos-xen-example` and `systemctl stop ryxos-xen-example`. Autostart is opt-in. Declared services serialize their start/stop transactions without starting or depending on peers. The module neither creates nor erases disks. It does not save, migrate or automatically restart guests.

A NixOS generation switch does not restart or reload a retained domain service when its declaration changes. Explicit stop and host shutdown still use its lifecycle stop action. Stop an owned domain before changing or removing its identity, boot artifacts or disk declaration; a new declaration does not adopt an existing domain, and a changed UUID cannot match the previous ownership receipt.

Retain the original immutable configuration, boot and backing-image closures while a domain or its writable disk overlay exists. The module does not yet create an independent GC lease for them: receipt files are ordinary data, not Nix GC roots. Preserve the original system generation or explicitly root the required closures until the domain is stopped and its overlay is retired. A NixOS rollback changes declarations, not guest disk contents.

Inspect actual Xen inventory and ownership with:

```sh
sudo ryxos-xen-domain --spec /etc/ryxos/xen-domains/example.json status
```

Status returns JSON with the observed `present` or `absent` state, domain ID, receipt phase and an `owned` boolean. A matching live domain without a receipt, or with only a `creating` receipt, is not reported as owned. Identity conflicts fail closed. This is a read-only snapshot: it neither creates state nor adopts, stops or retires a domain, and concurrent lifecycle activity may require a retry. Presence does not distinguish paused or shutting-down domains and does not test guest health.

## Ownership and failure

Creation refuses an existing name or UUID, including an exact match. A private ownership receipt records intent before creation and the domain ID afterward. Stop verifies name, UUID and recorded ID before acting. Conflicting or incomplete inventories fail closed.

A graceful shutdown timeout leaves the guest running and retains its receipt. `destroyOnTimeout = true` explicitly permits destruction after rechecking identity. The default is false. The service retains the `xl` event monitor when stopping fails; systemd's active state represents lifecycle ownership, not guest health.

An ambiguous failed start retains a `creating` receipt. Do not repeatedly start it or delete its receipt to bypass inspection. After inspecting the toolstack, recovery can use:

```sh
sudo ryxos-xen-domain --spec /etc/ryxos/xen-domains/example.json stop
```

If no domain is visible after ambiguous creation, recovery refuses to retire the receipt automatically. Inspect the child processes and reserved identity before manually retiring it.

External root tools must not concurrently manipulate managed names or UUIDs. The helper lock coordinates its own operations; Xen's name/ID-based `xl` commands cannot atomically protect against another root process recreating a domain between inspection and action.

## Nested guests

The `xl` setting for nested virtualization is `nestedhvm = 1`, distinct from Linux's KVM module parameter. Use HVM or separately qualified PVH guests, hardware-assisted paging and a verified CPU feature set. Do not force unsupported CPUID features on.

Xen classifies nesting as experimental without security support. Booting dom0, creating a KVM API object, booting an actual child workload, and recursively booting another virtualizing guest are separate qualifications. See [Xen's support statement](https://github.com/xen-project/xen/blob/RELEASE-4.20.3/SUPPORT.md#x86nested-hvm) and [KVM nesting guidance](https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html).
