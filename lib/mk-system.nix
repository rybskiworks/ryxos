{ baseModule }:
{
  nixpkgs,
  pkgs,
  stateVersion,
  modules ? [ ],
  specialArgs ? { },
}:
assert pkgs.lib.assertMsg pkgs.stdenv.hostPlatform.isLinux "ryxos requires a Linux package set";
assert pkgs.lib.assertMsg (
  builtins.match "[0-9]{2}[.][0-9]{2}" stateVersion != null
) "ryxos requires an explicit NixOS state version";
nixpkgs.lib.nixosSystem {
  inherit specialArgs;
  modules = [
    baseModule
    {
      nixpkgs.pkgs = pkgs;
      system.stateVersion = stateVersion;
      nix.registry.nixpkgs.flake = nixpkgs;
    }
  ]
  ++ modules;
}
