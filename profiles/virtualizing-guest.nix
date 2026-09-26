{ ... }:
{
  imports = [
    ./guest.nix
    ../modules/virtualization.nix
  ];
  ryxos.virtualization = {
    enable = true;
    nested = true;
  };
}
