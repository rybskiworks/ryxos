{ lib, pkgs, ... }:
{
  # Identity, storage, secrets and virtualization are consumer decisions.
  networking.firewall.enable = lib.mkDefault true;
  nix.settings = {
    max-jobs = lib.mkDefault 1;
    cores = lib.mkDefault 2;
  };
  environment.systemPackages = with pkgs; [
    git
    jq
    ripgrep
  ];
}
