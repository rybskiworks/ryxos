{
  config,
  modulesPath,
  pkgs,
  ...
}:
let
  probe = pkgs.writeText "xen-kvm-probe.py" (builtins.readFile ./probe.py);
in
{
  imports = [ (modulesPath + "/virtualisation/xen-domU.nix") ];

  boot = {
    loader.grub.enable = false;
    initrd.systemd.enable = true;
    kernelParams = [ "console=hvc0" ];
    extraModprobeConfig = ''
      options kvm_amd nested=1
      options kvm_intel nested=1
    '';
  };
  fileSystems."/" = {
    device = "/dev/xvda";
    fsType = "ext4";
  };
  networking = {
    hostName = "xen-kvm-probe";
    useDHCP = false;
  };
  services.openssh.enable = false;
  systemd.services.xen-kvm-probe = {
    description = "Record nested KVM capability in a disposable Xen guest";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    path = [
      pkgs.kmod
      pkgs.systemd
    ];
    serviceConfig = {
      Type = "oneshot";
      StateDirectory = "xen-kvm-probe";
      StateDirectoryMode = "0700";
      TimeoutStartSec = 60;
    };
    script = ''
      # Record failure as well as success before the synthetic guest powers off.
      ${pkgs.python3}/bin/python3 ${probe} /var/lib/xen-kvm-probe/result.json
    '';
    postStop = "${pkgs.systemd}/bin/systemctl --no-block poweroff";
  };
  system.stateVersion = "26.05";
}
