{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ryxos.xenDomains;
  inherit (lib) mkEnableOption mkOption types;
  uuidType = types.strMatching "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
  runner = pkgs.writeShellApplication {
    name = "ryxos-xen-domain-runner";
    text = ''
      exec ${pkgs.python3}/bin/python3 -I ${./xen-domain.py} "$@"
    '';
  };
  recoveryInventory = pkgs.writeText "xen-domain-recovery-inventory.json" (
    builtins.toJSON {
      schema = 1;
      domains = { };
    }
  );
  inventory = pkgs.writeText "xen-domain-inventory.json" (
    builtins.toJSON {
      schema = 1;
      domains = lib.mapAttrs (_: specification: "${specification}") specifications;
    }
  );
  helper = pkgs.writeShellApplication {
    name = "ryxos-xen-domain";
    text = ''
      exec ${runner}/bin/ryxos-xen-domain-runner ${inventory} "$@"
    '';
  };
  specifications = lib.mapAttrs (
    name: domain:
    pkgs.writeText "xen-domain-${name}.json" (
      builtins.toJSON {
        schema = 2;
        inherit name;
        inherit (domain)
          uuid
          startTimeoutSec
          shutdownTimeoutSec
          ;
        # Interpolation imports path-valued files and preserves store references.
        configFile = "${domain.configFile}";
        xl = "${config.virtualisation.xen.package}/bin/xl";
        nixStore = "${config.nix.package}/bin/nix-store";
        qemuImg = "${config.virtualisation.xen.qemu.package}/bin/qemu-img";
        lifecycleRunner = "${runner}/bin/ryxos-xen-domain-runner";
        recoveryInventory = "${recoveryInventory}";
        toolDirectories = [
          "${config.virtualisation.xen.package}/bin"
          "${config.virtualisation.xen.qemu.package}/bin"
          "${pkgs.coreutils}/bin"
        ];
        retainPaths = map (path: "${path}") domain.retainPaths;
        disposableImage = if domain.disposableImage == null then null else "${domain.disposableImage}";
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
        Images, networks and boot artifacts are supplied by the caller. Optional
        disposable storage uses one managed qcow2 overlay over an immutable raw
        image; otherwise the caller's external storage remains unmanaged.
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
            retainPaths = mkOption {
              type = types.listOf (types.either types.path types.package);
              default = [ ];
              description = ''
                Additional immutable boot and backing artifacts retained by the
                active specification's GC lease. Use this for literal store
                paths inside configuration text that lacks Nix string context.
                The lease remains until an explicit absent-only reset.
              '';
            };
            disposableImage = mkOption {
              type = types.nullOr (types.either types.path types.package);
              default = null;
              description = ''
                Immutable raw image file for a private disposable qcow2 overlay.
                This replaces the reviewed xl configuration's entire disk list
                with one writable qdisk-backed xvda. Start/stop preserve it;
                explicit reset removes it only after Xen confirms absence twice.
                External or remote backing paths and existing qcow2 chains are
                not accepted. Null leaves all configured disks unmanaged.
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
    system.build.ryxosXenDomainHelper = helper;
    environment.etc = {
      "ryxos/xen-domains.json".source = inventory;
    }
    // lib.mapAttrs' (
      name: specification: lib.nameValuePair "ryxos/xen-domains/${name}.json" { source = specification; }
    ) specifications;
    systemd.services = lib.mapAttrs' (
      name: domain:
      let
        command = "${helper}/bin/ryxos-xen-domain";
      in
      lib.nameValuePair "ryxos-xen-${name}" {
        description = "Manage the ${name} Xen domain";
        wantedBy = lib.optionals domain.autoStart [ "multi-user.target" ];
        # Applying a new declaration must not replace a running guest.
        restartIfChanged = false;
        stopIfChanged = false;
        requires = [
          "xen-init-dom0.service"
          "xenstored.service"
        ];
        after = [
          "xen-init-dom0.service"
          "xenstored.service"
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          # Keep xl's event monitor alive if graceful stop leaves a guest
          # running. Successful shutdown removes the domain and its monitor.
          KillMode = "process";
          StateDirectory = "ryxos-xen-domains";
          StateDirectoryMode = "0700";
          UMask = "0077";
          ExecStart = "${command} start ${name}";
          # The retained spec roots this inventory-independent stop command,
          # including Python, even when its system generation is collected.
          ExecStop = "${runner}/bin/ryxos-xen-domain-runner ${recoveryInventory} stop ${name}";
          TimeoutStartSec = domain.startTimeoutSec + 180;
          # Stop uses the retained specification, whose bound can differ from
          # the current declaration after a generation switch.
          TimeoutStopSec = 690;
        };
      }
    ) cfg.domains;
  };
}
