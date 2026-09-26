{ modulesPath, ... }:
{
  imports = [
    ./guest.nix
    (modulesPath + "/virtualisation/xen-domU.nix")
  ];
}
