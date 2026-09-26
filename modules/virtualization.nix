{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ryxos.virtualization;
in
{
  options.ryxos.virtualization = {
    enable = lib.mkEnableOption "KVM for creating child virtual machines";
    cpuVendor = lib.mkOption {
      type = lib.types.enum [
        "amd"
        "intel"
      ];
      description = "Vendor whose virtualization extensions the parent exposes.";
    };
    nested = lib.mkEnableOption "nested KVM support for virtualizing children";
    ksm = lib.mkEnableOption "memory merging within this kernel's trust domain";
  };

  config = lib.mkMerge [
    {
      assertions = [
        {
          assertion = !cfg.nested || cfg.enable;
          message = "Nested virtualization requires ryxos.virtualization.enable.";
        }
        {
          assertion = !cfg.ksm || cfg.enable;
          message = "KSM requires an explicitly virtualizing profile.";
        }
      ];
    }
    (lib.mkIf cfg.enable {
      boot.kernelModules = [ (if cfg.cpuVendor == "amd" then "kvm-amd" else "kvm-intel") ];
      boot.extraModprobeConfig = ''
        options ${if cfg.cpuVendor == "amd" then "kvm_amd" else "kvm_intel"} nested=${if cfg.nested then "1" else "0"}
      '';
      services.udev.extraRules = ''
        KERNEL=="kvm", GROUP="kvm", MODE="0660"
      '';
      systemd.services.ryxos-kvm-capability = {
        description = "Verify that this kernel exposes a usable KVM device";
        wantedBy = [ "multi-user.target" ];
        after = [ "systemd-modules-load.service" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          ${pkgs.python3}/bin/python3 ${../tests/kvm-probe.py} --require
        '';
      };
    })
    (lib.mkIf cfg.ksm { hardware.ksm.enable = true; })
  ];
}
