# Restricted Xen control over SSH

The optional `ryxos.xenControl` module exposes four lifecycle actions for an explicit subset of declared Xen domains. It creates the dedicated `ryxos-xen-control` system account with a locked password, an immutable restricted login program and enrolled **public** keys. It does not enable OpenSSH, create host keys, open firewall ports or enroll a key from a guest.

Import this module alongside the Xen lifecycle module into an already configured Xen host:

```nix
{ lib, ... }:
{
  imports = [
    ./modules/xen-domains.nix
    ./modules/xen-control.nix
  ];
  # OpenSSH and ryxos.xenDomains must already be explicitly configured.
  ryxos.xenControl = {
    enable = true;
    domains = [ "example" ];
    authorizedKeys = [ (lib.removeSuffix "\n" (builtins.readFile ./operator.pub)) ];
  };
}
```

Supply ordinary one-line public keys without key options, certificate authority markers or certificates. Remove the final newline if reading a `.pub` file into the single-line option. Public keys and the selected names enter the Nix store. Private operator keys remain outside the host configuration and must never be provisioned into a Branch or Leaf image. The existing OpenSSH listener, host-key enrollment, network admission and client host-key verification remain the operator's responsibility.

```sh
ssh -T ryxos-xen-control@host 'status example'
ssh -T ryxos-xen-control@host 'start example'
ssh -T ryxos-xen-control@host 'stop example'
ssh -T ryxos-xen-control@host 'reset example'
```

The grammar is exactly one lower-case action, one ASCII space and one enrolled domain name. Empty commands, interactive shells, subsystems, extra arguments, path syntax, quotes, control characters and `force-stop` are refused. The account's login program ignores the shell arguments supplied by SSH; only `SSH_ORIGINAL_COMMAND` reaches the parser. Python runs in isolated mode. The dispatcher supplies a fresh minimal environment, no standard input and a fixed argv to noninteractive sudo. Sudo independently permits only the exact packaged helper/action/name combinations, without environment overrides or a general root command.

The selected helper embeds the current immutable declaration inventory. Lifecycle ownership checks, retained specification selection, graceful stop and absent-only reset remain in that helper. A normal stop never escalates to destruction. Reset discards only the lifecycle manager's stopped disposable state and lease; it is not a reboot or a request to erase arbitrary disks. If a domain is removed from `xenControl.domains`, its remote authority is removed in the next configuration. Recover a removed retained domain locally through the lifecycle helper. No SSH operation may override its specification, UUID, disk, image or boot parameters.

The SSH Match block disables password and keyboard-interactive authentication, forwarding, tunnels, X11, PTYs and user rc files. It uses only the NixOS-owned authorized-keys file, disables external key providers and CA trust, and limits a connection to one session. The authorized keys also carry OpenSSH's `restrict` option. Startup environment admission must remain unset or restricted to `LANG` and `LC_*`; user environment files are refused. Additional administrator-supplied Match/Include directives must not weaken this account's effective policy. In particular, OpenSSH accumulates `AcceptEnv` entries across matching blocks, so inspect the final effective configuration after composition rather than assuming an earlier block cancels later entries.

The dispatcher logs structured account, validated peer address, admitted action/name and completion status to the authentication log. Refused raw commands, environment values, key material and helper output are not copied into that audit record. Correlate with OpenSSH's authentication log for the authenticating key. A helper failure is returned to the client. A timeout returns 124 and requests no forced stop; a disconnect or interrupted tool invocation is not proof that Xen stopped or that no operation remains pending. Inspect status and retained ownership before retrying.

## Qualification

Portable tests execute neither sudo nor Xen. They check the exact allowed argv and fresh environment, malformed command refusals, policy admission, nonzero/signal/timeout behavior and bounded audit metadata:

```sh
python3 -B -m unittest discover -s tests -p test_xen_control.py -v
```

Before deployment, evaluate module assertions and generated sudo rules, then inspect the real generated SSH configuration for this account with the selected OpenSSH package:

```sh
sshd -T -C user=ryxos-xen-control,host=localhost,addr=192.0.2.10
sudo -l -U ryxos-xen-control
```

The effective configuration must show the fixed forced command and key file, public-key-only authentication, no CA/external key provider, no forwarding/PTY/tunnel/user rc and only the permitted startup environment names. Module evaluation should cover disabled-by-default behavior, absent OpenSSH/lifecycle/sudo/PAM dependencies, empty or unknown domains, option-bearing keys, supplementary groups and dangerous environment admission. Exact sudo rules must contain only the four actions for enrolled names, with `NOPASSWD` and `NOSETENV`.

A disposable native SSH test remains required: authenticate with a synthetic key, execute the four actions against an owned declared guest, verify unknown-name/extra-argument/shell/subsystem refusals and confirm forwarding, PTY and unrelated sudo commands are denied. Exercise lifecycle failures and reset separately. No successful native SSH or Xen lifecycle qualification is implied by the portable parser tests. This is an operator capability for trusted reviewed guest declarations, not a boundary against a hostile Xen administrator or compromised hypervisor.
