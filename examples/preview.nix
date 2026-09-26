{ lib, modulesPath, ... }:
{
  imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ];
  networking.hostName = "ryxos-preview";
  users.users.preview = {
    isNormalUser = true;
    extraGroups = [ "wheel" ];
    initialPassword = "preview";
  };
  virtualisation = {
    cores = 2;
    memorySize = 3072;
    diskSize = 16384;
    graphics = lib.mkDefault false;
  };
}
