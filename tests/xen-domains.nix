{ pkgs }:
let
  inherit (pkgs) lib;
  sample = {
    uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123";
    configFile = pkgs.writeText "sample-xl.cfg" ''
      name = "sample"
      uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123"
    '';
  };
  evaluate =
    extra:
    (import (pkgs.path + "/nixos/lib/eval-config.nix") {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        ../modules/xen-domains.nix
        {
          nixpkgs.pkgs = pkgs;
          system.stateVersion = "26.05";
          boot.loader.systemd-boot.enable = true;
          boot.initrd.systemd.enable = true;
          virtualisation.xen.enable = true;
          systemd.services.xendomains.enable = false;
          ryxos.xenDomains = {
            enable = true;
            domains.sample = sample;
          };
        }
        extra
      ];
    }).config;
  valid = evaluate { };
  disabled = evaluate { ryxos.xenDomains.enable = lib.mkForce false; };
  automatic = evaluate { ryxos.xenDomains.domains.sample.autoStart = true; };
  duplicate = evaluate { ryxos.xenDomains.domains.second = sample; };
  twoManual = evaluate {
    ryxos.xenDomains.domains.second = sample // {
      uuid = "e3f70b01-3a87-4b5a-a77e-6238f6b78123";
    };
  };
  managed = evaluate {
    ryxos.xenDomains.domains.sample = {
      disposableImage = pkgs.writeText "synthetic.raw" "synthetic raw image";
      retainPaths = [ pkgs.hello ];
      # This evaluation-only fixture checks importing a local file as a path.
      configFile = lib.mkForce ./test_xen_domain.py;
    };
  };
  specification = configuration: configuration.environment.etc."ryxos/xen-domains/sample.json".source;
  parsed =
    configuration:
    builtins.fromJSON (builtins.unsafeDiscardStringContext (specification configuration).text);
  declaredInventory = builtins.fromJSON (
    builtins.unsafeDiscardStringContext valid.environment.etc."ryxos/xen-domains.json".source.text
  );
  xenDisabled = evaluate { virtualisation.xen.enable = lib.mkForce false; };
  legacyEnabled = evaluate { systemd.services.xendomains.enable = lib.mkForce true; };
  assertionsFor =
    configuration:
    builtins.filter (
      item:
      !item.assertion
      && (
        lib.hasPrefix "ryxos.xenDomains" item.message
        || lib.hasPrefix "Disable systemd.services.xendomains" item.message
        || lib.hasPrefix "Xen domain names" item.message
        || lib.hasPrefix "Managed Xen domains" item.message
        || lib.hasPrefix "Every managed Xen domain" item.message
      )
    ) configuration.assertions;
  admitted = configuration: assertionsFor configuration == [ ];
  contract = [
    (admitted valid)
    (!(admitted duplicate))
    (!(admitted xenDisabled))
    (!(admitted legacyEnabled))
    (!(disabled.systemd.services ? ryxos-xen-sample))
    (valid.systemd.services.ryxos-xen-sample.wantedBy == [ ])
    (automatic.systemd.services.ryxos-xen-sample.wantedBy == [ "multi-user.target" ])
    # Independent domain locks allow concurrent services without peer coupling.
    (!(builtins.elem "ryxos-xen-sample.service" twoManual.systemd.services.ryxos-xen-second.after))
    (twoManual.systemd.services.ryxos-xen-second.wantedBy == [ ])
    (!(builtins.elem "ryxos-xen-sample.service" twoManual.systemd.services.ryxos-xen-second.requires))
    (valid.systemd.services.ryxos-xen-sample.serviceConfig.KillMode == "process")
    (!valid.systemd.services.ryxos-xen-sample.restartIfChanged)
    (!valid.systemd.services.ryxos-xen-sample.stopIfChanged)
    (!valid.systemd.services.ryxos-xen-sample.reloadIfChanged)
    (!automatic.systemd.services.ryxos-xen-sample.restartIfChanged)
    (!automatic.systemd.services.ryxos-xen-sample.stopIfChanged)
    (lib.hasSuffix " start sample" valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStart)
    (lib.hasSuffix " stop sample" valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStop)
    (lib.hasPrefix (parsed valid).lifecycleRunner valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStop)
    (lib.hasInfix (parsed valid).recoveryInventory valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStop)
    (lib.all (path: builtins.hasAttr path (builtins.getContext (specification valid).text)) (
      builtins.attrNames (
        builtins.getContext valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStop
      )
    ))
    (!lib.hasInfix "--spec" valid.systemd.services.ryxos-xen-sample.serviceConfig.ExecStart)
    (valid.systemd.services.ryxos-xen-sample.serviceConfig.TimeoutStopSec >= 600 + 90)
    ((parsed valid).schema == 2)
    ((parsed valid).disposableImage == null)
    (declaredInventory.schema == 1)
    (declaredInventory.domains.sample == "${specification valid}")
    ((parsed managed).disposableImage == "${managed.ryxos.xenDomains.domains.sample.disposableImage}")
    ((parsed managed).retainPaths == [ "${pkgs.hello}" ])
    (lib.hasPrefix "${builtins.storeDir}/" (parsed managed).configFile)
    (builtins.hasAttr (builtins.unsafeDiscardStringContext pkgs.hello.drvPath) (
      builtins.getContext (specification managed).text
    ))
    (builtins.hasAttr (builtins.unsafeDiscardStringContext configFileStore) (
      builtins.getContext (specification managed).text
    ))
    (builtins.elem valid.system.build.ryxosXenDomainHelper valid.environment.systemPackages)
  ];
  configFileStore = "${./test_xen_domain.py}";
in
assert lib.assertMsg (builtins.all (value: value) contract) "Xen lifecycle module contract failed";
pkgs.runCommand "xen-domain-lifecycle-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
  }
  ''
    mkdir -p source/modules source/tests
    cp ${../modules/xen-domain.py} source/modules/xen-domain.py
    cp ${./test_xen_domain.py} source/tests/test_xen_domain.py
    python3 -B -m unittest discover -s source/tests -p test_xen_domain.py -v
    touch "$out"
  ''
