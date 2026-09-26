{ ... }:
{
  imports = [ ../modules/xenstore-config.nix ];
  # The consumer supplies its bootloader, dom0 budget and hardware module.
  boot.initrd.systemd.enable = true;
  virtualisation.xen.enable = true;
}
