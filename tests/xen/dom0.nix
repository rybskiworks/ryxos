{ lib, pkgs, ... }:
{
  virtualisation = {
    memorySize = 6144;
    cores = 4;
    useBootLoader = true;
    useEFIBoot = true;
    sharedDirectories = lib.mkForce { };
    useHostCerts = false;
    mountHostNixStore = false;
    graphics = false;
    vlans = [ ];
    qemu = {
      options = [ "-cpu host" ];
      networkingOptions = lib.mkForce [ "-nic none" ];
    };
    xen = {
      enable = true;
      dom0Resources = {
        memory = 2048;
        maxMemory = 2048;
        maxVCPUs = 2;
      };
      boot.params = [
        "console=com1"
        "com1=115200,8n1"
        "loglvl=all"
        "guest_loglvl=all"
      ];
    };
  };
  boot = {
    initrd.systemd.enable = true;
    loader.systemd-boot.enable = true;
    loader.efi.canTouchEfiVariables = true;
    kernelParams = [ "console=hvc0" ];
  };
  networking.useDHCP = false;
  systemd.services.xendomains.enable = lib.mkForce false;
  # Xen's console occupies hvc0; the test driver's virtio console is hvc1.
  # Dom0 cannot use the UART Xen owns, including the stock ttyS0 dependency.
  systemd.services.backdoor = {
    requires = lib.mkForce [ "dev-hvc1.device" ];
    after = lib.mkForce [ "dev-hvc1.device" ];
    script = lib.mkForce ''
      export USER=root HOME=/root PAGER=
      set +u
      source /etc/profile
      cd /tmp
      exec < /dev/hvc1 > /dev/hvc1 2> /dev/hvc0
      stty -F /dev/hvc1 raw -echo
      echo "Spawning backdoor root shell..."
      PS1="" exec ${pkgs.bashNonInteractive}/bin/bash --norc /dev/hvc1
    '';
  };
  systemd.services."serial-getty@hvc1".enable = false;
  services.journald.extraConfig = lib.mkForce ''
    ForwardToConsole=yes
    TTYPath=/dev/hvc0
    MaxLevelConsole=debug
  '';
  environment.systemPackages = [
    pkgs.e2fsprogs
    pkgs.python3
  ];
  system.stateVersion = "26.05";
}
