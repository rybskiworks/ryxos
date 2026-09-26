{ config, pkgs, ... }:
{
  imports = [
    ./guest.nix
    ../modules/virtualization.nix
  ];
  assertions = [
    {
      assertion = !config.ryxos.virtualization.enable;
      message = "The nonvirtualizing guest profile cannot enable KVM.";
    }
    {
      assertion = !(config.virtualisation.xen.enable or false);
      message = "The nonvirtualizing guest profile cannot enable Xen.";
    }
  ];
  boot.blacklistedKernelModules = [
    "kvm"
    "kvm_amd"
    "kvm_intel"
  ];
  # modprobe's blacklist only suppresses aliases. The kernel-side blacklist
  # also rejects an explicit load, when this profile owns the booted kernel.
  boot.kernelParams = [ "module_blacklist=kvm,kvm_amd,kvm_intel" ];
  # This also catches a runtime that supplies its own preloaded guest kernel.
  # Denial of CPU extensions and device passthrough belongs to the parent.
  systemd.services.ryxos-no-kvm = {
    description = "Reject usable KVM in a nonvirtualizing guest";
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.python3}/bin/python3 ${../tests/kvm-probe.py} --forbid
    '';
  };
}
