{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ryxos.xenDomains;
  inherit (lib) mkEnableOption mkOption types;
  domainNames = builtins.attrNames cfg.domains;
  predecessors = builtins.listToAttrs (
    lib.imap0 (index: name: {
      inherit name;
      value = lib.take index domainNames;
    }) domainNames
  );
  uuidType = types.strMatching "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
  helper = pkgs.writeShellApplication {
    name = "ryxos-xen-domain";
    text = ''
      exec ${pkgs.python3}/bin/python3 ${./xen-domain.py} "$@"
    '';
  };
  specifications = lib.mapAttrs (
    name: domain:
    pkgs.writeText "xen-domain-${name}.json" (
      builtins.toJSON {
        inherit name;
        inherit (domain)
          uuid
          startTimeoutSec
          shutdownTimeoutSec
          destroyOnTimeout
          ;
        configFile = toString domain.configFile;
        xl = "${config.virtualisation.xen.package}/bin/xl";
        stateDirectory = "/var/lib/ryxos-xen-domains";
      }
    )
  ) cfg.domains;
in
{
  options.ryxos.xenDomains = {
    enable = mkEnableOption "declarative lifecycle ownership of reviewed Xen domains";
    domains = mkOption {
      default = { };
      description = ''
        Domain configurations managed through xl. Each file must declare its
        matching name and UUID, and use destroy for poweroff, reboot and crash.
        Images, disks, networks and boot artifacts are supplied by the caller.
        This module does not create or erase storage.
      '';
      type = types.attrsOf (
        types.submodule {
          options = {
            configFile = mkOption {
              type = types.either types.path types.package;
              description = ''
                Reviewed xl configuration file, for example a pkgs.writeText
                result. Its contents enter the Nix store and must not contain
                credentials. A package must resolve to a file, not a directory.
              '';
            };
            uuid = mkOption {
              type = uuidType;
              description = "Stable nonzero UUID also declared in the xl configuration.";
            };
            autoStart = mkOption {
              type = types.bool;
              default = false;
              description = "Start this domain when multi-user.target is reached.";
            };
            startTimeoutSec = mkOption {
              type = types.ints.between 1 600;
              default = 120;
              description = "Maximum time allowed for xl create.";
            };
            shutdownTimeoutSec = mkOption {
              type = types.ints.between 1 600;
              default = 60;
              description = "Maximum wait for graceful shutdown and domain removal.";
            };
            destroyOnTimeout = mkOption {
              type = types.bool;
              default = false;
              description = ''
                Permit xl destroy only after graceful shutdown times out and
                the live name, UUID and domain ID still match the ownership
                receipt. Otherwise leave the guest running and fail the stop.
              '';
            };
          };
        }
      );
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.virtualisation.xen.enable;
        message = "ryxos.xenDomains requires virtualisation.xen.enable and a qualified Xen boot configuration.";
      }
      {
        assertion = !(config.systemd.services.xendomains.enable or true);
        message = "Disable systemd.services.xendomains.enable before enabling ryxos.xenDomains; two lifecycle managers must not own the same domains.";
      }
      {
        assertion = lib.all (name: builtins.match "[a-zA-Z][a-zA-Z0-9_-]{0,62}" name != null) (
          builtins.attrNames cfg.domains
        );
        message = "Xen domain names must start with a letter and contain at most 63 letters, digits, hyphens or underscores.";
      }
      {
        assertion = lib.all (domain: domain.uuid != "00000000-0000-0000-0000-000000000000") (
          builtins.attrValues cfg.domains
        );
        message = "Managed Xen domains require nonzero UUIDs.";
      }
      {
        assertion =
          let
            uuids = map (domain: domain.uuid) (builtins.attrValues cfg.domains);
          in
          builtins.length uuids == builtins.length (lib.unique uuids);
        message = "Every managed Xen domain must have a distinct UUID.";
      }
    ];
    environment.systemPackages = [ helper ];
    environment.etc = lib.mapAttrs' (
      name: specification: lib.nameValuePair "ryxos/xen-domains/${name}.json" { source = specification; }
    ) specifications;
    systemd.services = lib.mapAttrs' (
      name: domain:
      let
        command = "${helper}/bin/ryxos-xen-domain --spec ${specifications.${name}}";
      in
      lib.nameValuePair "ryxos-xen-${name}" {
        description = "Manage the ${name} Xen domain";
        wantedBy = lib.optionals domain.autoStart [ "multi-user.target" ];
        requires = [
          "xen-init-dom0.service"
          "xenstored.service"
        ];
        after = [
          "xen-init-dom0.service"
          "xenstored.service"
        ]
        # Order shared start/stop transactions without starting a peer or
        # requiring its success. This also covers manually started domains.
        ++ map (earlier: "ryxos-xen-${earlier}.service") predecessors.${name};
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          # Keep xl's event monitor alive if graceful stop leaves a guest
          # running. Successful shutdown removes the domain and its monitor.
          KillMode = "process";
          StateDirectory = "ryxos-xen-domains";
          StateDirectoryMode = "0700";
          UMask = "0077";
          ExecStart = "${command} start";
          ExecStop = "${command} stop";
          TimeoutStartSec = domain.startTimeoutSec + 90;
          TimeoutStopSec = domain.shutdownTimeoutSec + 90;
        };
      }
    ) cfg.domains;
  };
}
