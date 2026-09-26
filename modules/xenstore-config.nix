{ config, lib, ... }:
let
  correct = import ../lib/xenstore-config.nix { inherit lib; };
in
{
  # Extend the existing text option so its normal /etc source generation sees
  # the correction. Keep upstream fields and caller-supplied source files intact.
  options.environment.etc = lib.mkOption {
    type = lib.types.attrsOf (
      lib.types.submodule (
        { name, ... }:
        {
          options.text = lib.mkOption {
            apply =
              text:
              if
                name == "xen/oxenstored.conf"
                && text != null
                && config.virtualisation.xen.enable
                && config.virtualisation.xen.store.type == "ocaml"
              then
                correct text
              else
                text;
          };
        }
      )
    );
  };
}
