{ lib, pkgs, ... }:
{
  imports = [ ./domu.nix ];

  networking.hostName = lib.mkForce "xen-lifecycle-probe";
  systemd.services.xen-kvm-probe.enable = lib.mkForce false;
  boot.extraModprobeConfig = lib.mkForce ''
    options kvm_amd nested=0
    options kvm_intel nested=0
  '';
  systemd.services.ryxos-lifecycle-probe = {
    description = "Persist a fixed marker in the disposable Xen lifecycle guest";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "simple";
      StateDirectory = "ryxos-lifecycle-probe";
      StateDirectoryMode = "0700";
      UMask = "0077";
      ExecStart = "${pkgs.python3}/bin/python3 ${./lifecycle-probe.py}";
      TimeoutStopSec = 10;
    };
  };
}
