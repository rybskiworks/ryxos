# OCaml Xenstore configuration

The `xenHost` profile corrects two defects in the pinned NixOS OCaml Xenstore
configuration generator: five boolean settings use Nix's `1`/empty-string
conversion, and the access-log character-limit key is misspelled. Xen expects
textual `true`/`false` and `access-log-nb-chars`.

The compatibility module transforms only those lines in the generated
`environment.etc."xen/oxenstored.conf".text`, before NixOS creates its normal
source file. All other upstream settings and caller text are preserved. It
does not replace the upstream Xen module, change package pins, or select a
different daemon. C Xenstore, disabled Xen, other `/etc` files and explicit
source-file overrides are unaffected. Already corrected input is unchanged,
so consumers using a repaired Nixpkgs renderer keep its output.

Configure policy through the existing `virtualisation.xen.store.settings`
options. The correction does not choose new permission or quota policy.
The affected source defaults are enabled; Xen previously kept those defaults
when its parser rejected the generated values. Explicit false values and custom
access-log bounds now reach the parser correctly.

`nix build .#checks.x86_64-linux.xenstore-config` evaluates default, all-true,
all-false and mixed settings, verifies preservation and scope, and produces the
evaluated fixture report. This is a rendering check.

The native dom0 fixtures invoke the selected
`oxenstored --config-test --config-file FILE` for the installed configuration
and the generated matrix, including rejection of each former malformed value
and key. The binary initializes Xen's privileged control interface before it
handles this option, so this acceptance requires a real dom0. Config-test exits
before daemon startup and does not reconfigure the running service.

The ordinary Xen fixtures import the same corrected host profile. Their native
gate checks the effective configuration and Xenstore journal after real startup,
then retains the existing guest/lifecycle/shutdown assertions. Building a new
dom0 image and running those tests remain necessary to qualify daemon startup;
the config-only check is not a guest boot result.

The optional unset `XEN_DOM0_UUID` notice is unrelated and is left unchanged.

Sources: [pinned NixOS generator](https://github.com/NixOS/nixpkgs/blob/a799d3e3886da994fa307f817a6bc705ae538eeb/nixos/modules/virtualisation/xen-dom0.nix#L823-L855),
[Xen configuration parser](https://github.com/xen-project/xen/blob/RELEASE-4.20.3/tools/ocaml/xenstored/config.ml),
and [config-only startup branch](https://github.com/xen-project/xen/blob/RELEASE-4.20.3/tools/ocaml/xenstored/xenstored.ml#L359-L367).
