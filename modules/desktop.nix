{
  config,
  lib,
  pkgs,
  ...
}:
{
  options.ryxos.desktop.enable = lib.mkEnableOption "the niri Wayland desktop";
  config = lib.mkIf config.ryxos.desktop.enable {
    programs.niri.enable = true;
    programs.dconf.enable = true;
    networking.networkmanager.enable = true;
    security.polkit.enable = true;
    security.rtkit.enable = true;
    security.pam.services.swaylock = { };
    services.pipewire = {
      enable = true;
      alsa.enable = true;
      pulse.enable = true;
    };
    services.greetd = {
      enable = true;
      settings.default_session = {
        command = "${pkgs.tuigreet}/bin/tuigreet --time --remember --cmd niri-session";
        user = "greeter";
      };
    };
    fonts.packages = with pkgs; [
      nerd-fonts.jetbrains-mono
      noto-fonts
      noto-fonts-color-emoji
    ];
    environment.systemPackages = with pkgs; [
      foot
      fuzzel
      xwayland-satellite
    ];
  };
}
