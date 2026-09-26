# Declarative Xen domains

Import `nixosModules.xenDomains` into an explicitly configured Xen host. The module manages reviewed domain identities and lifecycle; callers supply the guest configuration, boot artifacts, networking and resource budgets. It can also manage one disposable disk over an immutable raw image.

```nix
{ inputs, ... }:
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

The host must separately enable `virtualisation.xen.enable` with a compatible boot configuration. The file must declare the matching name and UUID, and explicitly set `on_poweroff`, `on_reboot` and `on_crash` to `destroy`. Each domain needs its own UUID. Configuration files and declared images enter the publicly readable Nix store; do not put credentials in them. Local path-valued configurations are imported into the store, rather than interpreted from their original checkout.

The packaged command embeds an immutable inventory. It accepts only declared names for creation, and has no arbitrary configuration-path argument. `/etc/ryxos/xen-domains.json` and the individual `/etc/ryxos/xen-domains/<name>.json` files expose the current declarations for inspection; editing these views does not change the command's authority.

```sh
ryxos-xen-domain list
sudo systemctl start ryxos-xen-example
sudo ryxos-xen-domain status example
sudo systemctl stop ryxos-xen-example
```

Autostart is opt-in. Independent names have independent locks; concurrent operations on the same name fail promptly. The helper does not save, migrate or automatically restart guests. A oneshot service's active state represents lifecycle ownership, not guest health. Use the JSON status command to inspect actual Xen presence, the recorded identity, retained specification and whether the current declaration differs.

## Generations and retained state

A NixOS generation switch does not restart, reload or stop a retained domain service when its declaration changes or disappears. Explicit stop and host shutdown still use its lifecycle stop action. Status and stop resolve the original immutable specification from durable state, even after the declaration changes or is removed. They verify the live name, UUID, domain ID and host boot identity before acting.

Before preparing any writable disk or creating a domain, the helper registers a direct Nix GC root for its immutable specification under `/nix/var/nix/gcroots/ryxos-xen-domains/<name>`. That specification references the reviewed configuration, lifecycle tools and optional backing image. It also retains the service's inventory-independent stop runner, its Python closure and empty recovery inventory, so removing an old system generation cannot remove the stop executable. Ordinary Nix references in a generated configuration retain its boot artifacts transitively. If a configuration contains literal store paths without Nix string context, declare those artifacts explicitly in `retainPaths`; merely mentioning a path in a copied text file does not establish a Nix dependency.

Private state lives under `/var/lib/ryxos-xen-domains/<name>`. Clean shutdown leaves a `stopped` receipt and its GC lease in place. Starting the same specification reuses its disk. Starting a changed specification refuses until an explicit reset retires the old state. A retained UUID also cannot be reused by another declared name. Concurrent incompatible declarations may both refuse admission and retain their leases for inspection. A rollback changes declarations, not disk contents. Do not remove GC roots manually while state is retained.

If a declaration was removed, the current command can still inspect and stop its retained identity:

```sh
sudo ryxos-xen-domain status example
sudo ryxos-xen-domain stop example
```

After an unclean host shutdown, a stale receipt cannot establish ownership of a new domain with a recycled ID. Inspect status and complete an explicit stop/reset recovery before retrying autostart. Receipts from the older schema are refused; there is no automatic adoption or migration of pre-existing guests.

## Disposable storage and reset

`disposableImage` optionally selects an immutable **raw image file** in the Nix store. The helper creates a private qcow2 overlay with an explicit raw backing format. This replaces the configuration's entire `disk` list with one writable `xvda`, using Xen's qdisk backend. The guest's kernel/root configuration must match that device layout. Multiple managed disks, remote backing files and existing qcow2 backing chains are outside this interface.

Keep `disposableImage = null` for caller-managed disks. The helper neither creates nor resets those external disks. `retainPaths` may retain immutable boot artifacts independently of the storage choice.

A normal stop preserves writable state. Reset is a separate, destructive action for the selected domain's managed disposable state:

```sh
sudo systemctl stop ryxos-xen-example
sudo ryxos-xen-domain status example
sudo ryxos-xen-domain reset example
sudo systemctl start ryxos-xen-example
```

Reset requires two complete Xen inventory checks showing both the retained name and UUID absent. It removes only the fixed receipt and managed overlay files beneath the private owned directory, and retires the GC lease last. Unexpected files, symlinks, replaced overlay identities or incomplete inventory prevent deletion. Mutable hard links are refused except for the exact two-name, two-link pair left by an interrupted overlay publication. It never follows a configuration's external disk paths. An interrupted reset preserves the lease and can be retried after inspection; a new start cannot silently attach partially retired state.

## Failure and explicit force

Creation refuses an existing name or UUID, including an exact match. The receipt records intent before creation and records the live domain ID afterward. Failed disk preparation, interrupted creation and ambiguous toolstack results retain state and the GC lease. A failed create with no confirmed domain ID never grants permission to destroy a subsequently observed matching domain.

A graceful shutdown timeout leaves domain absence unconfirmed and retains the state and GC lease. Stop never escalates to destruction. After inspecting the guest and accepting the loss of its unsaved writes, destruction is a separate command that rechecks confirmed ownership:

```sh
sudo ryxos-xen-domain force-stop example
```

This command preserves the disk and lease, just like a successful graceful stop. It cannot adopt an unowned or ambiguous domain. Stop the systemd service after an explicit force-stop if its oneshot state remains active.

An ambiguous start with no visible domain can be retired using explicit reset only after inspecting the Xen toolstack and ensuring no creation process is still pending. Do not delete its receipt or lease to bypass inspection. If an uncertain domain remains live, recover it through the host's Xen administration procedure; this helper deliberately refuses to infer ownership.

External root tools must not concurrently manipulate reserved managed names or UUIDs. Per-name locks coordinate this helper's operations; Xen's name/ID-based commands cannot atomically protect against another root process recreating a domain between inspection and action. A changing or incomplete plain/long Xen inventory never proves absence. After one authorized shutdown command, the helper retries disagreements between those views until its deadline; it does not repeat the command. Other identity errors still refuse, and admission and reset do not retry ambiguous inventory.

## Qualification

Portable tests cover immutable admission, retained declarations, GC-lease ordering, graceful and explicit forced shutdown, ambiguous failures, per-name locking, and refusal/recovery of disposable-state resets. They use synthetic tool responses and temporary files. Native lifecycle cycles, actual garbage-collection survival across a generation switch, and qcow2-backed Xen boot/reset remain separate acceptance gates; a passing basic Xen boot test does not prove them.

The Xen setting for nested virtualization is `nestedhvm = 1`, distinct from Linux's KVM module parameter. Xen classifies nesting as experimental without security support. Use HVM or separately qualified PVH guests, hardware-assisted paging and a verified CPU feature set; do not force unsupported CPUID features on. Dom0 boot, a KVM API object, an actual child workload and recursive virtualization are separate qualifications. See [Xen's support statement](https://github.com/xen-project/xen/blob/RELEASE-4.20.3/SUPPORT.md#x86nested-hvm) and [KVM nesting guidance](https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html).
