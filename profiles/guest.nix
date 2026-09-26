{ lib, ... }:
{
  documentation.enable = lib.mkDefault false;
  environment.defaultPackages = lib.mkDefault [ ];
  nix.gc.automatic = false;
  nix.optimise.automatic = false;
}
